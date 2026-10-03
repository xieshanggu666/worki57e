# -*- coding: utf-8 -*-
"""末日地堡生存 —— 核心引擎的可测试纯逻辑，验证资源守恒、危机决策、结局判定。

注意：测试使用独立内存级 Session，需清空表。为隔离，这里用 engine 建临时表。
"""
import pytest
from sqlalchemy.orm import Session

from app.core.database import Base, engine, SessionLocal
from app.core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from app.models import GameSession, Resident, Facility
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    CRISIS_POOL,
    FACILITY_ZH,
    FOOD,
    OXY,
    POWER,
    WATER,
)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def make_session(db, residents=3, resources=None):
    gs = GameSession(
        name="测试",
        day=1,
        target_day=SURVIVAL_TARGET_DAY,
        status="running",
        resources=resources or dict(INITIAL_RESOURCES),
        survivors=residents,
        score=0,
    )
    db.add(gs)
    db.flush()
    for i in range(residents):
        db.add(Resident(session_id=gs.id, name=f"人{i}", job="general", health=90, morale=80, alive=1, joined_day=1))
    for cat in ("power", "farm", "water", "oxygen"):
        db.add(Facility(session_id=gs.id, name=FACILITY_ZH[cat], category=cat, level=1, status="active", built_day=1))
    db.commit()
    db.refresh(gs)
    return gs


class FixedRand:
    """固定值随机 —— 每个 .random() 返回 0.9（不触发危机，因 0.9 > 0.45）。"""

    def random(self):
        return 0.9

    def choice(self, seq):
        return seq[0]


class TriggerRand(FixedRand):
    """必定触发危机（0.1 <= 0.45），事件取危机池第一项。"""

    def random(self):
        return 0.1


def arm_crisis(eng, event_key, target=None):
    """在档案上挂起一个待处理危机（模拟推进触发后等待抉择的状态）。"""
    event = next(e for e in CRISIS_POOL if e["key"] == event_key)
    crisis = eng._build_crisis(event)
    if target is not None:
        crisis["needs_target"] = True
        crisis["target_id"] = target.id
        crisis["target_name"] = target.name
    eng.session.pending_crisis = crisis
    return crisis


def test_advance_increments_day(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.advance_day()
    assert gs.day == 2


def test_resources_change_with_population(db):
    """资源应有产出-消耗的净变化（守恒循环运行）。"""
    gs = make_session(db, residents=3)
    before = dict(gs.resources)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.advance_day()
    after = gs.resources
    # 至少一个资源发生变化
    assert any(abs(after[k] - before[k]) > 0.01 for k in ("food", "water", "power", "oxygen"))


def test_build_deducts_cost(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    food_before = gs.resources[FOOD]
    eng.build_facility("med")
    assert gs.resources[FOOD] < food_before
    assert any(f.category == "med" for f in gs.facilities)


def test_build_fails_when_poor(db):
    gs = make_session(db)
    gs.resources = {FOOD: 1, WATER: 1, POWER: 1, OXY: 1}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.build_facility("farm")


def test_upgrade_increases_level(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    fac = [f for f in gs.facilities if f.category == "farm"][0]
    eng.upgrade_facility(fac.id)
    assert fac.level == 2


def test_crisis_applies_resource_effects(db):
    """选择翻倍食物选项应减食物。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 挂起待处理危机后结算（危机必须先经“推进触发”写入存档）
    event = CRISIS_POOL[0]
    arm_crisis(eng, event["key"])
    choice = event["choices"][0]
    eff = choice["effects"].get("resources", {}).get(FOOD, 0)
    food_before = gs.resources[FOOD]
    detail, replayed = eng.resolve_crisis(event["key"], choice["key"])
    assert replayed is False
    assert gs.resources[FOOD] <= food_before + eff + 1
    assert gs.pending_crisis is None


def test_job_assignment_changes_resident(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    r = gs.residents[0]
    eng.set_job(r.id, "farmer")
    assert r.job == "farmer"


def test_win_at_target_day(db):
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    gs.day = SURVIVAL_TARGET_DAY  # 目标天数
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._check_end()
    assert gs.status == "win"


def test_population_zero_ends_game(db):
    gs = make_session(db)
    for r in gs.residents:
        r.alive = 0
    gs.survivors = 0
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._check_end()
    assert gs.status == "over"


def test_advance_rejected_after_game_end(db):
    gs = make_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.advance_day()


def test_morale_recovery_toward_75(db):
    gs = make_session(db)
    for r in gs.residents:
        r.morale = 40
    gs.resources = {FOOD: 999, WATER: 999, POWER: 999, OXY: 999}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._apply_health_morale()
    assert all(r.morale > 40 for r in gs.residents)


# ---- 目标归属校验：防止跨档案数据污染 ----

def test_foreign_archive_target_rejected(db):
    """提交其他档案的居民编号：报错且本档案居民/资源均不受影响。"""
    gs = make_session(db)
    other = make_session(db)
    foreign_id = other.residents[0].id

    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 待处理疫病的目标已绑定为本档案某居民，跨档案编号必须被拒绝
    arm_crisis(eng, "sick", target=gs.residents[0])
    health_before = [r.health for r in gs.residents]
    food_before = gs.resources[FOOD]
    # 疫病·隔离：扣食物且对目标造成健康 -5
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=foreign_id)
    # 本档案无人受到伤害
    assert [r.health for r in gs.residents] == health_before
    # 目标校验在资源结算之前，资源也不应被扣减
    assert gs.resources[FOOD] == food_before
    # 失败结算不得清掉待处理危机，玩家仍需抉择
    assert gs.pending_crisis is not None


def test_nonexistent_target_rejected(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=gs.residents[0])
    health_before = [r.health for r in gs.residents]
    missing_id = max(r.id for r in gs.residents) + 9999
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=missing_id)
    assert [r.health for r in gs.residents] == health_before


def test_dead_target_rejected(db):
    gs = make_session(db)
    dead = gs.residents[0]
    dead.alive = 0
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=dead)
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=dead.id)


def test_valid_target_only_affects_that_resident(db):
    """有效本档案目标：健康效果只作用于其本人，不波及其他居民。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    target = gs.residents[1]
    arm_crisis(eng, "raid", target=target)
    others = [r for r in gs.residents if r.id != target.id]
    others_before = [r.health for r in others]
    # 盗匪·武装抵抗：健康 -8
    eng.resolve_crisis("raid", "defend", target_id=target.id)
    assert target.health == 82  # 90 - 8
    assert [r.health for r in others] == others_before


def test_no_target_applies_to_all_alive(db):
    """未提供目标时，士气类全体效果仍按原语义作用于全体存活者。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")  # 士气 +20
    assert all(r.morale == 100 for r in gs.residents if r.alive)


# ---- 前后端目标语义统一：作用域由事件效果声明，而非客户端回传 ----

def test_all_scope_crisis_carries_no_target(db):
    """内讧（纯全体士气事件）生成待决策时不应随机出目标。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    assert crisis["needs_target"] is False
    assert crisis["target_id"] is None
    assert crisis["target_name"] is None
    assert all(c["targeted"] is False for c in crisis["choices"])


def test_single_scope_crisis_carries_target_and_flags(db):
    """疫病存在单体决策，须随机目标；隔离=单人，全员消毒=全体。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "sick")
    assert crisis["needs_target"] is True
    assert crisis["target_id"] is not None
    flags = {c["key"]: c["targeted"] for c in crisis["choices"]}
    assert flags == {"quarantine": True, "public_health": False}


def test_global_morale_ignores_client_target(db):
    """回归：前端无条件回传随机目标时，全体士气决策仍须作用于全体存活者。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    random_target = gs.residents[0]
    others = [r for r in gs.residents if r.id != random_target.id]
    before = {r.id: r.morale for r in gs.residents}
    # 内讧·严令镇压：士气 -15（全体），即便带了目标编号也不应收窄
    eng.resolve_crisis("mutiny", "suppress", target_id=random_target.id)
    assert random_target.morale == before[random_target.id] - 15
    for r in others:
        assert r.morale == before[r.id] - 15


def test_global_scope_ignores_even_foreign_target(db):
    """全体效果不做目标校验：跨档案编号也不会让结算失败或作用于单人。"""
    gs = make_session(db)
    other = make_session(db)
    foreign_id = other.residents[0].id
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "suppress", target_id=foreign_id)
    assert all(r.morale == 65 for r in gs.residents if r.alive)


def test_single_scope_requires_target(db):
    """单体决策缺少目标时报错，且不产生任何部分结算。"""
    gs = make_session(db)
    food_before = gs.resources[FOOD]
    health_before = [r.health for r in gs.residents]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=gs.residents[0])
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=None)
    assert gs.resources[FOOD] == food_before
    assert [r.health for r in gs.residents] == health_before
    assert gs.pending_crisis is not None  # 失败结算不清除待处理危机


