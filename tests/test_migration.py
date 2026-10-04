# -*- coding: utf-8 -*-
"""旧存档兼容迁移测试：补列 + 历史快照归一化（reconcile_old_saves）。"""
import json

import pytest
from sqlalchemy import text

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession, Resident, Facility
from app.core.migration import ensure_schema, reconcile_old_saves
from app.services.engine import BunkerEngine
from app.services.engine import FOOD, WATER, POWER, OXY
from tests.test_engine import make_session, FixedRand, TriggerRand  # noqa: F401


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def test_backfills_last_expedition_column(db):
    """缺列旧表经 ensure_schema 后补齐 last_expedition，旧行为 NULL。"""
    gs = make_session(db)
    db.commit()
    db.expire_all()
    db.execute(text("ALTER TABLE game_sessions RENAME TO gs_old"))
    db.execute(text(
        "CREATE TABLE game_sessions ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
        "status VARCHAR(16), resources JSON, survivors INTEGER, outcome JSON, "
        "score INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    db.execute(text(
        "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
        "survivors,outcome,score,created_at,updated_at FROM gs_old"
    ))
    db.execute(text("DROP TABLE gs_old"))
    db.commit()

    ensure_schema(engine)
    db.expire_all()
    row = db.query(GameSession).first()
    assert row.last_expedition is None
    assert row.pending_crisis is None


def test_running_save_with_corrupt_crisis_is_unblocked(db):
    """运行中档案挂着结构损坏/目标失踪的危机：归一化后清空，回到可推进的每日阶段。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    # 目标居民编号不存在于任何档案
    db.execute(
        text("UPDATE game_sessions SET pending_crisis = :c WHERE id = :sid"),
        {
            "c": json.dumps({"event": "sick", "choices": [{"key": "x"}], "target_id": 999999}),
            "sid": sid,
        },
    )
    db.commit()

    n = reconcile_old_saves(engine)
    assert n == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.pending_crisis is None
    eng = BunkerEngine(db, fixed, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()  # 不再被损坏快照死锁
    db.commit()


def test_ended_save_keeps_no_hanging_snapshots(db):
    """已结束档案即便残留危机/探索队快照，归一化后也一律收敛到 ended。"""
    gs = make_session(db)
    gs.status = "over"
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET pending_crisis = :c, expedition = :e WHERE id = :sid"),
        {
            "c": json.dumps({"event": "mutiny", "choices": [{"key": "x"}]}),
            "e": json.dumps({"status": "away", "members": [], "token": "t"}),
            "sid": sid,
        },
    )
    db.commit()

    reconcile_old_saves(engine)
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.pending_crisis is None
    assert fixed.expedition is None
    assert BunkerEngine(db, fixed).phase == "ended"


def test_expedition_with_all_missing_members_is_cleared(db):
    """探索队成员全部不在档（悬空快照）：整队清除，档案可继续推进。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET expedition = :e WHERE id = :sid"),
        {"e": json.dumps({"status": "away", "members": [424242, 525252], "token": "t"}),
         "sid": sid},
    )
    db.commit()

    reconcile_old_saves(engine)
    db.expire_all()
    assert db.get(GameSession, sid).expedition is None


