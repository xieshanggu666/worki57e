# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..core.database import get_db
from ..core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from ..models import GameSession, Resident, Facility
from ..services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    RESOURCE_KEYS,
    FACILITY_OUTPUT,
    FACILITY_COST,
    FACILITY_ZH,
    JOB_EFFICIENCY,
)
from ..schemas import (
    SessionCreate,
    SessionBrief,
    SessionDetail,
    AdvanceResult,
    CrisisChoice,
    ExpeditionSend,
    ExpeditionEncounterChoice,
    ExpeditionReturn,
    TradeApply,
    TradeIncidentChoice,
    TradeCancel,
    JobAssign,
    MedicalRegister,
    MedicalAdmit,
    BuildRequest,
    BuildableInfo,
    EngineConfig,
    Message,
)

router = APIRouter(prefix="/api")


# ---- 会话 ----
@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    rows = (
        db.query(GameSession)
        .order_by(GameSession.created_at.desc())
        .all()
    )
    return [SessionBrief.model_validate(r) for r in rows]


@router.post("/sessions", response_model=SessionDetail, status_code=201)
def create_session(body: SessionCreate, db: Session = Depends(get_db)):
    gs = GameSession(
        name=body.name,
        day=1,
        target_day=SURVIVAL_TARGET_DAY,
        status="running",
        resources=dict(INITIAL_RESOURCES),
        survivors=3,
        score=0,
    )
    db.add(gs)
    db.flush()
    # 初始三名幸存者
    for name, job in (("林粤", "engineer"), ("夏岚", "farmer"), ("老周", "general")):
        db.add(
            Resident(
                session_id=gs.id,
                name=name,
                job=job,
                health=90.0,
                morale=80.0,
                alive=1,
                joined_day=1,
            )
        )
    # 初始设施
    for cat in ("power", "farm", "water", "oxygen"):
        db.add(
            Facility(
                session_id=gs.id,
                name=FACILITY_ZH[cat],
                category=cat,
                level=1,
                status="active",
                built_day=1,
            )
        )
    db.commit()
    db.refresh(gs)
    return get_session_detail(gs, db)