def test_single_vs_all_choice_scope_within_one_event(db):
    """同一疫病事件：隔离只伤目标，全员消毒不动任何人健康。"""
    gs = make_session(db)
    target = gs.residents[0]

    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "sick", target=target)
    eng.resolve_crisis("sick", "quarantine", target_id=target.id)
    assert target.health == 85
    assert all(r.health == 90 for r in gs.residents if r.id != target.id)

    # 全员消毒：资源效果，无健康伤害，target_id 被忽略
    other = gs.residents[1]
    arm_crisis(eng, "sick", target=other)
    eng.resolve_crisis("sick", "public_health", target_id=other.id)
    assert other.health == 90
    assert target.health == 85  # 上一步的目标不受本次影响


def test_log_scope_matches_settlement(db):
    """日志作用域标注必须与实际结算一致：单体写姓名，全体写全体。"""
    from app.models import EventLog

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    target = gs.residents[1]
    arm_crisis(eng, "raid", target=target)
    eng.resolve_crisis("raid", "defend", target_id=target.id)
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")
    db.commit()

    logs = db.query(EventLog).filter_by(session_id=gs.id).order_by(EventLog.id).all()
    single_log = next(l for l in logs if "武装抵抗" in (l.detail or ""))
    global_log = next(l for l in logs if "加倍发放食物" in (l.detail or ""))
    assert target.name in single_log.detail
    assert "全体" in global_log.detail


def test_resource_change_persists_across_sessions(db):
    """资源 JSON 变更须真正落库（重新打开会话仍可见）。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    sid = gs.id
    before = gs.resources[FOOD]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")  # 食物 -20
    db.commit()

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        assert reloaded.resources[FOOD] == round(max(0.0, before - 20), 1)
    finally:
        db2.close()


# ---- 结算边界：已结束档案拒绝一切状态变更 ----

def test_actions_rejected_after_game_end(db):
    gs = make_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=FixedRand())
    rid = gs.residents[0].id
    fid = gs.facilities[0].id
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=rid)
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid, "farmer")


# ---- 待处理危机入档：不可跳过、刷新恢复、绑定事件与目标 ----

def test_advance_blocked_while_crisis_pending(db):
    """危机待处理时推进一天必须被拒绝，且日期不前进、危机不被覆盖。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    crisis = eng.advance_day()
    assert crisis is not None
    day = gs.day
    token = crisis["token"]

    eng2 = BunkerEngine(db, gs, rand=TriggerRand())
    with pytest.raises(BunkerEngineError):
        eng2.advance_day()
    assert gs.day == day
    assert gs.pending_crisis["token"] == token  # 原有危机未被跳过/覆盖


def test_crisis_persisted_and_recoverable_after_reload(db):
    """待处理危机随存档落库，重新打开会话仍能恢复同一个决策。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    crisis = eng.advance_day()
    db.commit()
    sid, event, token, target_id = gs.id, crisis["event"], crisis["token"], crisis["target_id"]

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        pending = reloaded.pending_crisis
        assert pending is not None
        assert pending["event"] == event
        assert pending["token"] == token
        assert pending["target_id"] == target_id
        # 恢复后可正常完成结算
        eng2 = BunkerEngine(db2, reloaded, rand=FixedRand())
        choice_key = next(
            c["key"] for c in pending["choices"] if not c["targeted"]
        )
        eng2.resolve_crisis(event, choice_key, token=token)
        assert reloaded.pending_crisis is None
        db2.commit()
    finally:
        db2.close()


def test_resolve_without_pending_crisis_rejected(db):
    """没有待处理危机时凭空提交任意事件结算：拒绝且资源不变。"""
    gs = make_session(db)
    food_before = gs.resources[FOOD]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("scavenge", "crack_open")
    assert gs.resources[FOOD] == food_before


def test_resolve_wrong_event_rejected(db):
    """挂起的是 A 事件，提交 B 事件的抉择：拒绝且不结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    arm_crisis(eng, "mutiny")
    food_before = gs.resources[FOOD]
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("scavenge", "crack_open")
    assert gs.resources[FOOD] == food_before
    assert gs.pending_crisis["event"] == "mutiny"