def test_expedition_partial_dangling_member_is_pruned(db):
    """队伍中夹杂一个已删除编号：剔除悬空编号，保留有效成员与待处理遭遇。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    exp = {
        "status": "away",
        "token": "team-token",
        "members": [valid, 999999],
        "travel_days": 1,
        "pending_encounter": {"token": "enc", "event": "cache", "target_id": None},
    }
    db.execute(
        text("UPDATE game_sessions SET expedition = :e WHERE id = :sid"),
        {"e": json.dumps(exp), "sid": sid},
    )
    db.commit()

    reconcile_old_saves(engine)
    db.expire_all()
    fixed = db.get(GameSession, sid).expedition
    assert fixed["members"] == [valid]
    assert fixed["pending_encounter"]["token"] == "enc"


def test_survivors_count_drift_reconciled(db):
    """survivors 与实际存活人数漂移（旧 bug）：以居民表为准校正。"""
    gs = make_session(db)
    gs.survivors = 9  # 实际只有 3 名存活居民
    db.commit()
    sid = gs.id

    reconcile_old_saves(engine)
    db.expire_all()
    assert db.get(GameSession, sid).survivors == 3


def test_valid_running_save_left_untouched(db):
    """结构完好、目标在档的运行中快照不被误清理。"""
    gs = make_session(db)
    target = gs.residents[0].id
    crisis = {"event": "sick", "choices": [{"key": "quarantine"}], "target_id": target}
    gs.pending_crisis = crisis
    db.commit()
    sid = gs.id

    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    assert db.get(GameSession, sid).pending_crisis["target_id"] == target


def test_reconcile_is_idempotent_no_rewrite(db):
    """重复执行归一化：第二次零更新，且不 bump 乐观锁版本号。"""
    gs = make_session(db)
    gs.survivors = 7  # 制造漂移，第一次需要校正
    db.commit()
    sid = gs.id

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    version_after_first = db.get(GameSession, sid).row_version

    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.survivors == 3
    assert fixed.row_version == version_after_first  # 幂等运行不产生额外写入


# ---- 贸易订单快照归一化 ----

def _write_trade(sid, order):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE game_sessions SET trade_order = :o WHERE id = :sid"),
            {"o": json.dumps(order, ensure_ascii=False), "sid": sid},
        )


def test_trade_backfills_new_columns(db):
    """缺列旧表经 ensure_schema 后补齐 trade_order/last_trade/reputation。"""
    gs = make_session(db)
    db.commit()
    db.expire_all()
    db.execute(text("ALTER TABLE game_sessions RENAME TO gs_old"))
    db.execute(text(
        "CREATE TABLE game_sessions ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
        "status VARCHAR(16), resources JSON, survivors INTEGER, outcome JSON, "
        "score INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    db.execute(text(
        "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
        "survivors,outcome,score,created_at,updated_at FROM gs_old"
    ))
    db.execute(text("DROP TABLE gs_old"))
    db.commit()

    ensure_schema(engine)
    db.expire_all()
    row = db.query(GameSession).first()
    assert row.trade_order is None
    assert row.last_trade is None
    assert row.reputation == 50


def test_ended_save_clears_trade_order(db):
    """已结束档案残留在途订单：清空，统一收敛到 ended。"""
    gs = make_session(db)
    gs.status = "over"
    db.commit()
    sid = gs.id
    _write_trade(sid, {
        "status": "transporting", "token": "o1", "escorts": [1],
        "eta": 3, "travel_days": 1,
    })
    reconcile_old_saves(engine)
    db.expire_all()
    assert db.get(GameSession, sid).trade_order is None


def test_trade_order_with_all_dangling_escorts_cleared(db):
    gs = make_session(db)
    db.commit()
    sid = gs.id
    _write_trade(sid, {
        "status": "transporting", "token": "o1",
        "escorts": [424242, 525252], "eta": 3, "travel_days": 1,
    })
    reconcile_old_saves(engine)
    db.expire_all()
    assert db.get(GameSession, sid).trade_order is None


def test_trade_order_prunes_dangling_escort_keeps_order(db):
    """押运队夹杂悬空编号：剔除编号，订单保留，可继续运输。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    _write_trade(sid, {
        "status": "transporting", "token": "o1",
        "escorts": [valid, 999999], "eta": 3, "travel_days": 1,
        "pending_incident": None,
    })
    reconcile_old_saves(engine)
    db.expire_all()
    order = db.get(GameSession, sid).trade_order
    assert order is not None
    assert order["escorts"] == [valid]


def test_settled_trade_order_snapshot_cleared(db):
    """已收敛的终态快照（delivered/failed/rejected/cancelled）残留：清空。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    for st in ("delivered", "failed", "rejected", "cancelled"):
        _write_trade(sid, {"status": st, "token": "o", "escorts": [gs.residents[0].id]})
        reconcile_old_saves(engine)
        db.expire_all()
        assert db.get(GameSession, sid).trade_order is None


def test_valid_trade_order_left_untouched(db):
    """结构完整的在途订单：归一化零改写。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    order = {
        "status": "reviewing", "token": "o1",
        "escorts": [gs.residents[0].id], "eta": 2,
        "escrow": {"food": 20}, "cargo": {"power": 15},
        "cargo_ratio": 1.0, "travel_days": 0, "pending_incident": None,
    }
    _write_trade(sid, order)
    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    assert db.get(GameSession, sid).trade_order["token"] == "o1"


