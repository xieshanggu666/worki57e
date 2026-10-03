# -*- coding: utf-8 -*-
from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class SessionCreate(BaseModel):
    name: str = "末日地堡档案"


class SessionBrief(BaseModel):
    id: int
    name: str
    day: int
    target_day: int
    status: str
    survivors: int
    score: int

    class Config:
        from_attributes = True


class ResidentOut(BaseModel):
    id: int
    name: str
    job: str
    job_zh: Optional[str] = None
    health: float
    morale: float
    alive: int
    away: int = 0
    # 医疗救治中心病例状态：registered/treating/isolated（终态/无病例不下发）
    case_status: Optional[str] = None
    case_infectious: int = 0
    # 贸易订单角色：transporting=在途押运；reviewing=待出发押运；None=无订单
    trade_status: Optional[str] = None
    joined_day: int

    class Config:
        from_attributes = True


class FacilityOut(BaseModel):
    id: int
    name: str
    category: str
    level: int
    status: str
    built_day: int

    class Config:
        from_attributes = True


class LogOut(BaseModel):
    id: int
    day: int
    event_type: str
    title: str
    detail: str
    decision: Optional[str] = None

    class Config:
        from_attributes = True


class SessionDetail(BaseModel):
    id: int
    name: str
    day: int
    target_day: int
    status: str
    resources: Dict[str, float]
    survivors: int
    score: int
    outcome: Optional[Dict[str, Any]] = None
    # 待处理危机快照：刷新/重进档案后前端据此恢复决策弹层
    pending_crisis: Optional[Dict[str, Any]] = None
    # 探索队状态快照：在外行军/遭遇/返程，刷新后恢复同一支队伍
    expedition: Optional[Dict[str, Any]] = None
    # 当前贸易/救援订单快照（审核中/在途），终局后清空
    trade_order: Optional[Dict[str, Any]] = None
    # 医疗救治中心病例簿（登记/治疗/隔离/康复/病亡全履历）与床位概览
    medical_cases: Optional[List[Dict[str, Any]]] = None
    medical_summary: Optional[Dict[str, Any]] = None
    # 地堡对外信誉 0-100，影响外部聚落审核/交付
    reputation: int = 50
    residents: List[ResidentOut] = []
    facilities: List[FacilityOut] = []
    logs: List[LogOut] = []


class AdvanceResult(BaseModel):
    session: SessionDetail
    # 本次推进挂起的待处理抉择：可能是地堡危机，也可能是探索队遭遇，
    # 前端统一据 session.pending_crisis / session.expedition.pending_encounter 渲染
    pending_event: Optional[Dict[str, Any]] = None
    # 兼容旧字段名（旧前端读取 crisis）；构造方保证与 pending_event 同值
    crisis: Optional[Dict[str, Any]] = None


class CrisisChoice(BaseModel):
    event_key: str
    choice_key: str
    target_id: Optional[int] = None
    # 待处理危机的一次性凭据，用于识别过期/并发的旧请求；旧客户端可省略
    token: Optional[str] = None


class ExpeditionSend(BaseModel):
    """派遣探索队：选择在堡居民与自带物资。"""
    member_ids: List[int]
    supplies: Dict[str, float] = {}


class ExpeditionEncounterChoice(BaseModel):
    """处理探索队途中遭遇。"""
    choice_key: str
    # 待处理遭遇的一次性凭据，用于识别过期/重复请求
    token: Optional[str] = None


class ExpeditionReturn(BaseModel):
    """召回探索队。"""
    token: Optional[str] = None


class TradeApply(BaseModel):
    """提交贸易/救援订单：选择当日市场报价与押运队员。"""
    offer_id: str
    escort_ids: List[int]


class TradeIncidentChoice(BaseModel):
    """处理押运途中事件。"""
    choice_key: str
    # 途中事件的一次性凭据，用于识别过期/重复请求
    token: Optional[str] = None


class TradeCancel(BaseModel):
    """审核阶段撤单。"""
    token: Optional[str] = None


class JobAssign(BaseModel):
    job: str


class MedicalRegister(BaseModel):
    """医疗救治中心病例登记：传染病例建议随后转入隔离。"""
    infectious: bool = False


class MedicalAdmit(BaseModel):
    """病例收治：mode=treating（治疗）/ isolated（隔离）。"""
    mode: str


class BuildRequest(BaseModel):
    category: str


class BuildableInfo(BaseModel):
    category: str
    name: str
    cost: Dict[str, float]
    level_scale: float


class EngineConfig(BaseModel):
    resources: Dict[str, float]
    facility_costs: Dict[int, Dict[str, float]]
    facility_names: Dict[str, str]
    job_options: List[str]
    status: str


class Message(BaseModel):
    detail: str = "ok"