def test_target_bound_to_pending_crisis(db):
    """单体决策的目标不可替换成其他居民。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    bound = gs.residents[0]
    other = gs.residents[1]
    arm_crisis(eng, "sick", target=bound)
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=other.id)
    assert bound.health == 90 and other.health == 90
    assert gs.pending_crisis is not None


def test_duplicate_resolve_settles_once(db):
    """同一抉择重复提交：第二次为幂等回放，效果只施加一次、日志只有一条。"""
    from app.models import EventLog

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    before = gs.resources[FOOD]

    detail1, replay1 = eng.resolve_crisis(
        "mutiny", "double_ration", token=crisis["token"]
    )
    after_first = gs.resources[FOOD]
    assert replay1 is False
    assert after_first == round(before - 20, 1)

    # 紧接着重复提交（同一引擎/同一事务内模拟用户连点）
    detail2, replay2 = eng.resolve_crisis(
        "mutiny", "double_ration", token=crisis["token"]
    )
    assert replay2 is True
    assert detail2 == detail1
    assert gs.resources[FOOD] == after_first  # 没有第二次扣减

    db.commit()
    logs = db.query(EventLog).filter_by(session_id=gs.id).count()
    assert logs == 1  # 只写了一条危机日志


def test_different_choice_after_resolve_rejected(db):
    """结算完成后改用另一选项再次提交：不得二次结算，直接拒绝。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration", token=crisis["token"])
    morale_after = gs.residents[0].morale
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("mutiny", "suppress", token=crisis["token"])
    assert gs.residents[0].morale == morale_after  # 未追加 -15


def test_same_event_next_day_is_not_a_replay(db):
    """第二天又触发同类型危机时，新结算不得被前一天的幂等记录拦截。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())

    c1 = arm_crisis(eng, "mutiny")
    gs.day = 5
    c1["day"] = 5
    food_d5 = gs.resources[FOOD]
    eng.resolve_crisis("mutiny", "double_ration", token=c1["token"])
    assert gs.resources[FOOD] == round(food_d5 - 20, 1)

    c2 = arm_crisis(eng, "mutiny")
    gs.day = 6
    c2["day"] = 6
    food_d6 = gs.resources[FOOD]
    # 同事件同选项、不同 token 不同天：必须是一次全新结算而非回放
    detail, replayed = eng.resolve_crisis("mutiny", "double_ration", token=c2["token"])
    assert replayed is False
    assert gs.resources[FOOD] == round(food_d6 - 20, 1)
    assert gs.last_resolution["day"] == 6


def test_stale_token_rejected(db):
    """token 与当前待处理危机不符（过期/串档请求）：拒绝结算。"""
    from app.services.engine import BunkerEngineConflict

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_crisis("mutiny", "suppress", token="stale-token")
    assert gs.pending_crisis is not None
    assert gs.pending_crisis["token"] == crisis["token"]


def test_concurrent_resolve_only_one_wins(db):
    """两个独立会话并发结算同一危机：乐观锁保证效果只落一次。"""
    from sqlalchemy.orm.exc import StaleDataError

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = arm_crisis(eng, "mutiny")
    db.commit()
    sid, token = gs.id, crisis["token"]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        ga, gb = db_a.get(GameSession, sid), db_b.get(GameSession, sid)
        ea = BunkerEngine(db_a, ga, rand=FixedRand())
        eb = BunkerEngine(db_b, gb, rand=FixedRand())
        ea.resolve_crisis("mutiny", "double_ration", token=token)
        db_a.commit()
        # B 持有的版本号已过期：提交时 StaleDataError，食物不会被再扣一次
        eb.resolve_crisis("mutiny", "double_ration", token=token)
        with pytest.raises(StaleDataError):
            db_b.commit()
        db_b.rollback()

        final = db_a.get(GameSession, sid)
        assert final.resources[FOOD] == round(300 - 20, 1)
        assert final.pending_crisis is None
    finally:
        db_a.close()
        db_b.close()


def test_operations_locked_during_crisis_phase(db):
    """危机阶段统一拒绝建造/升级/调岗，状态机只有 daily/crisis/ended。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    eng.advance_day()
    fid = gs.facilities[0].id
    rid = gs.residents[0].id
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid, "farmer")


def test_triggered_crisis_matches_pool_and_phase(db):
    """推进触发危机后进入 crisis 阶段，待处理事件来自危机池且带 token。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    crisis = eng.advance_day()
    assert eng.phase == "crisis"
    assert crisis["event"] == CRISIS_POOL[0]["key"]
    assert crisis["token"]
    # 完成结算后回到每日阶段
    all_choice = next(c["key"] for c in crisis["choices"] if not c["targeted"])
    eng.resolve_crisis(crisis["event"], all_choice, token=crisis["token"])
    assert eng.phase == "daily"


def test_reaching_target_day_ends_without_pending_crisis(db):
    """终局优先：抵达目标日直接胜利，不挂起无法处理的危机。"""
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    gs.day = SURVIVAL_TARGET_DAY - 1
    eng = BunkerEngine(db, gs, rand=TriggerRand())  # 即便必定触发危机
    crisis = eng.advance_day()
    assert crisis is None
    assert gs.status == "win"
    assert gs.pending_crisis is None
    assert eng.phase == "ended"


def test_old_archive_migration_backfills_columns(db):
    """旧结构表（无新列）经 ensure_schema 后可正常读写，旧档案停在每日阶段。"""
    from sqlalchemy import text
    from app.core.migration import ensure_schema as ensure_schema_migration
    from app.core import database as db_module

    gs = make_session(db)
    db.commit()
    # expire_all：避免 ORM 中被标脏的 GameSession 在 DDL 后 autoflush 到空表，
    # 保证下面的“旧表数据搬运”基于已提交的真实存量行
    db.expire_all()
    # 模拟旧版表结构：移除新增列（SQLite 走重建表）
    db.execute(text("ALTER TABLE game_sessions RENAME TO game_sessions_old"))
    db.execute(text(
        "CREATE TABLE game_sessions ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
        "status VARCHAR(16), resources JSON, survivors INTEGER, outcome JSON, "
        "score INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    db.execute(text(
        "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
        "survivors,outcome,score,created_at,updated_at FROM game_sessions_old"
    ))
    db.execute(text("DROP TABLE game_sessions_old"))
    db.commit()

    ensure_schema_migration(db_module.engine)
    db.expire_all()
    gs = db.query(GameSession).first()
    assert gs.pending_crisis is None
    assert gs.last_resolution is None
    assert gs.row_version == 1
    # 旧档案处于每日阶段，可正常推进
    eng = BunkerEngine(db, gs, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()
    db.commit()


# ---- 探索队系统 ----

class ScriptedRand:
    """可脚本化的随机数：random 返回固定值，choice 按指定 key 选择（默认首项）。"""

    def __init__(self, random_val=0.5, encounter_key=None):
        self.random_val = random_val
        self.encounter_key = encounter_key

    def random(self):
        return self.random_val  # 0.5 <= 0.85，触发探索遭遇

    def choice(self, seq):
        if self.encounter_key:
            for item in seq:
                if isinstance(item, dict) and item.get("key") == self.encounter_key:
                    return item
        return seq[0]


def test_send_expedition_deducts_supplies(db):
    """派遣探索队：扣除自带物资、写入队伍状态、队员标记为离堡。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    members = [gs.residents[0].id, gs.residents[1].id]
    food_before = gs.resources[FOOD]
    water_before = gs.resources[WATER]
    exp = eng.send_expedition(members, {FOOD: 10, WATER: 8})
    assert exp["status"] == "away"
    assert exp["members"] == members
    assert exp["supplies"][FOOD] == 10
    assert exp["supplies"][WATER] == 8
    assert gs.resources[FOOD] == round(food_before - 10, 1)
    assert gs.resources[WATER] == round(water_before - 8, 1)
    # 队员被标记为离堡
    assert gs.residents[0].id in eng._away_resident_ids()
    assert gs.residents[1].id in eng._away_resident_ids()
    assert eng.phase == "daily"  # 队伍在外但无遭遇，仍可推进


