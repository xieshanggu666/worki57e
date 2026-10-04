# -*- coding: utf-8 -*-
"""轻量建表后迁移：为旧版数据库补齐新增列并归一化历史快照。

项目没有引入 Alembic，这里用 ADD COLUMN 做前向兼容（对已是最新结构的库为幂等无操作）：
- pending_crisis / last_resolution / last_expedition / expedition：状态机快照列，旧行补 NULL
- trade_order / last_trade：贸易救援订单与幂等凭据列，旧行补 NULL
- medical_cases：医疗救治中心病例簿列，旧行补 NULL
- reputation：对外信誉，旧行统一从初始值 50 开始
- row_version：乐观锁版本号，旧行统一从 1 开始

补列后再做一次“旧存档归一化”（reconcile_old_saves），保证历史行加载进新引擎后
状态机可以唯一收敛：
- 已结束档案上悬而未决的危机/探索队/贸易订单一律清除，统一收敛到 ended
- 损坏/悬空的快照（成员全部不在档、目标居民失踪、JSON 残缺）不阻塞每日推进
- 待处理危机/遭遇/途中事件引用已下线的事件 key 时清除该抉择，避免档案软锁
  （不清除会永远卡在对应阶段：结算报“已失效”、推进被状态机拒绝）
- 探索队与贸易订单并存（互斥违规）时按引擎日推进优先级保留探索队，
  订单按撤单/失败回退口径收敛并把托管物资退回资源，不凭空蒸发
- 病例簿中结构残缺或居民已不在档的活跃病例收敛/剔除，终态履历同步校正
- survivors 与实际存活居民数漂移时以居民表为准校正
"""
import json

from sqlalchemy import inspect, text


def _existing_columns(conn, table):
    try:
        return {c["name"] for c in inspect(conn).get_columns(table)}
    except Exception:
        return set()


def ensure_schema(engine):
    with engine.begin() as conn:
        columns = _existing_columns(conn, "game_sessions")
        if not columns:
            # 表尚未创建，create_all 会按最新模型建表，无需迁移
            return
        if "pending_crisis" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN pending_crisis JSON"))
        if "last_resolution" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_resolution JSON"))
        if "expedition" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN expedition JSON"))
        if "last_expedition" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_expedition JSON"))
        if "trade_order" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN trade_order JSON"))
        if "last_trade" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_trade JSON"))
        if "medical_cases" not in columns:
            # 医疗救治中心病例簿：旧档案从未开展救治，从空病历开始
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN medical_cases JSON"))
        if "reputation" not in columns:
            # NOT NULL + 常量默认值：旧档案从未开展贸易，信誉从初始值 50 开始
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN reputation INTEGER NOT NULL DEFAULT 50")
            )
        if "row_version" not in columns:
            # NOT NULL + 常量默认值，存量行全部初始化为 1
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1")
            )
    # DDL 提交后再做数据归一化（SQLite 不允许在同一事务里混用 DDL 与行更新）
    reconcile_old_saves(engine)


def _loads(raw):
    """SQLite JSON 列读回可能是 str/dict/None；解析失败一律按损坏快照处理。"""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _known_event_keys():
    """当前版本三类抉择事件池的 key 集合（危机/探索遭遇/贸易途中事件）。

    延迟导入避免 core → services 的模块级依赖；旧存档里引用已下线事件 key 的
    待处理快照对状态机而言等同于损坏快照，归一化时按池外事件清除。
    """
    from ..services.engine import CRISIS_POOL, EXPEDITION_ENCOUNTERS, TRADE_INCIDENTS
    return (
        {e["key"] for e in CRISIS_POOL},
        {e["key"] for e in EXPEDITION_ENCOUNTERS},
        {e["key"] for e in TRADE_INCIDENTS},
    )