def test_trade_incident_with_dangling_target_is_dropped(db):
    """在途事件绑定的目标已不在押运队：丢弃事件但保留订单。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    _write_trade(sid, {
        "status": "transporting", "token": "o1",
        "escorts": [valid], "eta": 3, "travel_days": 1,
        "pending_incident": {
            "token": "inc1", "event": "ambush",
            "target_id": 999999, "choices": [],
        },
    })
    reconcile_old_saves(engine)
    db.expire_all()
    order = db.get(GameSession, sid).trade_order
    assert order is not None
    assert order["pending_incident"] is None


# ---- 医疗病例簿归一化 ----

def _write_medical(sid, cases):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE game_sessions SET medical_cases = :m WHERE id = :sid"),
            {"m": json.dumps(cases, ensure_ascii=False), "sid": sid},
        )


def test_medical_cases_column_backfilled(db):
    """缺列旧表经 ensure_schema 后补齐 medical_cases，旧行为 NULL。"""
    gs = make_session(db)
    db.commit()
    db.expire_all()
    db.execute(text("ALTER TABLE game_sessions RENAME TO gs_old"))
    db.execute(text(
        "CREATE TABLE game_sessions ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
        "status VARCHAR(16), resources JSON, survivors INTEGER, outcome JSON, "
        "score INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    db.execute(text(
        "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
        "survivors,outcome,score,created_at,updated_at FROM gs_old"
    ))
    db.execute(text("DROP TABLE gs_old"))
    db.commit()

    ensure_schema(engine)
    db.expire_all()
    assert db.query(GameSession).first().medical_cases is None


def test_corrupt_medical_cases_cleared(db):
    """病例簿整体结构损坏（非数组）：归一化清空。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    _write_medical(sid, {"status": "treating"})
    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    assert db.get(GameSession, sid).medical_cases is None


def test_active_case_of_missing_resident_pruned(db):
    """活跃病例绑定居民已不在档：整份病例剔除。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    cases = [
        {"id": "c1", "resident_id": gs.residents[0].id, "status": "treating"},
        {"id": "c2", "resident_id": 424242, "status": "isolated"},
    ]
    _write_medical(sid, cases)
    reconcile_old_saves(engine)
    db.expire_all()
    kept = db.get(GameSession, sid).medical_cases
    assert [c["id"] for c in kept] == ["c1"]


def test_active_case_of_dead_resident_settled_deceased(db):
    """居民已死亡但病例仍挂活跃态：收敛为病亡，保留履历。"""
    gs = make_session(db)
    gs.residents[0].alive = 0
    db.commit()
    sid = gs.id
    _write_medical(sid, [
        {"id": "c1", "resident_id": gs.residents[0].id, "status": "treating",
         "open_day": 3, "days_cared": 2},
    ])
    reconcile_old_saves(engine)
    db.expire_all()
    kept = db.get(GameSession, sid).medical_cases
    assert len(kept) == 1
    assert kept[0]["status"] == "deceased"


def test_duplicate_active_cases_for_same_resident_pruned(db):
    """同一居民两条活跃病例：保留最早一条。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    rid = gs.residents[0].id
    cases = [
        {"id": "c1", "resident_id": rid, "status": "registered", "open_day": 2},
        {"id": "c2", "resident_id": rid, "status": "treating", "open_day": 4},
        {"id": "c3", "resident_id": gs.residents[1].id, "status": "recovered",
         "open_day": 1, "close_day": 5},
    ]
    _write_medical(sid, cases)
    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    kept = db.get(GameSession, sid).medical_cases
    assert [c["id"] for c in kept] == ["c1", "c3"]