def test_send_expedition_requires_members(db):
    """未选择队员：拒绝派遣。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([], {FOOD: 10})
    assert gs.expedition is None


def test_send_expedition_rejects_second_team(db):
    """已有探索队在外：拒绝派遣第二支。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.send_expedition([gs.residents[0].id], {FOOD: 5, WATER: 5})
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[1].id], {FOOD: 5, WATER: 5})


def test_send_expedition_insufficient_supplies(db):
    """物资不足：拒绝派遣且不扣资源。"""
    gs = make_session(db, resources={FOOD: 2, WATER: 2, POWER: 100, OXY: 100})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    assert gs.expedition is None
    assert gs.resources[FOOD] == 2


def test_away_residents_pause_production(db):
    """离堡人员暂停地堡生产：农夫离队后 job_count 归零，消耗只计在堡人口。"""
    gs = make_session(db)
    gs.residents[1].job = "farmer"  # 人为设定一名农夫
    farmer = gs.residents[1]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    assert eng.job_count("farmer") == 1
    eng.send_expedition([farmer.id], {FOOD: 5, WATER: 5})
    # 农夫离队：不参与产出
    assert eng.job_count("farmer") == 0
    # 在堡人口为 2（3 人减去 1 名离队者）
    assert eng._in_bunker_count() == 2


def test_advance_triggers_expedition_encounter(db):
    """探索队在外时推进一天：触发探索遭遇（而非地堡危机），进入 expedition 阶段。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    assert encounter is not None
    assert encounter["event"] == "cache"
    assert gs.expedition["pending_encounter"] is not None
    assert eng.phase == "expedition"
    # 遭遇待处理时无法继续推进
    with pytest.raises(BunkerEngineError):
        eng.advance_day()


def test_resolve_expedition_encounter_gives_loot(db):
    """处理探索遭遇：战利品累计到队伍，待处理遭遇清除。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    choice = next(c for c in encounter["choices"] if c["key"] == "search_carefully")
    detail, replayed = eng.resolve_expedition_encounter(choice["key"], token=encounter["token"])
    assert replayed is False
    assert gs.expedition["pending_encounter"] is None
    # 战利品累计（cache·仔细搜索：食物+8 水+6）
    assert gs.expedition["loot"][FOOD] == 8
    assert gs.expedition["loot"][WATER] == 6
    assert "战利品" in detail


def test_return_expedition_settles_loot(db):
    """返程结算：战利品入库、剩余物资归还、队伍状态清除。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
    food_before = gs.resources[FOOD]
    water_before = gs.resources[WATER]
    detail, replayed = eng.return_expedition(token=gs.expedition["token"])
    assert replayed is False
    assert gs.expedition is None
    # 战利品入库（食物+8 水+6），剩余物资归还（消耗 1 天后剩 9 食物 9 水）
    assert gs.resources[FOOD] == round(food_before + 8 + 9, 1)
    assert gs.resources[WATER] == round(water_before + 6 + 9, 1)
    assert "战利品" in detail


def test_return_expedition_idempotent(db):
    """重复返程：第二次为幂等回放，战利品只结算一次。

    队伍清除后档案级 last_expedition 凭据仍可识别重复/并发落败请求，
    统一返回上次结果而非报错（与危机结算的幂等口径一致）。
    """
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
    food_after_encounter = gs.resources[FOOD]
    exp_token = gs.expedition["token"]
    detail1, replay1 = eng.return_expedition(token=exp_token)
    assert replay1 is False
    food_after_return = gs.resources[FOOD]
    # 队伍已清除，再次返程命中档案级幂等凭据：回放而不二次发放战利品
    detail2, replay2 = eng.return_expedition(token=exp_token)
    assert replay2 is True
    assert detail2 == detail1
    assert gs.expedition is None
    assert gs.resources[FOOD] == food_after_return  # 没有第二次发放
    # 带错队伍 token 的返程则必须拒绝（不能回放别人的结算）
    from app.services.engine import BunkerEngineConflict
    with pytest.raises(BunkerEngineConflict):
        eng.return_expedition(token="some-other-team-token")


def test_forced_return_when_supplies_out(db):
    """补给耗尽：强制返程，队伍立即结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    # 2 人队只带 1 天口粮（2 食物 2 水），行军一天后即耗尽
    eng.send_expedition([gs.residents[0].id, gs.residents[1].id], {FOOD: 2, WATER: 2})
    eng.advance_day()
    # 补给耗尽触发强制返程，队伍已结算
    assert gs.expedition is None