def reconcile_old_saves(engine):
    """旧存档快照归一化：见模块文档字符串。幂等，可重复执行。

    只有“确实发生了变化”的行才会被 UPDATE：快照本就规范时零写入，
    不会无谓 bump 乐观锁版本号。
    """
    inspector = inspect(engine)
    if "game_sessions" not in inspector.get_table_names():
        return 0
    resident_columns = _existing_columns(engine, "residents")
    crisis_keys, encounter_keys, incident_keys = _known_event_keys()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, pending_crisis, expedition, trade_order, "
                "medical_cases, survivors, resources "
                "FROM game_sessions"
            )
        ).all()
        updates = []
        for sid, status, crisis_raw, exp_raw, trade_raw, med_raw, survivors, res_raw in rows:
            old_crisis = _loads(crisis_raw)
            old_exp = _loads(exp_raw)
            old_trade = _loads(trade_raw)
            old_med = _loads(med_raw)
            new_resources, resources_changed = _loads(res_raw), False
            alive_count = None
            if {"id", "alive", "session_id"} <= resident_columns:
                alive_count = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM residents WHERE session_id = :sid AND alive = 1"
                    ),
                    {"sid": sid},
                ).scalar()

            if status == "running":
                # 运行中：只清理无法再被状态机处理的损坏/悬空快照
                new_crisis, crisis_changed = _clean_pending_crisis(
                    conn, sid, old_crisis, crisis_keys
                )
                new_exp, exp_changed = _clean_expedition(old_exp, conn, sid, encounter_keys)
                new_trade, trade_changed = _clean_trade_order(old_trade, conn, sid, incident_keys)
                if new_exp is not None and new_trade is not None:
                    # 互斥违规：同一档案同时存在在外探索队与在谈/在途订单
                    # （正常运行时两者互斥，只可能来自旧版/损坏快照）。与引擎
                    # 日推进的优先级一致保留探索队；订单按撤单/失败回退口径收敛，
                    # 托管物资退回资源而不是随快照凭空蒸发
                    new_resources, resources_changed = _refund_trade_escrow(
                        new_resources, new_trade
                    )
                    new_trade, trade_changed = None, True
            else:
                # 已结束：危机/探索队/贸易订单快照一律清空，阶段统一收敛到 ended
                new_crisis, crisis_changed = None, old_crisis is not None
                new_exp, exp_changed = None, old_exp is not None
                new_trade, trade_changed = None, old_trade is not None
            # 病例簿归一化与档案阶段无关：活跃病例绑定的居民必须仍在档且状态一致
            new_med, med_changed = _clean_medical_cases(old_med, conn, sid)

            if crisis_changed or exp_changed or trade_changed or med_changed or (
                alive_count is not None and alive_count != survivors
            ):
                updates.append(
                    {
                        "sid": sid,
                        "crisis": json.dumps(new_crisis, ensure_ascii=False)
                        if new_crisis is not None
                        else None,
                        "expedition": json.dumps(new_exp, ensure_ascii=False)
                        if new_exp is not None
                        else None,
                        "trade": json.dumps(new_trade, ensure_ascii=False)
                        if new_trade is not None
                        else None,
                        "medical": json.dumps(new_med, ensure_ascii=False)
                        if new_med is not None
                        else None,
                        "survivors": alive_count if alive_count is not None else survivors,
                        # 资源列仅在互斥收敛退托管时变化；未解析成功的损坏资源
                        # 快照原样回写（同值覆盖），绝不清空成 NULL
                        "resources": json.dumps(new_resources, ensure_ascii=False)
                        if resources_changed
                        else res_raw,
                    }
                )
        for u in updates:
            conn.execute(
                text(
                    "UPDATE game_sessions SET pending_crisis = :crisis, "
                    "expedition = :expedition, trade_order = :trade, "
                    "medical_cases = :medical, "
                    "survivors = :survivors, resources = :resources WHERE id = :sid"
                ),
                u,
            )
    return len(updates)


def _resident_ids(conn, sid):
    return {
        row[0]
        for row in conn.execute(
            text("SELECT id FROM residents WHERE session_id = :sid"), {"sid": sid}
        ).all()
    }


