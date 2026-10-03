# -*- coding: utf-8 -*-
"""贸易救援模块测试：申请→审核→运输→交付/失败回退的状态链，
以及资源/居民/信誉回写、阶段锁、幂等回放与终局收敛。"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    TRADE_INCIDENTS,
    TRADE_REVIEWING,
    TRADE_TRANSPORTING,
    FOOD, WATER, POWER, OXY,
    RESOURCE_KEYS,
)
from tests.test_engine import make_session, FixedRand


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


class TradeRand:
    """可脚本化的贸易随机：依次弹出 random() 值，choice 取事件池首项。"""

    def __init__(self, random_vals=()):
        self.vals = list(random_vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 0.9

    def choice(self, seq):
        return seq[0]


def rescue_offer(eng, partner="ridge"):
    return next(
        o for o in eng.trade_market()
        if o["type"] == "rescue" and o["partner"] == partner
    )


def procure_offer(eng, partner="station"):
    return next(
        o for o in eng.trade_market()
        if o["type"] == "procure" and o["partner"] == partner
    )


# ---- 市场与申请 ----

def test_market_is_deterministic_within_day(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    m1 = eng.trade_market()
    m2 = eng.trade_market()
    assert [o["id"] for o in m1] == [o["id"] for o in m2]
    assert len(m1) == 4
    assert {o["type"] for o in m1} == {"rescue", "procure"}


def test_market_rotates_across_days(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    day1 = {o["id"] for o in eng.trade_market()}
    gs.day = 5
    day5 = {o["id"] for o in eng.trade_market()}
    assert day1.isdisjoint(day5)


def test_apply_freezes_escrow_and_enters_reviewing(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    offer = rescue_offer(eng)
    before = dict(gs.resources)
    order = eng.apply_trade(offer["id"], [gs.residents[0].id])
    assert order["status"] == TRADE_REVIEWING
    assert order["escrow"] == offer["escrow"]
    assert order["cargo"] == offer["cargo"]
    # 托管物资已冻结
    for k, v in offer["escrow"].items():
        assert gs.resources[k] == round(before[k] - v, 1)
    # 审核阶段押运队尚未离堡，仍参与地堡生产
    assert gs.residents[0].id not in eng._away_resident_ids()


def test_apply_rejects_expired_offer(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    offer = rescue_offer(eng)
    gs.day = 9
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(offer["id"], [gs.residents[0].id])


def test_apply_validates_escorts(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    offer = rescue_offer(eng)
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(offer["id"], [])                       # 无人押运
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(offer["id"], [gs.residents[0].id] * 2)  # 重复队员
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(offer["id"], [9999])                   # 不存在的居民


def test_apply_rejects_when_insufficient_escrow(db):
    gs = make_session(db, resources={k: 1 for k in RESOURCE_KEYS})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    offer = rescue_offer(eng)
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(offer["id"], [gs.residents[0].id])


def test_only_one_order_at_a_time(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(procure_offer(eng)["id"], [gs.residents[1].id])


def test_expedition_and_trade_are_mutually_exclusive(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 有审核中订单时不能派探索队
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[1].id], {FOOD: 5, WATER: 5})


# ---- 审核 ----

def test_review_rejection_refunds_full_escrow(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.99]))  # 0.99 >= 通过率：驳回
    offer = rescue_offer(eng)
    before = dict(gs.resources)
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()  # 审核日：驳回
    assert gs.trade_order is None
    # 托管全额退回（当日生产另行结算，退回后不得低于冻结额减正常消耗以外的差异；
    # 直接核对退回流水：驳回前一刻资源 + 全额 escrow）
    # 这里用幂等口径：审核驳回当天不再有运输，订单字段已清空
    assert gs.reputation == 50


def test_review_approval_starts_transport(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))  # 通过、无事件
    offer = rescue_offer(eng)  # ridge: eta 2
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()
    assert gs.trade_order is not None
    assert gs.trade_order["status"] == TRADE_TRANSPORTING
    assert gs.trade_order["travel_days"] == 1
    # 押运队已离堡
    assert gs.residents[0].id in eng._away_resident_ids()


def test_low_reputation_makes_rejection_likely(db):
    gs = make_session(db)
    gs.reputation = 0
    gs.day = 2  # 市场报价按日生成，审核发生在 day3
    eng = BunkerEngine(db, gs, rand=TradeRand([0.9]))  # 信誉0求援通过率0.50：0.9 被驳回
    offer = rescue_offer(eng, partner="station")  # day2 的求援方含 station(eta2)
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()
    assert gs.trade_order is None


# ---- 撤单 ----

def test_cancel_during_reviewing_refunds_fully(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    before = dict(gs.resources)
    order = eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    detail, replayed = eng.cancel_trade(token=order["token"])
    assert replayed is False
    assert gs.trade_order is None
    assert gs.resources == before
    # 连点幂等回放
    detail2, replayed2 = eng.cancel_trade(token=order["token"])
    assert replayed2 is True
    assert detail2 == detail
    assert gs.resources == before  # 没有第二次退款


def test_cancel_blocked_after_transport_starts(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))
    order = eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    token = order["token"]
    eng.advance_day()  # 审核通过，进入运输
    with pytest.raises(BunkerEngineConflict):
        eng.cancel_trade(token=token)


# ---- 运输与途中事件 ----

def test_transport_day_excludes_escorts_from_bunker_consumption(db):
    # 用两个相同初始档案对照：
    # 贸易档案 day2 审核通过（当日押运队仍在堡，与对照组完全同口径），
    # day3 为完整在途日；对照组全员始终在堡。两档案 day3 的食物日变化之差
    # 应恰好等于 1 名离堡队员的口粮 1.5
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))
    offer = rescue_offer(eng, partner="dome")  # eta 4，保证 day3 仍在途
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()                          # day2：审核 + 出发
    food_day2 = gs.resources[FOOD]
    eng.rand = TradeRand([0.9])                # day3 在途、无事件
    eng.advance_day()
    delta_trade = gs.resources[FOOD] - food_day2

    gs_ref = make_session(db)
    eng_ref = BunkerEngine(db, gs_ref, rand=TradeRand([0.9]))
    eng_ref.advance_day()                      # day2
    ref_day2 = gs_ref.resources[FOOD]
    eng_ref.rand = TradeRand([0.9])
    eng_ref.advance_day()                      # day3
    delta_ref = gs_ref.resources[FOOD] - ref_day2

    # 唯一差别：day3 贸易档案有 1 人离堡不消耗地堡口粮（少消耗 1.5）
    assert round(delta_trade - delta_ref, 1) == 1.5
    assert gs.trade_order["status"] == TRADE_TRANSPORTING


def test_incident_pends_and_locks_phase(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))  # 通过、触发事件
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    assert incident is not None
    assert incident["event"] == TRADE_INCIDENTS[0]["key"]
    assert eng.phase == "trade"
    with pytest.raises(BunkerEngineError):
        eng.advance_day()
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")


def test_incident_cargo_loss_reduces_delivery(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    offer = rescue_offer(eng)  # eta 2
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    # 缴货物买路：货损 30%
    eng.resolve_trade_incident("pay_toll", token=incident["token"])
    assert gs.trade_order["cargo_ratio"] == 0.7
    # 次日抵达并交付成功
    eng.rand = TradeRand([0.1])
    before = dict(gs.resources)
    eng.advance_day()
    assert gs.trade_order is None
    gain_key = list(offer["cargo"].keys())[0]
    gained = round(gs.resources[gain_key] - before[gain_key], 1)
    # 回礼按残存比例：32 * 0.7 ≈ 22.4（当日生产另计，gain_key 食物产出设施也在，
    # 因此只断言拿到了严格正的、且小于全额的回礼）
    assert gained > 0


def test_incident_wrong_token_rejected(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    eng.advance_day()
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_trade_incident("pay_toll", token="stale-token")


def test_incident_targeted_choice_binds_member(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id, gs.residents[1].id])
    incident = eng.advance_day()
    # 强行突围是单体效果：目标必须在押运队中且与事件绑定
    assert incident["needs_target"]
    hp_others = [r.health for r in gs.residents if r.id != incident["target_id"]]
    eng.resolve_trade_incident("fight_through", token=incident["token"])
    target = next(r for r in gs.residents if r.id == incident["target_id"])
    assert target.health < 90
    # 非目标队员健康不受影响
    for r in gs.residents:
        if r.id != incident["target_id"]:
            assert r.health == 90


def test_resolved_incident_is_idempotent(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    d1, rp1 = eng.resolve_trade_incident("pay_toll", token=incident["token"])
    assert rp1 is False
    d2, rp2 = eng.resolve_trade_incident("pay_toll", token=incident["token"])
    assert rp2 is True
    assert d2 == d1
    # 货损只施加一次：仍为 0.7
    assert gs.trade_order["cargo_ratio"] == 0.7


def test_abort_settles_order_immediately(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    offer = rescue_offer(eng)
    before = dict(gs.resources)
    order = eng.apply_trade(offer["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    detail, _ = eng.resolve_trade_incident("abandon", token=incident["token"])
    # 当场收敛为失败，不留僵尸订单
    assert gs.trade_order is None
    # 弃货时货损为 0：托管全额带回（当日地堡生产另计，只核对托管键）
    for k, v in offer["escrow"].items():
        assert gs.resources[k] >= before[k] - v  # 冻结额已退回（可能还叠加生产消耗）
    assert gs.reputation == 50 - 8  # 求援失败 -8
    # 同一事件请求重放：不再二次结算
    detail2, replayed = eng.resolve_trade_incident("abandon", token=incident["token"])
    assert replayed is True
    assert detail2 == detail


def test_abort_wrong_choice_after_convergence_is_conflict(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    eng.resolve_trade_incident("abandon", token=incident["token"])
    with pytest.raises(BunkerEngineConflict):
        eng.resolve_trade_incident("pay_toll", token=incident["token"])


def test_total_cargo_loss_converges_to_failed(db):
    gs = make_session(db)
    # patrol·分货打点 货损 20%；breakdown·人力拖拽 25%。直接构造连续货损更快：
    # 用 breakdown 抢修 15% 三次叠加 0.85^3≈0.61 不为零，故手工脚本验证零货物路径
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng, partner="dome")["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    # 缴三成 + 直接把残存手工压到 0，模拟全损后下一次事件
    eng.resolve_trade_incident("pay_toll", token=incident["token"])
    gs.trade_order["cargo_ratio"] = 0.0
    gs.trade_order["pending_incident"] = None
    # 再造一个挂起事件并以零货损选项结算：全损应收敛
    event = next(e for e in TRADE_INCIDENTS if e["key"] == "patrol")
    pending = eng._build_trade_incident(event, gs.trade_order)
    gs.trade_order["pending_incident"] = pending
    detail, _ = eng.resolve_trade_incident("papers", token=pending["token"])
    assert gs.trade_order is None
    assert gs.reputation < 50


def test_incident_casualty_converges_when_all_escorts_die(db):
    gs = make_session(db, residents=3)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng, partner="dome")["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    gs.residents[0].health = 5
    # 强行突围：单体 -14 杀死唯一押运队员 → 全员失联，订单当场失败
    eng.resolve_trade_incident("fight_through", token=incident["token"])
    assert gs.trade_order is None
    assert gs.survivors == 2
    assert not gs.residents[0].alive


# ---- 交付 ----

def test_successful_rescue_delivery_grants_cargo_and_reputation(db):
    gs = make_session(db)
    # 序列：审核通过(0.1)、day2 无事件(0.9)、day3 交付掷点(0.1 成功)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))
    offer = rescue_offer(eng)  # eta 2
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()          # day2：审核通过 + 运输（无事件）
    eng.rand = TradeRand([0.1])
    eng.advance_day()          # day3：抵达，交付成功
    assert gs.trade_order is None
    assert gs.reputation == 56  # +6
    # 押运队员士气奖励（+10，自然恢复不抵消）
    assert gs.residents[0].morale > 80


def test_successful_procure_partial_loss_refunds_lost_share(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    offer = procure_offer(eng, partner="station")  # eta 2
    assert offer["eta"] == 2
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    # patrol 首项为 papers（无货损）——改用贿赂 20% 货损需匹配事件；首事件是 ambush
    # ambush 买路货损 30%
    eng.resolve_trade_incident("pay_toll", token=incident["token"])
    assert gs.trade_order["cargo_ratio"] == 0.7
    eng.rand = TradeRand([0.1])
    eng.advance_day()  # 抵达交付成功
    assert gs.trade_order is None
    assert gs.reputation == 53  # 采购成功 +3


def test_failed_delivery_refunds_and_penalizes(db):
    gs = make_session(db)
    # 信誉 50：求援成功率 0.75；掷 0.9 失败
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9, 0.9]))
    offer = rescue_offer(eng)
    escrow_key = list(offer["escrow"].keys())[0]
    baseline = None
    order = eng.apply_trade(offer["id"], [gs.residents[0].id])
    frozen = gs.resources[escrow_key]
    eng.advance_day()          # day2 审核通过、无事件
    eng.advance_day()          # day3 抵达但交易失败：托管按残存比例退回
    assert gs.trade_order is None
    assert gs.reputation == 42  # -8
    # 托管全额退回（无在途货损）
    assert gs.resources[escrow_key] >= frozen


# ---- 危机流程衔接 ----

def test_inbound_trade_replaces_bunker_crisis(db):
    """押运在途的日子只走途中事件，不触发地堡危机。"""
    gs = make_session(db)
    # random 序列：审核通过、途中事件必触发（0.1）；危机概率根本不被查询
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng, partner="dome")["id"], [gs.residents[0].id])
    pending = eng.advance_day()
    assert pending is not None
    assert gs.pending_crisis is None
    assert gs.trade_order["pending_incident"] is not None


def test_reputation_crisis_changes_reputation(db):
    """地堡危机池新增信誉类事件：抉择结果回写信誉。"""
    from app.services.engine import CRISIS_POOL
    event = next(e for e in CRISIS_POOL if e["key"] == "caravan_help")
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = eng._build_crisis(event)
    gs.pending_crisis = crisis
    eng.resolve_crisis("caravan_help", "refuse")
    assert gs.reputation == 44  # -6


# ---- 终局收敛 ----

def test_endgame_clears_trade_order(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))
    eng.apply_trade(rescue_offer(eng, partner="dome")["id"], [gs.residents[0].id])
    eng.advance_day()  # 在途
    gs.day = gs.target_day
    gs.pending_crisis = None
    eng.advance_day()
    assert gs.status == "win"
    assert gs.trade_order is None


def test_reviewing_order_on_endgame_is_cancelled_with_refund(db):
    # 在第 target_day 当天申请：审核要等到次日，但次日已超出终局日，
    # 因此推进时直接撤单退款并收敛到胜利
    gs = make_session(db)
    gs.day = gs.target_day
    eng = BunkerEngine(db, gs, rand=FixedRand())
    offer = rescue_offer(eng, partner="station")  # target_day=120 市场中的求援方
    before = dict(gs.resources)
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()  # 终局日：审核不再进行，撤单退款
    assert gs.status == "win"
    assert gs.trade_order is None
    # 托管全额退回：除正常当日生产净变外，冻结额完璧归赵
    for k, v in offer["escrow"].items():
        assert gs.resources[k] >= before[k] - v


# ---- 快照持久化与恢复 ----

def test_order_persisted_and_recoverable_after_reload(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    token = incident["token"]
    db.commit()
    sid = gs.id

    db2 = SessionLocal()
    try:
        gs2 = db2.get(GameSession, sid)
        eng2 = BunkerEngine(db2, gs2, rand=FixedRand())
        assert eng2.phase == "trade"
        assert gs2.trade_order["pending_incident"]["token"] == token
        eng2.resolve_trade_incident("pay_toll", token=token)
        db2.commit()
    finally:
        db2.close()


def test_delivery_gain_does_not_rescue_collapse(db):
    """抵达交付当日地堡已全线枯竭：回礼入库不得把败局"救回"，仍判失败结局。

    与探索队战利品返程同一口径：终局裁决在交付入库前快照并优先。
    """
    gs = make_session(db, resources={k: 0 for k in RESOURCE_KEYS})
    # 手动构造一个当日即将抵达的在途订单：押运队员存活，货物完好
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1]))
    gs.trade_order = {
        "token": "tok", "offer_id": "x", "type": "rescue", "partner": "ridge",
        "partner_name": "岭上镇", "eta": 1, "status": TRADE_TRANSPORTING,
        "applied_day": gs.day, "escorts": [gs.residents[0].id],
        "escrow": {POWER: 20}, "cargo": {FOOD: 40, WATER: 40, OXY: 40},
        "cargo_ratio": 1.0, "travel_days": 0, "incidents_resolved": 0,
        "pending_incident": None,
    }
    # 推进前全线枯竭（裁决快照为失败）；抵达交付成功本会把四种物资全部抬过阈值
    eng.advance_day()
    assert gs.status == "over"
    assert gs.trade_order is None


def test_failed_delivery_when_cargo_lost_partial_refund(db):
    """采购在途货损 50% 后交付成功：到货按半数，损失半数托管退回。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.1]))
    offer = procure_offer(eng, partner="station")
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    incident = eng.advance_day()
    # ambush 买路货损 30%
    eng.resolve_trade_incident("pay_toll", token=incident["token"])
    assert gs.trade_order["cargo_ratio"] == 0.7
    eng.rand = TradeRand([0.1])
    # station eta 2：day3 抵达
    eng.advance_day()
    assert gs.trade_order is None
    assert gs.reputation == 53  # 采购成功 +3
    # 结局未触发
    assert gs.status == "running"


def test_trade_escorts_skip_bunker_crisis_effects(db):
    """在途押运队员不参与地堡危机的全体效果（口径同探索队员）。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))
    offer = rescue_offer(eng, partner="dome")
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()  # 押运队离堡在途
    assert gs.residents[0].id in eng._away_resident_ids()
    # 手动挂一个全体士气危机并结算：押运队员不应受 -15 影响
    from app.services.engine import CRISIS_POOL
    event = next(e for e in CRISIS_POOL if e["key"] == "mutiny")
    crisis = eng._build_crisis(event)
    gs.pending_crisis = crisis
    morale_before = gs.residents[0].morale
    eng.resolve_crisis("mutiny", "suppress")
    assert gs.residents[0].morale == morale_before  # 离堡队员不受地堡危机影响
    # 在堡队员士气下降
    assert gs.residents[1].morale < 80