def test_expedition_encounter_casualty(db):
    """遭遇导致队员阵亡：伤亡记录在案，返程时不再重复扣减人口。

    单人队阵亡即“全员失联”，遭遇结算后队伍当场收敛返程（不留下无活人
    的僵尸队伍），人口在遭遇中即时扣减一次，返程结算不再重复扣减。
    """
    gs = make_session(db)
    victim = gs.residents[0]
    victim.health = 10  # 重伤员，遭遇陷阱即可能阵亡
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="trap"))
    eng.send_expedition([victim.id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    before_survivors = gs.survivors
    # 陷阱·强行挣脱：健康 -18（单体），10 - 18 = -8 → 阵亡
    detail, replayed = eng.resolve_expedition_encounter("force_free", token=encounter["token"])
    assert replayed is False
    assert victim.alive == 0
    # 全员阵亡当场收敛：队伍已返程清除，人口已即时扣减且只扣一次
    assert gs.expedition is None
    assert gs.survivors == before_survivors - 1
    assert gs.last_expedition["action"] == "return"
    assert gs.last_expedition["token"] == encounter["token"]  # 返程凭据挂住遭遇 token
    # 同一遭遇连点：幂等回放，不二次扣减人口
    detail2, replay2 = eng.resolve_expedition_encounter(
        "force_free", token=encounter["token"]
    )
    assert replay2 is True
    assert detail2 == detail
    assert gs.survivors == before_survivors - 1


def test_expedition_encounter_partial_casualty_keeps_team(db):
    """多人队部分阵亡：队伍继续在外，人口即时扣减；返程时不重复扣减。"""
    gs = make_session(db)
    victim = gs.residents[0]
    victim.health = 10
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="trap"))
    eng.send_expedition([victim.id, gs.residents[1].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    before_survivors = gs.survivors
    eng.resolve_expedition_encounter("force_free", token=encounter["token"])
    assert victim.alive == 0
    assert victim.id in gs.expedition["casualties"]
    assert gs.expedition is not None  # 仍有幸存队员，队伍继续在外
    assert gs.survivors == before_survivors - 1
    # 返程结算：不再重复扣减人口
    eng.return_expedition(token=gs.expedition["token"])
    assert gs.expedition is None
    assert gs.survivors == before_survivors - 1


def test_away_residents_cannot_be_assigned(db):
    """探索队中的居民无法调整岗位。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.send_expedition([gs.residents[0].id], {FOOD: 5, WATER: 5})
    with pytest.raises(BunkerEngineError):
        eng.set_job(gs.residents[0].id, "farmer")


def test_refresh_recovers_expedition(db):
    """刷新/重进档案后探索队状态与待处理遭遇可恢复，且能正常结算。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    db.commit()
    sid = gs.id
    exp_token = gs.expedition["token"]
    enc_token = encounter["token"]

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        assert reloaded.expedition is not None
        assert reloaded.expedition["token"] == exp_token
        assert reloaded.expedition["pending_encounter"]["token"] == enc_token
        # 恢复后可正常处理遭遇
        eng2 = BunkerEngine(db2, reloaded, rand=FixedRand())
        eng2.resolve_expedition_encounter("search_carefully", token=enc_token)
        assert reloaded.expedition["pending_encounter"] is None
        db2.commit()
    finally:
        db2.close()


def test_expedition_encounter_token_mismatch_rejected(db):
    """遭遇 token 与存档不符（过期/串档）：拒绝结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    eng.advance_day()
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_expedition_encounter("search_carefully", token="stale-token")
    assert gs.expedition["pending_encounter"] is not None


def test_expedition_blocked_during_crisis(db):
    """地堡危机待处理时：无法派遣探索队。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    eng.advance_day()  # 触发地堡危机
    assert eng.phase == "crisis"
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[0].id], {FOOD: 5, WATER: 5})


def test_expedition_survivor_joins_mid_journey(db):
    """偶遇幸存者：新成员加入队伍，返程时一同归来。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="survivors"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    before_count = len(gs.expedition["members"])
    eng.resolve_expedition_encounter("accept", token=encounter["token"])
    # 新成员加入队伍
    assert len(gs.expedition["members"]) == before_count + 1
    assert gs.survivors == 4  # 总人口增加

# ---- 统一状态流转：阶段守卫、终局收敛、前后端口径一致 ----

def _force_exp_day(gs, eng, day):
    """把在外探索队推进到指定行军天数（构造期满返程场景）。"""
    gs.expedition["travel_days"] = day


def test_operations_locked_during_expedition_phase(db):
    """探索遭遇挂起（expedition 阶段）：建造/升级/调岗/返程全部被后端拒绝。

    前端按钮禁用之外，后端守卫必须独立成立，防止绕过页面直接打 API。
    """
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
    eng.advance_day()  # 遭遇挂起
    assert eng.phase == "expedition"
    fid = gs.facilities[0].id
    rid_in_bunker = gs.residents[1].id
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid_in_bunker, "farmer")
    with pytest.raises(BunkerEngineError):
        eng.return_expedition(token=gs.expedition["token"])


def test_away_expedition_never_pends_bunker_crisis(db):
    """状态不变量：探索队在外的行军日只会挂起遭遇，绝不挂起地堡危机。

    “危机待处理 + 探索队在外”在正常流程中不可达（派遣只允许在 daily 阶段），
    因此返程无需单独拦截危机阶段——这里用不变量把它钉死。
    """
    gs = make_session(db)
    gs.resources = {FOOD: 999, WATER: 999, POWER: 999, OXY: 999}
    # 即便地堡危机概率拉满：在外行军日也只能触发遭遇
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    eng.send_expedition([gs.residents[0].id], {FOOD: 30, WATER: 30})
    result = eng.advance_day()
    assert gs.pending_crisis is None
    assert result is gs.expedition["pending_encounter"]


def test_encounter_resolve_records_archive_level_idempotency(db):
    """遭遇结算后档案上留下 last_expedition 凭据；同 token 同选项连点只回放。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
    encounter = eng.advance_day()
    detail1, replay1 = eng.resolve_expedition_encounter(
        "search_carefully", token=encounter["token"]
    )
    assert replay1 is False
    assert gs.last_expedition["action"] == "encounter"
    assert gs.last_expedition["token"] == encounter["token"]
    loot_after = dict(gs.expedition["loot"])
    # 连点同一抉择：幂等回放，战利品不二次累计
    detail2, replay2 = eng.resolve_expedition_encounter(
        "search_carefully", token=encounter["token"]
    )
    assert replay2 is True
    assert detail2 == detail1
    assert gs.expedition["loot"] == loot_after


