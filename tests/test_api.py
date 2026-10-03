# -*- coding: utf-8 -*-
"""HTTP 层端到端测试：每日推进 / 危机 / 探索队遭遇 / 返程的前后端一致性与并发回放。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import Base, engine, SessionLocal
from app.models import GameSession
from app.services.engine import (
    BunkerEngine,
    FOOD, WATER, POWER, OXY,
)
from tests.test_engine import make_session, ScriptedRand, FixedRand, TriggerRand  # noqa: F401


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def _seed(fn):
    """在独立 DB 会话里布置初始状态并提交，返回会话 id。"""
    db = SessionLocal()
    try:
        gs = make_session(db)
        eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
        ret = fn(db, gs, eng)
        db.commit()
        sid = gs.id
        return (sid,) + ((ret,) if ret is not None else ())
    finally:
        db.close()


def test_full_crisis_cycle(client):
    """挂起危机 → API 结算 200 → 危机清除。"""
    def setup(db, gs, eng):
        c = gs.pending_crisis = {
            "token": "tok-1", "event": "mutiny", "day": gs.day,
            "title": "t", "desc": "d", "needs_target": False,
            "target_id": None, "target_name": None,
            "choices": [{"key": "suppress", "label": "镇压", "hint": "", "targeted": False}],
        }
        return c
    sid, crisis = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/resolve", json={
        "event_key": "mutiny", "choice_key": "suppress", "target_id": None,
        "token": "tok-1",
    })
    assert r.status_code == 200
    assert r.json()["pending_crisis"] is None


def test_advance_blocked_while_crisis_pending(client):
    """危机待处理时推进一天 → 400，日期不前进。"""
    def setup(db, gs, eng):
        gs.pending_crisis = {"token": "t", "event": "mutiny", "choices": []}
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 400
    assert client.get(f"/api/sessions/{sid}").json()["day"] == 1


def test_expedition_full_flow_and_away_flag(client):
    """API 派遣 → 引擎行军挂遭遇 → API 结算遭遇 → API 返程；成员 away 标记前后一致。"""
    # 1) API 派遣
    r = client.post("/api/sessions", json={"name": "e2e"})
    sid = r.json()["id"]
    members = r.json()["residents"]
    mid = members[0]["id"]
    r = client.post(f"/api/sessions/{sid}/expedition/send", json={
        "member_ids": [mid], "supplies": {"food": 10, "water": 10},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["expedition"]["status"] == "away"
    # 离堡成员 away=1，在堡成员 away=0
    flags = {x["id"]: x["away"] for x in body["residents"]}
    assert flags[mid] == 1
    assert all(v == 0 for k, v in flags.items() if k != mid)
    team_token = body["expedition"]["token"]

    # 2) 直接用 API 推进一天（遭遇必然触发：随机由脚本固定，这里改走真实推进的前置布置）
    #    通过引擎在独立会话挂起遭遇，再用 API 验证推进响应里的 pending_event 别名
    db = SessionLocal()
    try:
        gs = db.get(GameSession, sid)
        enc0 = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache")).advance_day()
        assert enc0["event"] == "cache"
        db.commit()
    finally:
        db.close()
    state = client.get(f"/api/sessions/{sid}").json()
    assert state["expedition"]["pending_encounter"]["event"] == "cache"
    enc_token = state["expedition"]["pending_encounter"]["token"]

    # 3) API 结算遭遇
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "search_carefully", "token": enc_token,
    })
    assert r.status_code == 200
    assert r.json()["expedition"]["pending_encounter"] is None

    # 4) API 返程
    r = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": team_token})
    assert r.status_code == 200
    assert r.json()["expedition"] is None
    # 成员归队
    assert all(x["away"] == 0 for x in r.json()["residents"])


def test_advance_response_pending_event_aliases_crisis(client, monkeypatch):
    """真实推进挂起遭遇时：响应里 pending_event 与兼容别名 crisis 同值。"""
    from app.services import engine as engine_mod

    class Scripted(ScriptedRand):
        pass

    # 让真实请求里的引擎也使用确定性随机（遭遇必触发）
    monkeypatch.setattr(engine_mod, "_rng", lambda: Scripted(encounter_key="cache"))

    r = client.post("/api/sessions", json={"name": "alias"})
    sid = r.json()["id"]
    mid = r.json()["residents"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/expedition/send", json={
        "member_ids": [mid], "supplies": {"food": 20, "water": 20},
    })
    assert r.status_code == 200
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    body = r.json()
    assert body["pending_event"] is not None
    assert body["pending_event"]["event"] == "cache"
    # 兼容旧前端的 crisis 字段同值
    assert body["crisis"] == body["pending_event"]


def test_build_locked_during_pending_encounter(client):
    """探索遭遇挂起时建造设施 → 400（后端阶段守卫，不依赖前端禁用）。"""
    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        eng.advance_day()  # 挂起 cache 遭遇
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/build", json={"category": "med"})
    assert r.status_code == 400


def test_concurrent_crisis_resolve_loser_replays_200(client):
    """对方已结算同一危机：落败方重放同抉择 → 200 幂等回放，食物只扣一次。"""
    def setup(db, gs, eng):
        gs.pending_crisis = {
            "token": "tok", "event": "mutiny", "day": gs.day,
            "title": "", "desc": "", "needs_target": False,
            "target_id": None, "target_name": None,
            "choices": [{"key": "double_ration", "label": "", "hint": "", "targeted": False}],
        }
        # 对家先完成结算（食物 -20）并提交，留下 last_resolution
        eng.resolve_crisis("mutiny", "double_ration", token="tok")
        assert gs.resources[FOOD] == 280
    (sid,) = _seed(setup)
    # 落败方带相同负载重试
    r = client.post(f"/api/sessions/{sid}/resolve", json={
        "event_key": "mutiny", "choice_key": "double_ration",
        "target_id": None, "token": "tok",
    })
    assert r.status_code == 200
    assert r.json()["resources"]["food"] == 280  # 没有第二次扣减


def test_concurrent_expedition_return_loser_replays_200(client):
    """对方已完成返程：落败方带同一队伍 token 重试 → 200 回放，战利品只入库一次。"""
    team = {}

    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
        encounter = eng.advance_day()
        eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
        team["token"] = gs.expedition["token"]
        detail, replayed = eng.return_expedition(token=gs.expedition["token"])
        assert replayed is False
    (sid,) = _seed(setup)
    food_after = client.get(f"/api/sessions/{sid}").json()["resources"]["food"]
    r = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": team["token"]})
    assert r.status_code == 200
    assert r.json()["expedition"] is None
    assert r.json()["resources"]["food"] == food_after  # 战利品未二次入库


def test_stale_encounter_token_rejected_409(client):
    """过期遭遇 token → 409，遭遇仍在档待正确处理。"""
    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        eng.advance_day()
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "search_carefully", "token": "stale",
    })
    assert r.status_code == 409
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["expedition"]["pending_encounter"] is not None


def test_expedition_send_requires_daily_phase(client):
    """遭遇挂起时派遣第二支队伍 → 400（状态机只允许 daily 阶段派遣）。"""
    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        eng.advance_day()
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/send", json={
        "member_ids": [1], "supplies": {},
    })
    assert r.status_code == 400


def test_encounter_supply_exhaustion_converges_via_api(client):
    """遭遇把补给扣空：API 结算遭遇即完成返程收敛，会话回到无队伍状态。"""
    team = {}

    def setup(db, gs, eng):
        # weather·就地躲避：食物 -3 水 -3；1 人队带 4/4，行军消耗 1 后剩 3/3
        eng.rand = ScriptedRand(encounter_key="weather")
        eng.send_expedition([gs.residents[0].id], {FOOD: 4, WATER: 4})
        enc = eng.advance_day()
        team["token"] = enc["token"]
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "take_shelter", "token": team["token"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["expedition"] is None            # 当场收敛，无零补给僵尸队伍
    assert body["status"] == "running"
    assert all(x["away"] == 0 for x in body["residents"])
    # 同一遭遇请求重试：幂等回放 200，资源/人口不再变化
    food = body["resources"]["food"]
    survivors = body["survivors"]
    r2 = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "take_shelter", "token": team["token"],
    })
    assert r2.status_code == 200
    assert r2.json()["resources"]["food"] == food
    assert r2.json()["survivors"] == survivors


def test_concurrent_fatal_encounter_loser_replays_200(client):
    """致命遭遇已被另一请求结算并收敛：落败方凭遭遇 token 重试 → 200 回放。"""
    team = {}

    def setup(db, gs, eng):
        gs.residents[0].health = 5
        eng.rand = ScriptedRand(encounter_key="weather")
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        enc = eng.advance_day()
        team["token"] = enc["token"]
        # 对家先结算：全体健康 -6 杀死单人队 → 遭遇 + 返程收敛一次完成
        detail, replayed = eng.resolve_expedition_encounter(
            "push_through", token=enc["token"]
        )
        assert replayed is False
        assert gs.expedition is None
        assert gs.survivors == 2
    (sid,) = _seed(setup)
    survivors_after = client.get(f"/api/sessions/{sid}").json()["survivors"]
    assert survivors_after == 2
    # 落败方带相同负载重试
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "push_through", "token": team["token"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["expedition"] is None
    assert body["survivors"] == 2  # 人口没有第二次扣减


def test_fatal_encounter_wrong_choice_after_convergence_409(client):
    """收敛完成后用同 token 另一选项重试 → 409，不能回放成别人的结算。"""
    team = {}

    def setup(db, gs, eng):
        gs.residents[0].health = 5
        eng.rand = ScriptedRand(encounter_key="weather")
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        enc = eng.advance_day()
        team["token"] = enc["token"]
        eng.resolve_expedition_encounter("push_through", token=enc["token"])
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "take_shelter", "token": team["token"],
    })
    assert r.status_code == 409


# ---- 贸易救援 HTTP 端到端 ----

def test_trade_market_endpoint(client):
    """市场只读端点：返回当日报价与信誉。"""
    r = client.post("/api/sessions", json={"name": "trade"})
    sid = r.json()["id"]
    r = client.get(f"/api/sessions/{sid}/trade/market")
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == 1
    assert body["reputation"] == 50
    assert len(body["offers"]) == 4


def test_trade_full_success_flow(client, monkeypatch):
    """申请→审核→在途(无事件)→交付成功：状态链经 HTTP 完整走通。"""
    from app.services import engine as engine_mod

    # 每个 HTTP 请求都会新建引擎并重新调用 _rng()，因此脚本必须是共享单例。
    # 第一个 advance 实际消耗三个值：审核通过 0.1、在途事件掷点 0.1（必触发）、
    # 事件选择 0.9（不挂起的处理不需要——这里直接选 eta=2 且让事件掷点落空）。
    # 为稳妥给足：审核 0.1、首日无事件 0.9、抵达交付成功 0.1
    state = {"vals": [0.1, 0.9, 0.9, 0.1, 0.9, 0.9]}

    class ScriptedMarket:
        def random(self):
            return state["vals"].pop(0) if state["vals"] else 0.9

        def choice(self, seq_):
            return seq_[0]

    _scripted = ScriptedMarket()
    monkeypatch.setattr(engine_mod, "_rng", lambda: _scripted)

    r = client.post("/api/sessions", json={"name": "trade"})
    sid = r.json()["id"]
    escort = r.json()["residents"][0]["id"]
    market = client.get(f"/api/sessions/{sid}/trade/market").json()
    # 岭上镇求援：eta 2，day2 出发、day3 抵达
    offer = next(
        o for o in market["offers"]
        if o["type"] == "rescue" and o["partner"] == "ridge"
    )

    # 申请
    r = client.post(f"/api/sessions/{sid}/trade/apply", json={
        "offer_id": offer["id"], "escort_ids": [escort],
    })
    assert r.status_code == 200
    order = r.json()["trade_order"]
    assert order["status"] == "reviewing"
    # 审核中押运队员仍在堡：away=0
    assert r.json()["residents"][0]["away"] == 0
    assert r.json()["residents"][0]["trade_status"] == "reviewing"

    # day2：审核通过 + 第一个在途日
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    body = r.json()["session"]
    assert body["trade_order"]["status"] == "transporting"
    assert body["residents"][0]["away"] == 1
    assert body["trade_order"]["travel_days"] == 1

    # day3：抵达交付成功
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    body = r.json()["session"]
    assert body["trade_order"] is None
    assert body["reputation"] == 56


def test_trade_incident_http_cycle(client, monkeypatch):
    """在途事件经 HTTP 挂起→结算：pending_event 带回事件，resolve 后清除。"""
    from app.services import engine as engine_mod

    vals = [0.1, 0.1, 0.9, 0.9]  # 审核通过、触发途中事件（富余值防止耗尽）

    class Scripted:
        def random(self):
            return vals.pop(0) if vals else 0.9

        def choice(self, seq_):
            return seq_[0]

    _scripted = Scripted()
    monkeypatch.setattr(engine_mod, "_rng", lambda: _scripted)

    r = client.post("/api/sessions", json={"name": "trade2"})
    sid = r.json()["id"]
    escort = r.json()["residents"][0]["id"]
    market = client.get(f"/api/sessions/{sid}/trade/market").json()
    offer = next(o for o in market["offers"] if o["type"] == "rescue")
    client.post(f"/api/sessions/{sid}/trade/apply", json={
        "offer_id": offer["id"], "escort_ids": [escort],
    })
    r = client.post(f"/api/sessions/{sid}/advance")
    body = r.json()
    assert body["pending_event"] is not None
    assert body["session"]["trade_order"]["pending_incident"]["event"] == body["pending_event"]["event"]
    token = body["pending_event"]["token"]

    # 事件待处理期间经营动作被后端拒绝
    r = client.post(f"/api/sessions/{sid}/build", json={"category": "med"})
    assert r.status_code == 400

    r = client.post(f"/api/sessions/{sid}/trade/resolve", json={
        "choice_key": "pay_toll", "token": token,
    })
    assert r.status_code == 200
    assert r.json()["trade_order"]["cargo_ratio"] == 0.7
    assert r.json()["trade_order"]["pending_incident"] is None


def test_trade_cancel_http_refund(client):
    """审核阶段撤单：托管退还，订单清除；重复撤单 200 幂等。"""
    r = client.post("/api/sessions", json={"name": "trade3"})
    sid = r.json()["id"]
    escort = r.json()["residents"][0]["id"]
    market = client.get(f"/api/sessions/{sid}/trade/market").json()
    offer = market["offers"][0]
    before = client.get(f"/api/sessions/{sid}").json()["resources"]
    client.post(f"/api/sessions/{sid}/trade/apply", json={
        "offer_id": offer["id"], "escort_ids": [escort],
    })
    frozen = client.get(f"/api/sessions/{sid}").json()
    token = frozen["trade_order"]["token"]
    r = client.post(f"/api/sessions/{sid}/trade/cancel", json={"token": token})
    assert r.status_code == 200
    assert r.json()["trade_order"] is None
    assert r.json()["resources"] == before
    # 重复撤单：幂等回放，不二次退款
    r2 = client.post(f"/api/sessions/{sid}/trade/cancel", json={"token": token})
    assert r2.status_code == 200
    assert r2.json()["resources"] == before


def test_trade_apply_rejects_expired_offer_http(client):
    """跨天后用旧报价下单 → 400。"""
    r = client.post("/api/sessions", json={"name": "trade4"})
    sid = r.json()["id"]
    escort = r.json()["residents"][0]["id"]
    market = client.get(f"/api/sessions/{sid}/trade/market").json()
    old_id = market["offers"][0]["id"]
    # 用引擎直接推进一天（旧报价随市场轮换失效）
    db = SessionLocal()
    try:
        gs = db.get(GameSession, sid)
        BunkerEngine(db, gs).advance_day()
        db.commit()
    finally:
        db.close()
    r = client.post(f"/api/sessions/{sid}/trade/apply", json={
        "offer_id": old_id, "escort_ids": [escort],
    })
    assert r.status_code == 400


def test_trade_concurrent_incident_loser_replays_200(client, monkeypatch):
    """途中事件并发落败：对家先结算（弃货收敛），落败方同请求重试 → 200 回放。"""
    from app.services import engine as engine_mod

    class Scripted:
        def random(self):
            return 0.1  # 审核通过、事件触发

        def choice(self, seq_):
            return seq_[0]

    _scripted = Scripted()
    monkeypatch.setattr(engine_mod, "_rng", lambda: _scripted)

    r = client.post("/api/sessions", json={"name": "trade5"})
    sid = r.json()["id"]
    escort = r.json()["residents"][0]["id"]
    market = client.get(f"/api/sessions/{sid}/trade/market").json()
    offer = next(o for o in market["offers"] if o["type"] == "rescue")
    client.post(f"/api/sessions/{sid}/trade/apply", json={
        "offer_id": offer["id"], "escort_ids": [escort],
    })
    body = client.post(f"/api/sessions/{sid}/advance").json()
    token = body["pending_event"]["token"]

    # 对家先弃货收敛
    db = SessionLocal()
    try:
        gs = db.get(GameSession, sid)
        eng = BunkerEngine(db, gs)
        eng.resolve_trade_incident("abandon", token=token)
        db.commit()
    finally:
        db.close()

    # 落败方同请求重试：200 回放，信誉只扣一次（42）
    r = client.post(f"/api/sessions/{sid}/trade/resolve", json={
        "choice_key": "abandon", "token": token,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["trade_order"] is None
    assert body["reputation"] == 42


# ---- 医疗救治中心 HTTP 端到端 ----

def test_medical_register_and_admit_flow(client):
    """建中心 → 登记病例 → 收治治疗，序列化字段随档案正确下发。"""
    r = client.post("/api/sessions", json={"name": "med-e2e"})
    sid = r.json()["id"]
    residents = r.json()["residents"]
    rid = residents[0]["id"]

    # 未建中心：登记 400
    db = SessionLocal()
    try:
        gs = db.get(GameSession, sid)
        gs.residents[0].health = 40
        db.commit()
    finally:
        db.close()
    r = client.post(f"/api/sessions/{sid}/medical/register/{rid}", json={"infectious": True})
    assert r.status_code == 400

    # 建造医疗救治中心
    r = client.post(f"/api/sessions/{sid}/build", json={"category": "clinic"})
    assert r.status_code == 200
    body = r.json()
    assert body["medical_summary"]["has_clinic"] is True
    assert body["medical_summary"]["bed_capacity"] == 2

    # 登记传染病例
    r = client.post(f"/api/sessions/{sid}/medical/register/{rid}", json={"infectious": True})
    assert r.status_code == 200
    body = r.json()
    me = next(x for x in body["residents"] if x["id"] == rid)
    assert me["case_status"] == "registered"
    assert me["case_infectious"] == 1
    assert body["medical_summary"]["registered"] == 1
    assert len(body["medical_cases"]) == 1

    # 重复登记：400
    r = client.post(f"/api/sessions/{sid}/medical/register/{rid}", json={"infectious": False})
    assert r.status_code == 400

    # 收治隔离
    r = client.post(f"/api/sessions/{sid}/medical/admit/{rid}", json={"mode": "isolated"})
    assert r.status_code == 200
    body = r.json()
    me = next(x for x in body["residents"] if x["id"] == rid)
    assert me["case_status"] == "isolated"
    assert body["medical_summary"]["isolated"] == 1
    assert body["medical_summary"]["beds_used"] == 1

    # 非法收治方式：422（Pydantic 只校验字符串，引擎校验模式）→ 400
    r = client.post(f"/api/sessions/{sid}/medical/admit/{rid}", json={"mode": "freezer"})
    assert r.status_code == 400


def test_medical_register_requires_low_health(client):
    """健康良好的居民登记病例：400。"""
    r = client.post("/api/sessions", json={"name": "med-health"})
    sid = r.json()["id"]
    rid = r.json()["residents"][0]["id"]
    client.post(f"/api/sessions/{sid}/build", json={"category": "clinic"})
    r = client.post(f"/api/sessions/{sid}/medical/register/{rid}", json={"infectious": False})
    assert r.status_code == 400


def test_medical_blocked_when_crisis_pending(client):
    """危机待处理时登记/收治：400。"""
    def setup(db, gs, eng):
        gs.pending_crisis = {"token": "t", "event": "mutiny", "choices": []}
    (sid,) = _seed(setup)
    rid = client.get(f"/api/sessions/{sid}").json()["residents"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/medical/register/{rid}", json={"infectious": False})
    assert r.status_code == 400


def test_medic_job_assignable_via_api(client):
    """医护岗位可通过调岗接口下发。"""
    r = client.post("/api/sessions", json={"name": "medic-job"})
    sid = r.json()["id"]
    rid = r.json()["residents"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/resident/{rid}/job", json={"job": "medic"})
    assert r.status_code == 200
    me = next(x for x in r.json()["residents"] if x["id"] == rid)
    assert me["job"] == "medic"
    assert me["job_zh"] == "医护"


def test_clinic_in_buildings_and_endpoint(client):
    """可建造列表含医疗救治中心。"""
    rows = client.get("/api/buildings").json()
    cats = {b["category"]: b for b in rows}
    assert "clinic" in cats
    assert cats["clinic"]["name"] == "医疗救治中心"
