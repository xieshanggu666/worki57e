# -*- coding: utf-8 -*-
"""待决事件统一管线测试：地堡危机 / 探索遭遇 / 押运途中事件三类挂起抉择
共用同一套机制——同构快照落库恢复、档案级幂等凭据回放、互斥状态机与
终局收敛；并验证旧存档格式的快照与凭据在新管线下仍可恢复、可回放。
"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    CRISIS_POOL,
    EXPEDITION_ENCOUNTERS,
    TRADE_INCIDENTS,
    TRADE_TRANSPORTING,
    PENDING_CRISIS,
    PENDING_ENCOUNTER,
    PENDING_INCIDENT,
    _PENDING_SLOTS,
    FOOD,
    WATER,
)
from tests.test_engine import make_session, FixedRand, ScriptedRand, arm_crisis
from tests.test_trade import TradeRand, rescue_offer


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


SNAPSHOT_KEYS = {
    "token", "event", "day", "title", "desc",
    "needs_target", "target_id", "target_name", "choices",
}


def _arm_encounter(eng, gs, encounter_key="cache"):
    """派遣探索队并挂起一场遭遇，返回遭遇快照。"""
    eng.rand = ScriptedRand(encounter_key=encounter_key)
    eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
    return eng.advance_day()


def _arm_incident(eng, gs):
    """申请救援订单并挂起一个途中事件，返回事件快照。"""
    eng.rand = TradeRand([0.1, 0.1])  # 审核通过、触发途中事件
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    return eng.advance_day()


# ---- 同构快照：三类待决事件同一结构 ----

def test_snapshots_share_one_structure(db):
    """三类构造器产出同一套快照键，且 token 均为一次性、发生日绑定当日。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    crisis = eng._build_crisis(CRISIS_POOL[0])
    encounter = eng._build_expedition_encounter(EXPEDITION_ENCOUNTERS[0], {})
    incident = eng._build_trade_incident(TRADE_INCIDENTS[0], {})
    for snap in (crisis, encounter, incident):
        assert SNAPSHOT_KEYS <= set(snap.keys())
        assert snap["day"] == gs.day
        assert snap["token"]
    # 一次性 token 互不相同
    tokens = {crisis["token"], encounter["token"], incident["token"]}
    assert len(tokens) == 3


def test_slot_registry_covers_all_kinds(db):
    """槽位注册表完整声明三类待决事件：持久化字段、凭据列与互斥阶段。"""
    assert set(_PENDING_SLOTS) == {PENDING_CRISIS, PENDING_ENCOUNTER, PENDING_INCIDENT}
    fields = {s["field"] for s in _PENDING_SLOTS.values()}
    creds = {s["credential_attr"] for s in _PENDING_SLOTS.values()}
    phases = {s["phase"] for s in _PENDING_SLOTS.values()}
    assert fields == {"pending_crisis", "pending_encounter", "pending_incident"}
    assert creds == {"last_resolution", "last_expedition", "last_trade"}
    assert phases == {"crisis", "expedition", "trade"}


# ---- 持久化与恢复：三类快照落库后重开档案恢复同一场抉择 ----