def test_encounter_replay_then_return_takes_over(db):
    """遭遇结算后连点可回放；若另一请求已召回清队，过期遭遇请求按 409 拒绝。

    档案只保留最近一次探索队动作的幂等凭据（与 last_resolution 同构）：
    返程凭据覆盖遭遇凭据后，旧遭遇请求不应被当成新结算，也不应回放成返程结果。
    """
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
    encounter = eng.advance_day()
    token = encounter["token"]
    detail1, replay1 = eng.resolve_expedition_encounter("search_carefully", token=token)
    assert replay1 is False
    # 队伍尚未返程：同一遭遇连点安全回放
    detail2, replay2 = eng.resolve_expedition_encounter("search_carefully", token=token)
    assert replay2 is True and detail2 == detail1
    # 另一请求把队伍召回，档案状态前进
    team_token = gs.expedition["token"]
    eng.return_expedition(token=team_token)
    assert gs.expedition is None
    # 旧遭遇请求重试：状态已变化，409 拒绝（前端据此刷新），不二次结算
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_expedition_encounter("search_carefully", token=token)


def test_return_wrong_team_token_is_conflict(db):
    """返程后携带错误队伍 token 重试：409 冲突，不能回放成别人的结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
    real_token = gs.expedition["token"]
    eng.return_expedition(token=real_token)
    with pytest.raises(BunkerEngineConflict):
        eng.return_expedition(token="wrong-team-token")


def test_concurrent_encounter_only_one_wins(db):
    """两个独立会话并发结算同一遭遇：乐观锁保证战利品只累计一次。"""
    from sqlalchemy.orm.exc import StaleDataError

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
    encounter = eng.advance_day()
    db.commit()
    sid, token = gs.id, encounter["token"]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        ga, gb = db_a.get(GameSession, sid), db_b.get(GameSession, sid)
        ea = BunkerEngine(db_a, ga, rand=FixedRand())
        eb = BunkerEngine(db_b, gb, rand=FixedRand())
        ea.resolve_expedition_encounter("search_carefully", token=token)
        db_a.commit()
        eb.resolve_expedition_encounter("search_carefully", token=token)
        with pytest.raises(StaleDataError):
            db_b.commit()
        db_b.rollback()

        final = db_a.get(GameSession, sid)
        assert final.expedition["loot"][FOOD] == 8
        assert final.expedition["loot"][WATER] == 6
        assert final.last_expedition["action"] == "encounter"
    finally:
        db_a.close()
        db_b.close()


def test_concurrent_return_only_one_wins(db):
    """两个独立会话并发召回同一队伍：乐观锁保证战利品只入库一次。"""
    from sqlalchemy.orm.exc import StaleDataError

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
    db.commit()
    sid, exp_token = gs.id, gs.expedition["token"]
    # 结算后地堡食物基数（不含尚未入库的战利品 8）
    base_food = db.get(GameSession, sid).resources[FOOD]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        ga, gb = db_a.get(GameSession, sid), db_b.get(GameSession, sid)
        BunkerEngine(db_a, ga).return_expedition(token=exp_token)
        db_a.commit()
        BunkerEngine(db_b, gb).return_expedition(token=exp_token)
        with pytest.raises(StaleDataError):
            db_b.commit()
        db_b.rollback()

        final = db_a.get(GameSession, sid)
        assert final.expedition is None
        # 战利品 8 + 剩余口粮 9 只入库一次
        assert final.resources[FOOD] == round(base_food + 8 + 9, 1)
    finally:
        db_a.close()
        db_b.close()


def test_forced_return_at_max_days(db):
    """探索期满（达到最长行军天数）：当日强制返程结算，不再挂遭遇。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 50, WATER: 50})
    gs.expedition["travel_days"] = 6  # 下一日行军后达到 7 天上限
    result = eng.advance_day()
    assert result is None
    assert gs.expedition is None