def _clean_pending_crisis(conn, sid, crisis, known_keys):
    """待处理危机快照结构残缺、事件 key 已下线或绑定目标已不在档：
    清空以解除每日阶段的死锁。

    返回 (归一化快照, 是否发生变化)。
    """
    if crisis is None:
        return None, False
    if not isinstance(crisis, dict):
        return None, True
    if not crisis.get("event") or not isinstance(crisis.get("choices"), list):
        return None, True
    if crisis.get("event") not in known_keys:
        # 事件已下线：留着会永远卡在 crisis 阶段（结算失效、推进被拒）
        return None, True
    target_id = crisis.get("target_id")
    if target_id is not None and target_id not in _resident_ids(conn, sid):
        return None, True
    return crisis, False


def _clean_expedition(exp, conn, sid, known_keys):
    """探索队快照结构残缺或成员全部不在档：清空（队伍无法恢复）。

    成员中夹杂已删除/重复编号时剔除；剔除后无人则整队清除。
    待处理遭遇引用已下线事件 key 或绑定目标已不在队时仅丢弃该遭遇。
    返回 (归一化快照, 是否发生变化)。
    """
    if exp is None:
        return None, False
    if not isinstance(exp, dict) or exp.get("status") != "away":
        return None, True
    raw_members = exp.get("members")
    if not isinstance(raw_members, list):
        return None, True
    known = _resident_ids(conn, sid)
    # 剔除悬空/重复编号，保持首次出现顺序
    members, seen = [], set()
    for m in raw_members:
        if m in known and m not in seen:
            seen.add(m)
            members.append(m)
    if not members:
        return None, True
    changed = members != raw_members
    cleaned = dict(exp)
    cleaned["members"] = members
    # 待处理遭遇绑定的目标若已不在队中，丢弃该遭遇（无法再结算单体效果）
    pending = cleaned.get("pending_encounter")
    if not isinstance(pending, dict):
        if pending is not None:
            changed = True
        cleaned["pending_encounter"] = None
    elif pending.get("event") not in known_keys:
        # 遭遇事件已下线：丢弃遭遇保留队伍，否则档案永远卡在 expedition 阶段
        cleaned["pending_encounter"] = None
        changed = True
    else:
        target_id = pending.get("target_id")
        if target_id is not None and target_id not in members:
            cleaned["pending_encounter"] = None
            changed = True
    return cleaned, changed


# 贸易订单合法状态链
_TRADE_STATUSES = {"reviewing", "transporting", "delivered", "failed", "rejected", "cancelled"}


def _clean_trade_order(order, conn, sid, known_keys):
    """贸易订单快照归一化。

    - 结构残缺 / 非法状态：清空（无法恢复的订单）
    - 已收敛的终态（delivered/failed/rejected/cancelled）残留在列上：清空，
      统一以"无在谈订单"开始（结算结果早已回写资源/信誉）
    - 押运成员夹杂悬空/重复编号：剔除；剔除后无人且已在途则整单清除
    - 在途中事件引用已下线事件 key 或绑定目标已不在队：丢弃该事件
      （无法再结算单体效果），订单本身保留，下一次推进正常运输/交付

    返回 (归一化快照, 是否发生变化)。
    """
    if order is None:
        return None, False
    if not isinstance(order, dict):
        return None, True
    status = order.get("status")
    if status not in _TRADE_STATUSES:
        return None, True
    if status in ("delivered", "failed", "rejected", "cancelled"):
        return None, True
    raw_escorts = order.get("escorts")
    if not isinstance(raw_escorts, list) or not order.get("token"):
        return None, True
    known = _resident_ids(conn, sid)
    escorts, seen = [], set()
    for m in raw_escorts:
        if m in known and m not in seen:
            seen.add(m)
            escorts.append(m)
    if not escorts:
        return None, True
    changed = escorts != raw_escorts
    cleaned = dict(order)
    cleaned["escorts"] = escorts
    pending = cleaned.get("pending_incident")
    if not isinstance(pending, dict):
        if pending is not None:
            changed = True
        cleaned["pending_incident"] = None
    elif pending.get("event") not in known_keys:
        # 途中事件已下线：丢弃事件保留订单，否则档案永远卡在 trade 阶段
        cleaned["pending_incident"] = None
        changed = True
    else:
        target_id = pending.get("target_id")
        if target_id is not None and target_id not in escorts:
            cleaned["pending_incident"] = None
            changed = True
    return cleaned, changed