@router.get("/sessions/{sid}", response_model=SessionDetail)
def get_session(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    return get_session_detail(gs, db)


def _serialize_resident(r, away_ids=None, trade_status=None, case_map=None):
    active_case = (case_map or {}).get(r.id)
    return {
        "id": r.id,
        "name": r.name,
        "job": r.job,
        "job_zh": JOB_ZH.get(r.job, r.job),
        "health": r.health,
        "morale": r.morale,
        "alive": r.alive,
        "away": 1 if (away_ids and r.id in away_ids) else 0,
        # 医疗救治中心病例状态：仅活跃病例（登记/治疗/隔离）随居民下发
        "case_status": active_case["status"] if active_case else None,
        "case_infectious": 1 if (active_case and active_case.get("infectious")) else 0,
        # 贸易订单角色：transporting=在途押运（按离堡结算）；reviewing=待出发押运（仍在堡）
        "trade_status": trade_status,
        "joined_day": r.joined_day,
    }


def get_session_detail(gs, db):
    # 离堡成员编号（探索队 + 在途贸易押运队）：用于标注居民"探索中/押运中"状态
    away_ids = set()
    if gs.expedition and gs.expedition.get("status") == "away":
        away_ids = set(gs.expedition.get("members", []))
    trade_tag = {}
    if gs.trade_order:
        order = gs.trade_order
        if order.get("status") == "transporting":
            for mid in order.get("escorts", []):
                away_ids.add(mid)
                trade_tag[mid] = "transporting"
        elif order.get("status") == "reviewing":
            for mid in order.get("escorts", []):
                trade_tag[mid] = "reviewing"
    # 活跃病例（登记/治疗/隔离）按居民编号映射，终态履历只保留在 medical_cases 中
    med_cases = gs.medical_cases or []
    case_map = {
        c.get("resident_id"): c for c in med_cases
        if c.get("status") in ("registered", "treating", "isolated")
    }
    residents = [
        _serialize_resident(r, away_ids, trade_tag.get(r.id), case_map)
        for r in gs.residents
    ]
    facilities = [
        {
            "id": f.id,
            "name": f.name,
            "category": f.category,
            "level": f.level,
            "status": f.status,
            "built_day": f.built_day,
        }
        for f in gs.facilities
    ]
    logs = [
        {
            "id": l.id,
            "day": l.day,
            "event_type": l.event_type,
            "title": l.title,
            "detail": l.detail,
            "decision": l.decision,
        }
        for l in gs.logs
    ]
    return SessionDetail(
        id=gs.id,
        name=gs.name,
        day=gs.day,
        target_day=gs.target_day,
        status=gs.status,
        resources={k: gs.resources.get(k, 0) for k in RESOURCE_KEYS},
        survivors=gs.survivors,
        score=gs.score,
        outcome=gs.outcome,
        pending_crisis=gs.pending_crisis,
        expedition=gs.expedition,
        trade_order=gs.trade_order,
        medical_cases=med_cases,
        # 床位/医护/病例计数概览（纯派生，由引擎计算保证与口径一致）
        medical_summary=BunkerEngine(db, gs).med_summary(),
        reputation=gs.reputation if gs.reputation is not None else 50,
        residents=residents,
        facilities=facilities,
        logs=logs,
    )


# ---- 游戏动作 ----
def _run_mutation(db, gs, action):
    """统一执行经营类状态变更。

    - 业务校验失败（BunkerEngineError）→ 400
    - 乐观锁版本冲突（并发请求已先行落库，StaleDataError）→ 409，
      落败方不产生任何效果，避免重复推进/重复扣费
    """
    eng = BunkerEngine(db, gs)
    try:
        action(eng)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        db.rollback()
        db.refresh(gs)
        raise HTTPException(409, "档案已被其他请求更新，请刷新后重试")


@router.post("/sessions/{sid}/advance", response_model=AdvanceResult)
def advance(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        crisis = eng.advance_day()
        db.commit()
        db.refresh(gs)
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发/重复的“推进一天”落败：另一个请求已经推进过，这里幂等回放
        # 当前状态（含可能已挂起的待处理危机/探索遭遇），绝不再多推进一天
        db.rollback()
        db.refresh(gs)
        crisis = gs.pending_crisis
    # 探索队在外时推进可能挂起的是遭遇而非地堡危机：把当前任一待处理抉择带回，
    # 前端据此恢复对应弹层（落败回放与正常返回保持同一口径）
    if crisis is None and gs.expedition and gs.expedition.get("pending_encounter"):
        crisis = gs.expedition["pending_encounter"]
    # 贸易押运队在途时推进也可能挂起途中事件，统一走 pending_event 返回
    if crisis is None and gs.trade_order and gs.trade_order.get("pending_incident"):
        crisis = gs.trade_order["pending_incident"]
    # pending_event 为语义准确的新字段；crisis 为兼容旧前端的同值别名
    return AdvanceResult(session=get_session_detail(gs, db), pending_event=crisis, crisis=crisis)


@router.post("/sessions/{sid}/resolve", response_model=SessionDetail)
def resolve_crisis(sid: int, body: CrisisChoice, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.resolve_crisis(
            body.event_key, body.choice_key, body.target_id, token=body.token
        )
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复结算：版本不匹配说明对方已先落库。核对是否同一次抉择：
        # 相同则幂等回放当前状态（效果只结算一次），否则 409 拒绝
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_resolution(
                body.event_key, body.choice_key, body.target_id, token=body.token
            )
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


# ---- 探索队 ----
@router.post("/sessions/{sid}/expedition/send", response_model=SessionDetail)
def send_expedition(sid: int, body: ExpeditionSend, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.send_expedition(body.member_ids, body.supplies))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/expedition/resolve", response_model=SessionDetail)
def resolve_expedition(sid: int, body: ExpeditionEncounterChoice, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.resolve_expedition_encounter(body.choice_key, token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复结算：版本不匹配说明对方已先落库。核对是否同一次遭遇抉择：
        # 相同则幂等回放当前状态（效果只结算一次），否则 409 拒绝
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_expedition(
                "encounter", token=body.token, choice_key=body.choice_key
            )
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/expedition/return", response_model=SessionDetail)
def return_expedition(sid: int, body: ExpeditionReturn, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.return_expedition(token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复返程：版本不匹配说明对方已先落库。核对是否同一支队伍：
        # 相同则幂等回放（战利品只结算一次，队伍已清除也能识别），否则 409
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_expedition("return", exp_token=body.token)
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


# ---- 贸易救援 ----
@router.get("/sessions/{sid}/trade/market")
def trade_market(sid: int, db: Session = Depends(get_db)):
    """当日外部聚落的贸易/救援报价（按天确定性轮换，只读）。"""
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    return {
        "day": gs.day,
        "reputation": gs.reputation if gs.reputation is not None else 50,
        "offers": eng.trade_market(),
    }


@router.post("/sessions/{sid}/trade/apply", response_model=SessionDetail)
def trade_apply(sid: int, body: TradeApply, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(
        db, gs, lambda eng: eng.apply_trade(body.offer_id, body.escort_ids)
    )
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/trade/resolve", response_model=SessionDetail)
def trade_resolve(sid: int, body: TradeIncidentChoice, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.resolve_trade_incident(body.choice_key, token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复结算：版本不匹配说明对方已先落库。核对是否同一次途中事件抉择：
        # 相同（含抉择直接触发失败回退的情形）则幂等回放，否则 409 拒绝
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_trade(
                "incident", token=body.token, choice_key=body.choice_key
            )
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/trade/cancel", response_model=SessionDetail)
def trade_cancel(sid: int, body: TradeCancel, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.cancel_trade(token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发撤单落败：同一订单的撤单已落库时幂等回放，托管只退一次
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_trade("cancel", token=body.token)
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/build", response_model=SessionDetail)
def build(sid: int, body: BuildRequest, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    if body.category not in FACILITY_OUTPUT:
        raise HTTPException(400, "未知设施类别")
    _run_mutation(db, gs, lambda eng: eng.build_facility(body.category))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/upgrade/{fid}", response_model=SessionDetail)
def upgrade(sid: int, fid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.upgrade_facility(fid))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/resident/{rid}/job", response_model=SessionDetail)
def set_job(sid: int, rid: int, body: JobAssign, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.set_job(rid, body.job))
    return get_session_detail(gs, db)


# ---- 医疗救治中心 ----
@router.post("/sessions/{sid}/medical/register/{rid}", response_model=SessionDetail)
def register_case(sid: int, rid: int, body: MedicalRegister, db: Session = Depends(get_db)):
    """为居民登记病例（健康低于登记线且已建救治中心）。"""
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(
        db, gs,
        lambda eng: eng.register_case(rid, infectious=body.infectious),
    )
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/medical/admit/{rid}", response_model=SessionDetail)
def admit_case(sid: int, rid: int, body: MedicalAdmit, db: Session = Depends(get_db)):
    """收治已登记病例：转入治疗或隔离床位（已在治者可切换方案）。"""
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.admit_case(rid, body.mode))
    return get_session_detail(gs, db)


@router.delete("/sessions/{sid}", response_model=Message)
def delete_session(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    db.delete(gs)
    db.commit()
    return Message(detail="已删除")


# ---- 配置信息 ----
@router.get("/config", response_model=EngineConfig)
def get_config():
    return EngineConfig(
        resources=dict(INITIAL_RESOURCES),
        facility_costs=FACILITY_COST,
        facility_names=FACILITY_ZH,
        job_options=list(JOB_EFFICIENCY.keys()),
        status="running",
    )


@router.get("/buildings", response_model=list)
def list_buildable():
    return [
        BuildableInfo(
            category=k,
            name=FACILITY_ZH[k],
            cost=FACILITY_COST[1],
            level_scale=1.6,
        )
        for k in ("farm", "water", "power", "oxygen", "clinic", "med", "storage")
    ]


JOB_ZH = {"engineer": "工程师", "farmer": "农民", "general": "杂工", "medic": "医护"}