def test_end_at_target_day_settles_away_expedition(db):
    """胜利日探索队仍在外：先返程结算（战利品/余粮入库、成员归队）再终局。"""
    gs = make_session(db, resources={FOOD: 999, WATER: 999, POWER: 999, OXY: 999})
    gs.day = SURVIVAL_TARGET_DAY - 1
    eng = BunkerEngine(db, gs, rand=ScriptedRand(random_val=0.95, encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
    result = eng.advance_day()  # 0.95 > 0.85，不触发遭遇，直接到目标日
    assert result is None
    assert gs.status == "win"
    assert gs.expedition is None          # 无僵尸队伍
    assert gs.pending_crisis is None
    # 成员平安归队，人口仍是初始 3
    assert gs.survivors == 3


def test_endgame_clears_expedition_snapshot(db):
    """直接命中终局（人口归零）时：在外队伍快照一并清除，统一收敛 ended。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    for r in gs.residents:
        r.alive = 0
    gs.survivors = 0
    assert eng._check_end() is True
    assert gs.status == "over"
    assert gs.expedition is None
    assert eng.phase == "ended"


def test_crisis_all_scope_skips_away_members(db):
    """地堡危机的全体效果只作用在堡居民；探索队成员士气不受波及。"""
    gs = make_session(db)
    away = gs.residents[0]
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    eng.send_expedition([away.id], {FOOD: 20, WATER: 20})
    # 行军日不触发地堡危机；在 daily 阶段手工挂一个内讧危机
    crisis = arm_crisis(eng, "mutiny")
    morale_before = away.morale
    eng.resolve_crisis("mutiny", "suppress", token=crisis["token"])
    assert away.morale == morale_before  # 探索队在外，不吃地堡 -15 士气
    assert gs.residents[1].morale == 65


def test_bunker_production_morale_uses_in_bunker_only(db):
    """地堡产出的士气系数只计在堡人员：队员士气低不拖累地堡产出。"""
    gs = make_session(db, resources={FOOD: 999, WATER: 999, POWER: 999, OXY: 999})
    low = gs.residents[0]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.send_expedition([low.id], {FOOD: 30, WATER: 30})
    low.morale = 5
    # 在堡两人士气 80，产出系数应按 (80+80)/2 计，而非被 5 拉低
    assert eng.avg_morale(in_bunker_only=True) == 80


# ---- 遭遇结算后的状态收敛：补给耗尽 / 全员阵亡 / 人口归零 / 终局只结算一次 ----

def test_supply_loss_that_exhausts_supplies_settles_immediately(db):
    """遭遇的物资损失把自带补给扣到 0：遭遇结算当场强制返程，不等下一次行军。

    战利品与余粮当场入库、队伍清除、档案阶段回到 daily（或 ended），
    不留下“零补给仍在外”的悬空队伍。
    """
    gs = make_session(db)
    # weather·就地躲避：自带物资食物 -3 水 -3
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="weather"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 4, WATER: 4})
    encounter = eng.advance_day()  # 行军消耗 1：剩 3/3
    supplies_before_resolve = dict(gs.expedition["supplies"])
    assert supplies_before_resolve[FOOD] == 3
    detail, replayed = eng.resolve_expedition_encounter(
        "take_shelter", token=encounter["token"]
    )
    assert replayed is False
    # 补给被遭遇扣空 → 当场收敛返程
    assert gs.expedition is None
    assert eng.phase == "daily"
    # 余粮为 0，没有任何物资被重复归还/扣减
    assert "归还物资" not in detail or "剩余食物" not in detail


def test_exhausted_supply_settlement_loot_and_survivors_counted_once(db):
    """补给耗尽当场收敛后：战利品只入库一次，继续行军/再次返程都不二次结算。"""
    from app.models import EventLog

    gs = make_session(db)
    # 2 人队：行军一天消耗 2，自带 5/5 → 剩 3/3；beast·绕道撤退食物 -6 水 -4 → 耗尽
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="beast"))
    eng.send_expedition([gs.residents[0].id, gs.residents[1].id], {FOOD: 5, WATER: 5})
    encounter = eng.advance_day()
    food_before = gs.resources[FOOD]
    detail, _ = eng.resolve_expedition_encounter("flee", token=encounter["token"])
    # flee 无战利品；余粮 0，只有士气效果，资源不增
    assert gs.resources[FOOD] == food_before
    food_after = gs.resources[FOOD]
    db.commit()
    return_logs = db.query(EventLog).filter(
        EventLog.session_id == gs.id, EventLog.title.like("探索队返程%")
    ).count()
    assert return_logs == 1
    # 队伍已清除后继续行军：只是普通的地堡推进（0.5 的随机不触发地堡危机）
    result = eng.advance_day()
    assert result is None
    assert gs.pending_crisis is None
    assert gs.expedition is None
    food_after_advance = gs.resources[FOOD]
    # 带原遭遇 token 的重复遭遇请求：回放收敛时的同一份明细，不二次扣减
    detail3, replay3 = eng.resolve_expedition_encounter(
        "flee", token=encounter["token"]
    )
    assert replay3 is True
    assert detail3 == detail
    assert gs.resources[FOOD] == food_after_advance
    # 无凭据主动返程：命中档案返程凭据安全回放
    detail4, replay4 = eng.return_expedition(token=None)
    assert replay4 is True
    assert gs.resources[FOOD] == food_after_advance


def test_encounter_kills_all_members_converges_without_zombie_team(db):
    """遭遇（全体伤害）杀死队内存活队员：当场收敛，不留无活人僵尸队伍。"""
    gs = make_session(db)
    for r in gs.residents[:2]:
        r.health = 5  # push_through 全体健康 -6 → 两人同时阵亡
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="weather"))
    eng.send_expedition([gs.residents[0].id, gs.residents[1].id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    before_survivors = gs.survivors
    detail, _ = eng.resolve_expedition_encounter(
        "push_through", token=encounter["token"]
    )
    # 全员失联，队伍当场清除；堡内仍有 1 人，游戏继续
    assert gs.expedition is None
    assert gs.status == "running"
    assert gs.survivors == before_survivors - 2
    assert eng.phase == "daily"
    assert "殉职" in detail
    # 同一遭遇连点：回放，人口不二次扣减
    detail2, replay2 = eng.resolve_expedition_encounter(
        "push_through", token=encounter["token"]
    )
    assert replay2 is True
    assert gs.survivors == before_survivors - 2


def test_encounter_kills_last_survivor_settles_endgame_once(db):
    """遭遇杀死最后一名幸存者：返程收敛与终局结算都只发生一次。"""
    from app.models import EventLog

    gs = make_session(db)
    gs.residents[1].alive = 0
    gs.residents[2].alive = 0
    gs.survivors = 1
    victim = gs.residents[0]
    victim.health = 10
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="trap"))
    eng.send_expedition([victim.id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    detail, _ = eng.resolve_expedition_encounter(
        "force_free", token=encounter["token"]
    )
    assert gs.status == "over"
    assert gs.expedition is None
    assert gs.survivors == 0
    assert gs.outcome["survivors"] == 0
    db.commit()
    end_logs = db.query(EventLog).filter(
        EventLog.session_id == gs.id, EventLog.title == "游戏结束"
    ).count()
    assert end_logs == 1
    # 终局后一切变更被拒绝；遭遇重放也不能把档案拉回 running
    with pytest.raises(BunkerEngineError):
        eng.resolve_expedition_encounter("force_free", token=encounter["token"])
    assert gs.status == "over"
    assert gs.score is not None


def test_manual_return_with_all_dead_settles_once(db):
    """主动返程前队伍已在连续遭遇中全员阵亡（堡内仍有幸存者）：
    最后一次遭遇当场收敛，人口只扣一次，随后再返程不重复结算。"""
    gs = make_session(db)
    gs.residents[0].health = 5
    gs.residents[1].health = 5
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="weather"))
    # 2 人队、补给充足；weather·冒雨前进为全体健康 -6，一次遭遇同时杀死两人
    eng.send_expedition([gs.residents[0].id, gs.residents[1].id], {FOOD: 20, WATER: 20})
    encounter = eng.advance_day()
    before = gs.survivors
    detail, replayed = eng.resolve_expedition_encounter(
        "push_through", token=encounter["token"]
    )
    assert replayed is False
    assert gs.expedition is None
    assert gs.survivors == before - 2
    assert gs.status == "running"  # 堡内还有第三人
    survivors_after = gs.survivors
    # 再用无凭据主动返程：档案已无队伍，命中最近返程记录的安全回放，不再扣减
    detail2, replay2 = eng.return_expedition(token=None)
    assert replay2 is True
    assert gs.survivors == survivors_after
    assert gs.expedition is None
    # 同一遭遇 token 的重放同样幂等
    detail3, replay3 = eng.resolve_expedition_encounter(
        "push_through", token=encounter["token"]
    )
    assert replay3 is True
    assert gs.survivors == survivors_after


def test_converging_encounter_concurrent_loser_replays(db):
    """并发两请求结算同一致命遭遇：先到者完成遭遇+返程收敛，落败方安全回放。

    落败方提交时版本号过期 → StaleDataError → reconcile 凭遭遇 token 命中
    返程凭据并回放（人口只扣一次、战利品只入库一次）。
    """
    gs = make_session(db)
    victim = gs.residents[0]
    victim.health = 5
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="weather"))
    eng.send_expedition([victim.id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    db.commit()
    sid, token = gs.id, encounter["token"]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        ga, gb = db_a.get(GameSession, sid), db_b.get(GameSession, sid)
        BunkerEngine(db_a, ga).resolve_expedition_encounter(
            "push_through", token=token
        )
        db_a.commit()
        BunkerEngine(db_b, gb).resolve_expedition_encounter(
            "push_through", token=token
        )
        from sqlalchemy.orm.exc import StaleDataError
        with pytest.raises(StaleDataError):
            db_b.commit()
        db_b.rollback()
        db_b.refresh(gb)
        # 落败方核对：凭遭遇 token 回放已完成的收敛结算（200 口径，非 409）
        replay_eng = BunkerEngine(db_b, gb)
        detail, replayed = replay_eng.reconcile_stale_expedition(
            "encounter", token=token, choice_key="push_through"
        )
        assert replayed is True
        final = db_a.get(GameSession, sid)
        assert final.expedition is None
        assert final.survivors == 2  # 只扣一次
        assert final.last_expedition["action"] == "return"
        assert final.last_expedition["token"] == token
    finally:
        db_a.close()
        db_b.close()


def test_converging_encounter_other_choice_is_conflict(db):
    """收敛完成后，用同一遭遇 token 但不同选项重试：409，不回放成别人的结算。"""
    gs = make_session(db)
    victim = gs.residents[0]
    victim.health = 10
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="trap"))
    eng.send_expedition([victim.id], {FOOD: 10, WATER: 10})
    encounter = eng.advance_day()
    eng.resolve_expedition_encounter("force_free", token=encounter["token"])
    assert gs.expedition is None
    survivors_after = gs.survivors
    # 同 token 另一选项：不得当成同一次结算
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_expedition_encounter(
            "free_carefully", token=encounter["token"]
        )
    assert gs.survivors == survivors_after


def test_return_loot_does_not_rescue_collapse(db):
    """主动返程：返程前地堡已全线枯竭，战利品入库不得把败局救回。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    gs.expedition["loot"] = {FOOD: 20, WATER: 20, POWER: 20, OXY: 20}
    gs.resources = {FOOD: 0, WATER: 0, POWER: 0, OXY: 0}
    assert eng._end_verdict() is not None
    detail, replayed = eng.return_expedition(token=gs.expedition["token"])
    assert replayed is False
    # 战利品与余粮照常入库（终局日志/资源仍反映这次返程），但状态必须是 over
    assert gs.status == "over"
    assert gs.expedition is None
    assert gs.resources[FOOD] > 0
    assert gs.outcome is not None
    # 返程凭据仍在，重复返程只回放，不二次入库
    food_after = gs.resources[FOOD]
    detail2, replay2 = eng._settle_expedition(
        {"token": gs.last_expedition["exp_token"]}, reason="重复"
    )
    assert replay2 is True
    assert gs.resources[FOOD] == food_after


def test_advance_forced_return_loot_does_not_rescue_collapse(db):
    """行军日补给耗尽强制返程：推进开始时已枯竭，当日产出/战利品都不得救回败局。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedRand(random_val=0.5, encounter_key="cache"))
    # 2 人队带 3/3：行军一天剩 1/1 并挂 cache；结算得 loot 8/6；次日行军耗尽
    eng.send_expedition([gs.residents[0].id, gs.residents[1].id], {FOOD: 3, WATER: 3})
    encounter = eng.advance_day()
    eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
    gs.resources = {FOOD: 0, WATER: 0, POWER: 0, OXY: 0}
    eng.advance_day()  # 在堡 1 人有产出，但推进开始时已枯竭
    assert gs.status == "over"
    assert gs.expedition is None
    # 战利品确实入了库（结算发生过且只发生一次），但终局裁决以推进前为准
    assert gs.resources[FOOD] >= 8
    assert gs.outcome is not None


def test_target_day_with_exhausted_team_settles_once(db):
    """终局日队伍补给耗尽：先返程结算（只一次）再胜利收敛，无僵尸队伍。"""
    from app.models import EventLog

    gs = make_session(db, resources={FOOD: 999, WATER: 999, POWER: 999, OXY: 999})
    gs.day = SURVIVAL_TARGET_DAY - 1
    eng = BunkerEngine(db, gs, rand=ScriptedRand(random_val=0.95))
    # 3 人队带 3/3：终局日行军消耗 3 恰好耗尽；0.95 > 0.85 不触发遭遇
    eng.send_expedition(
        [r.id for r in gs.residents], {FOOD: 3, WATER: 3}
    )
    result = eng.advance_day()
    assert result is None
    assert gs.status == "win"
    assert gs.expedition is None
    assert gs.survivors == 3
    db.commit()
    end_logs = db.query(EventLog).filter(
        EventLog.session_id == gs.id, EventLog.title == "游戏结束"
    ).count()
    assert end_logs == 1
    return_logs = db.query(EventLog).filter(
        EventLog.session_id == gs.id, EventLog.title.like("探索队返程%")
    ).count()
    assert return_logs == 1


def test_finish_is_idempotent(db):
    """终局结算幂等：重复 _finish/_check_end 不重算分数、不重复写结局日志。"""
    from app.models import EventLog

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    assert eng._check_end() is False
    gs.day = SURVIVAL_TARGET_DAY
    assert eng._check_end() is True
    score1, outcome1 = gs.score, dict(gs.outcome)
    assert eng._check_end() is True
    eng._finish(win=False, reason="不应覆盖")
    assert gs.status == "win"  # 首次裁决不被覆盖
    assert gs.score == score1
    assert gs.outcome == outcome1
    db.commit()
    end_logs = db.query(EventLog).filter(
        EventLog.session_id == gs.id, EventLog.title == "游戏结束"
    ).count()
    assert end_logs == 1