@pytest.mark.parametrize("kind", ["crisis", "encounter", "incident"])
def test_pending_event_survives_reload(db, kind):
    """挂起任一类待决事件 → 提交 → 新会话重开：恢复同一快照并可完成结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    if kind == "crisis":
        snap = arm_crisis(eng, "mutiny")
    elif kind == "encounter":
        snap = _arm_encounter(eng, gs)
    else:
        snap = _arm_incident(eng, gs)
    assert snap is not None
    db.commit()
    sid, token = gs.id, snap["token"]

    db2 = SessionLocal()
    try:
        gs2 = db2.get(GameSession, sid)
        eng2 = BunkerEngine(db2, gs2, rand=FixedRand())
        # 统一检出：恢复出的待决事件种类与快照 token 一致
        found_kind, found = eng2.current_pending_event()
        assert found_kind == kind
        assert found["token"] == token
        assert eng2.phase == _PENDING_SLOTS[kind]["phase"]
        # 恢复后可正常结算，快照被清除、阶段回到 daily
        if kind == "crisis":
            eng2.resolve_crisis("mutiny", "double_ration", token=token)
        elif kind == "encounter":
            eng2.resolve_expedition_encounter("search_carefully", token=token)
        else:
            eng2.resolve_trade_incident("pay_toll", token=token)
        assert eng2.current_pending_event() == (None, None)
        assert eng2.phase == "daily"
        db2.commit()
    finally:
        db2.close()


# ---- 事件互斥：同一时刻至多一个待决事件 ----

def test_current_pending_event_is_exclusive_and_prioritized(db):
    """危机优先于遭遇/途中事件被检出；清除后按顺序露出下一个。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    # 人为制造危机与遭遇同时挂起的损坏状态（正常流程互斥不可能出现）
    gs.expedition = {
        "token": "t", "status": "away", "members": [gs.residents[0].id],
        "pending_encounter": {"token": "e", "event": "cache"},
    }
    kind, snap = eng.current_pending_event()
    assert kind == PENDING_CRISIS and snap["token"] == crisis["token"]
    assert eng.phase == "crisis"
    eng.resolve_crisis("mutiny", "double_ration", token=crisis["token"])
    kind, snap = eng.current_pending_event()
    assert kind == PENDING_ENCOUNTER and snap["token"] == "e"
    assert eng.phase == "expedition"


def test_pending_event_locks_management_actions(db):
    """任一类待决事件挂起时，经营/推进动作一律被同一守卫拒绝。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    _arm_incident(eng, gs)
    assert eng.phase == "trade"
    with pytest.raises(BunkerEngineError):
        eng.advance_day()
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[1].id], {FOOD: 5, WATER: 5})


def test_mission_mutual_exclusion_both_directions(db):
    """探索队与贸易订单互斥：任一在外/在谈都阻塞另一方的发起。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[1].id])


# ---- 并发重放：统一凭据机制 ----

def test_credential_matches_shared_semantics(db):
    """统一凭据匹配：require 逐字相等；optional 双方非空才校验（旧存档缺字段不误判）。"""
    m = BunkerEngine._credential_matches
    rec = {"action": "encounter", "token": "t1", "choice": "a", "day": 3}
    assert m(rec, require={"action": "encounter"}, token="t1", choice="a")
    assert not m(rec, require={"action": "return"}, token="t1")
    assert not m(rec, require={"action": "encounter"}, token="other")
    assert not m(rec, require={"action": "encounter"}, choice="b")
    # 旧存档凭据缺字段 / 请求未带凭据：不判冲突也不误判
    old_rec = {"action": "encounter"}
    assert m(old_rec, require={"action": "encounter"}, token="t1")
    assert m(rec, require={"action": "encounter"}, token=None)
    assert not m(None, require={"action": "encounter"})


def test_credentials_are_isolated_across_kinds(db):
    """凭据按种类分列：一类事件的结算凭据不会被另一类事件的请求命中。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration", token=crisis["token"])
    assert gs.last_resolution is not None
    # 危机凭据存在期间，遭遇/途中事件请求不会被误判为回放
    with pytest.raises(BunkerEngineError):
        eng.resolve_expedition_encounter("search_carefully", token=crisis["token"])
    with pytest.raises(BunkerEngineError):
        eng.resolve_trade_incident("pay_toll", token=crisis["token"])
    assert gs.last_expedition is None
    assert gs.last_trade is None


def test_old_format_mission_credential_still_replays(db):
    """旧存档格式的幂等凭据（含 exp_token/order_token 键）在新管线下照常回放。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 手写旧版 last_expedition 凭据：遭遇已结算、队伍仍在外的状态
    gs.expedition = {
        "token": "team-1", "status": "away", "members": [gs.residents[0].id],
        "supplies": {FOOD: 5, WATER: 5}, "travel_days": 1,
        "encounters_resolved": 1, "pending_encounter": None,
        "loot": {FOOD: 8}, "casualties": [],
    }
    gs.last_expedition = {
        "action": "encounter", "token": "enc-1", "exp_token": "team-1",
        "choice": "search_carefully", "day": gs.day, "detail": "旧明细",
    }
    detail, replayed = eng.resolve_expedition_encounter(
        "search_carefully", token="enc-1"
    )
    assert replayed is True
    assert detail == "旧明细"
    assert gs.expedition["loot"] == {FOOD: 8}  # 未二次结算