def test_valid_medical_cases_left_untouched(db):
    """结构完整的病例簿：归一化零改写、不 bump 版本号。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    cases = [
        {"id": "c1", "resident_id": gs.residents[0].id, "status": "isolated",
         "infectious": True, "open_day": 2, "days_cared": 3},
        {"id": "c2", "resident_id": gs.residents[1].id, "status": "recovered",
         "open_day": 1, "close_day": 4},
    ]
    _write_medical(sid, cases)
    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    kept = db.get(GameSession, sid).medical_cases
    assert {c["id"] for c in kept} == {"c1", "c2"}


# ---- 已下线事件 key 的待处理快照（防软锁） ----

def _write_expedition(sid, exp):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE game_sessions SET expedition = :e WHERE id = :sid"),
            {"e": json.dumps(exp, ensure_ascii=False), "sid": sid},
        )


def test_unknown_crisis_event_key_cleared(db):
    """待处理危机引用已下线事件 key：清空，档案回到可推进的每日阶段。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET pending_crisis = :c WHERE id = :sid"),
        {"c": json.dumps({
            "token": "t", "event": "removed_event",
            "choices": [{"key": "x"}], "target_id": None,
        }), "sid": sid},
    )
    db.commit()

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.pending_crisis is None
    eng = BunkerEngine(db, fixed, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()  # 不再被已失效的危机软锁
    db.commit()


def test_unknown_encounter_event_key_dropped_team_kept(db):
    """探索遭遇引用已下线事件 key：仅丢弃遭遇，队伍保留并可继续行军。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET expedition = :e WHERE id = :sid"),
        {"e": json.dumps({
            "status": "away", "token": "team", "members": [valid],
            "supplies": {"food": 5, "water": 5}, "travel_days": 1,
            "pending_encounter": {
                "token": "enc", "event": "removed_encounter",
                "choices": [{"key": "a"}], "target_id": None,
            },
        }), "sid": sid},
    )
    db.commit()

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.expedition is not None
    assert fixed.expedition["pending_encounter"] is None
    eng = BunkerEngine(db, fixed, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()  # 队伍继续行军，不再软锁
    db.commit()


def test_unknown_incident_event_key_dropped_order_kept(db):
    """途中事件引用已下线事件 key：仅丢弃事件，订单保留并可继续运输。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    _write_trade(sid, {
        "status": "transporting", "token": "o1", "type": "rescue",
        "escorts": [valid], "eta": 3, "travel_days": 1,
        "escrow": {"food": 20}, "cargo": {"water": 30}, "cargo_ratio": 1.0,
        "pending_incident": {
            "token": "inc", "event": "removed_incident",
            "choices": [{"key": "a"}], "target_id": None,
        },
    })
    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.trade_order is not None
    assert fixed.trade_order["pending_incident"] is None
    eng = BunkerEngine(db, fixed, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()  # 订单继续推进，不再软锁
    db.commit()


def test_known_event_keys_left_untouched(db):
    """事件 key 仍在当前池中的待处理快照：归一化零改写。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET pending_crisis = :c, expedition = :e WHERE id = :sid"),
        {
            "c": json.dumps({
                "token": "t", "event": "mutiny",
                "choices": [{"key": "suppress"}], "target_id": None,
            }),
            "e": json.dumps({
                "status": "away", "token": "team", "members": [valid],
                "supplies": {"food": 5, "water": 5}, "travel_days": 1,
                "pending_encounter": {
                    "token": "enc", "event": "cache",
                    "choices": [{"key": "search_carefully"}], "target_id": None,
                },
            }),
            "sid": sid,
        },
    )
    db.commit()
    # 危机与探索队并存：危机待处理属合法状态（顺序结算），不属互斥违规
    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.pending_crisis["event"] == "mutiny"
    assert fixed.expedition["pending_encounter"]["event"] == "cache"


# ---- 探索队 / 贸易订单并存的互斥收敛 ----

def test_coexisting_expedition_and_reviewing_order_converges(db):
    """探索队与审核中订单并存：保留探索队，订单撤单收敛，托管全额退回资源。"""
    gs = make_session(db, resources={FOOD: 100, WATER: 100, POWER: 100, OXY: 100})
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    _write_expedition(sid, {
        "status": "away", "token": "team", "members": [valid],
        "supplies": {"food": 5, "water": 5}, "travel_days": 1,
        "pending_encounter": None,
    })
    _write_trade(sid, {
        "status": "reviewing", "token": "o1", "type": "procure",
        "escorts": [gs.residents[1].id], "eta": 2,
        "escrow": {"food": 20, "power": 10}, "cargo": {"water": 30},
        "cargo_ratio": 1.0, "travel_days": 0, "pending_incident": None,
    })

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.expedition is not None        # 探索队保留
    assert fixed.trade_order is None           # 订单收敛
    assert fixed.resources[FOOD] == 120        # 托管全额退回
    assert fixed.resources[POWER] == 110
    # 幂等：第二次归一化零更新
    assert reconcile_old_saves(engine) == 0


def test_coexisting_transporting_order_refunded_at_ratio(db):
    """探索队与在途订单并存：订单按失败回退口径收敛，托管按残存比例退回。"""
    gs = make_session(db, resources={FOOD: 100, WATER: 100, POWER: 100, OXY: 100})
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    _write_expedition(sid, {
        "status": "away", "token": "team", "members": [valid],
        "supplies": {"food": 5, "water": 5}, "travel_days": 1,
    })
    _write_trade(sid, {
        "status": "transporting", "token": "o1", "type": "rescue",
        "escorts": [gs.residents[1].id], "eta": 3, "travel_days": 1,
        "escrow": {"food": 40}, "cargo": {"water": 50}, "cargo_ratio": 0.5,
        "pending_incident": None,
    })

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.expedition is not None
    assert fixed.trade_order is None
    assert fixed.resources[FOOD] == 120        # 40 * 0.5 残存退回
    assert reconcile_old_saves(engine) == 0


def test_coexisting_both_dangling_cleared_independently(db):
    """并存的两者各自损坏（成员全悬空）：各自清空，不涉及托管退款。"""
    gs = make_session(db, resources={FOOD: 100, WATER: 100, POWER: 100, OXY: 100})
    db.commit()
    sid = gs.id
    _write_expedition(sid, {"status": "away", "members": [424242], "token": "t"})
    _write_trade(sid, {
        "status": "transporting", "token": "o1", "escorts": [525252],
        "eta": 3, "travel_days": 1, "escrow": {"food": 40},
    })

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.expedition is None
    assert fixed.trade_order is None
    assert fixed.resources[FOOD] == 100        # 订单因悬空清除，不触发互斥退款