def _refund_trade_escrow(resources, order):
    """互斥收敛时把订单托管物资退回资源快照，返回 (新资源快照, 是否发生变化)。

    口径与引擎一致：审核中（reviewing）撤单全额退还；在途（transporting）
    按失败回退以货物残存比例退还。资源快照损坏（无法解析）时不做退款，
    避免把整份资源覆盖成只剩托管的残缺快照。
    """
    if not isinstance(resources, dict):
        return resources, False
    ratio = 1.0 if order.get("status") == "reviewing" else order.get("cargo_ratio", 1.0)
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        ratio = 1.0
    ratio = max(0.0, min(1.0, ratio))
    refunded = dict(resources)
    changed = False
    escrow = order.get("escrow")
    if isinstance(escrow, dict):
        for k, v in escrow.items():
            try:
                amt = round(float(v) * ratio, 1)
            except (TypeError, ValueError):
                continue
            if amt > 0:
                try:
                    cur = float(refunded.get(k, 0.0) or 0.0)
                except (TypeError, ValueError):
                    cur = 0.0
                refunded[k] = round(cur + amt, 1)
                changed = True
    return refunded, changed


# 病例合法状态：登记/治疗/隔离（活跃）与康复/病亡（终态）
_MED_STATUSES = {"registered", "treating", "isolated", "recovered", "deceased"}
_MED_ACTIVE = {"registered", "treating", "isolated"}


def _clean_medical_cases(cases, conn, sid):
    """医疗病例簿归一化。

    - 整体结构损坏（非 list）：清空，巡检会重新建档
    - 单条病例字段残缺（无 id/resident_id/合法状态）：剔除
    - 活跃病例绑定居民不在档：整份病例剔除（悬空病例无法再结算）
    - 活跃病例绑定居民已死亡（alive=0）：收敛为 deceased，保持病亡履历一致
    - 同一居民多条活跃病例：仅保留最早建档的一条，其余重复病例剔除

    返回 (归一化病例簿, 是否发生变化)。
    """
    if cases is None:
        return None, False
    if not isinstance(cases, list):
        return None, True
    known = _resident_ids(conn, sid)
    dead_ids = {
        row[0]
        for row in conn.execute(
            text("SELECT id FROM residents WHERE session_id = :sid AND alive = 0"),
            {"sid": sid},
        ).all()
    }
    cleaned = []
    changed = False
    active_owners = set()
    for c in cases:
        if not isinstance(c, dict) or not c.get("id"):
            changed = True
            continue
        rid = c.get("resident_id")
        status = c.get("status")
        if rid is None or status not in _MED_STATUSES:
            changed = True
            continue
        if rid not in known:
            # 居民已不在档：活跃/终态病历都无法对应到人，一并剔除
            changed = True
            continue
        c = dict(c)
        if status in _MED_ACTIVE:
            if rid in dead_ids:
                # 居民死亡但病例仍挂活跃态：收敛为病亡
                c["status"] = "deceased"
                c["close_day"] = c.get("close_day")
                changed = True
                cleaned.append(c)
                continue
            if rid in active_owners:
                # 同一居民重复的活跃病例：保留最早一条
                changed = True
                continue
            active_owners.add(rid)
        cleaned.append(c)
    if not cleaned:
        # 归一化后无任何病历：回 None（等价空病历），减少无意义 JSON 落库
        return None, True
    return cleaned, changed