def test_old_save_pending_snapshots_recover_and_resolve(db):
    """旧存档里的待决快照（遭遇/途中事件）加载后可直接恢复并完成结算。"""
    gs = make_session(db)
    rid = gs.residents[0].id
    # 手写旧版快照：在外探索队挂着待处理遭遇
    gs.expedition = {
        "token": "team-1", "status": "away", "members": [rid],
        "supplies": {FOOD: 5, WATER: 5}, "travel_days": 1,
        "encounters_resolved": 0, "loot": {}, "casualties": [],
        "pending_encounter": {
            "token": "enc-old", "event": "cache", "day": gs.day,
            "title": "废弃补给点", "desc": "", "needs_target": False,
            "target_id": None, "target_name": None,
            "choices": [
                {"key": "search_carefully", "label": "", "hint": "", "targeted": False},
            ],
        },
    }
    db.commit()
    db.expire_all()
    eng = BunkerEngine(db, db.get(GameSession, gs.id), rand=FixedRand())
    kind, snap = eng.current_pending_event()
    assert kind == PENDING_ENCOUNTER and snap["token"] == "enc-old"
    detail, replayed = eng.resolve_expedition_encounter(
        "search_carefully", token="enc-old"
    )
    assert replayed is False
    assert gs.expedition["pending_encounter"] is None
    assert gs.expedition["loot"][FOOD] == 8


def test_reconcile_409_is_uniform_across_kinds(db):
    """三类核对入口对完全陌生的凭据统一抛 409（并发重复结算的兜底）。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineConflict):
        eng.reconcile_stale_resolution("mutiny", "suppress", None, token="x")
    with pytest.raises(BunkerEngineConflict):
        eng.reconcile_stale_expedition("encounter", token="x", choice_key="a")
    with pytest.raises(BunkerEngineConflict):
        eng.reconcile_stale_trade("incident", token="x", choice_key="a")


# ---- 终局收敛：一次清算全部待决槽位 ----

def test_finish_clears_all_pending_slots(db):
    """终局收敛：危机/探索队/贸易订单三类快照一次性清空，阶段统一 ended。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    gs.pending_crisis = {"token": "c", "event": "mutiny", "choices": []}
    gs.expedition = {
        "token": "t", "status": "away", "members": [gs.residents[0].id],
        "pending_encounter": {"token": "e", "event": "cache"},
    }
    gs.trade_order = {
        "token": "o", "status": TRADE_TRANSPORTING, "escorts": [gs.residents[1].id],
        "pending_incident": {"token": "i", "event": "ambush"},
    }
    eng._finish(win=True, reason="测试终局")
    assert gs.pending_crisis is None
    assert gs.expedition is None
    assert gs.trade_order is None
    assert eng.current_pending_event() == (None, None)
    assert eng.phase == "ended"


def test_ended_save_rejects_all_decision_replays(db):
    """终局后三类结算入口统一拒绝状态变更（含凭据重放）。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration", token=crisis["token"])
    eng._finish(win=True, reason="测试终局")
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("mutiny", "double_ration", token=crisis["token"])
    with pytest.raises(BunkerEngineError):
        eng.resolve_expedition_encounter("a", token="x")
    with pytest.raises(BunkerEngineError):
        eng.resolve_trade_incident("a", token="x")
