# -*- coding: utf-8 -*-
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Text,
    DateTime,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.ext.mutable import MutableDict

from ..core.database import Base


class GameSession(Base):
    __tablename__ = "game_sessions"

    # 乐观锁版本号：并发的每日推进/危机结算只会有一个请求落库，
    # 落败请求在 UPDATE 时因版本不匹配失败，从而杜绝重复结算
    row_version = Column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": row_version}

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), nullable=False, default="末日地堡档案")
    day = Column(Integer, nullable=False, default=1)
    target_day = Column(Integer, nullable=False, default=120)
    status = Column(String(16), nullable=False, default="running")  # running/over/win
    resources = Column(JSON, nullable=False, default=dict)  # {food,water,power,oxygen}
    survivors = Column(Integer, nullable=False, default=0)
    # 待决事件快照列（含一次性 token、绑定的事件与目标），落库后刷新可恢复决策；
    # 为 None 表示当前处于“每日阶段”，不允许凭空结算危机。
    # 三类待决事件（危机/探索遭遇/押运途中事件）共用引擎的"待决事件统一管线"
    # （见 services/engine.py 的 _PENDING_SLOTS）：同构快照、幂等凭据、互斥与终局收敛
    pending_crisis = Column(JSON, nullable=True)
    # 最近一次危机结算的幂等凭据，重复/并发落败请求据此安全回放，不再二次结算
    last_resolution = Column(JSON, nullable=True)
    # 最近一次探索队动作（遭遇抉择/返程）的幂等凭据，作用与 last_resolution 相同：
    # 队伍在动作完成后即被清除时，凭此仍能识别并发落败/连点的重复请求并安全回放
    last_expedition = Column(JSON, nullable=True)
    # 探索队状态快照（含一次性 token、队员、携带物资、行军天数、遭遇与战利品），
    # 落库后刷新可恢复同一支队伍；为 None 表示当前没有在外的探索队。
    # MutableDict.as_mutable：原地修改 JSON 字段（如 exp["travel_days"]=1）也会被追踪落库
    expedition = Column(MutableDict.as_mutable(JSON), nullable=True)
    # 贸易/救援订单快照（含一次性 token、交易对手、押运队、托管物资、在途货物、
    # 运输天数与途中事件），落库后刷新可恢复同一订单；为 None 表示当前没有在谈订单。
    # 状态链：reviewing(申请待审) → transporting(押运运输) → delivered/failed/rejected/cancelled
    trade_order = Column(MutableDict.as_mutable(JSON), nullable=True)
    # 最近一次贸易动作（途中事件抉择/事件直接触发的失败收敛）的幂等凭据，
    # 作用与 last_expedition 相同：订单在动作完成后即被清除时，凭此仍能识别
    # 并发落败/连点的重复请求并安全回放
    last_trade = Column(JSON, nullable=True)
    # 医疗救治中心病例簿：每名居民的病例经历 登记→治疗/隔离→康复/病亡。
    # 活跃病例驱动每日医疗结算（床位占用/物资消耗/传染扩散），终态病例作为
    # 危机后健康结算履历保留至终局。引擎以"深拷贝整体回写"（_save_cases）
    # 保证 JSON 列追踪到病例状态的原地修改
    medical_cases = Column(JSON, nullable=True)
    # 地堡对外信誉 0-100：影响外部聚落的审核通过率与交付成功率，随订单成败升降
    reputation = Column(Integer, nullable=False, default=50)
    outcome = Column(JSON, nullable=True)  # 结局详情
    score = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    residents = relationship("Resident", back_populates="session", cascade="all, delete-orphan")
    facilities = relationship("Facility", back_populates="session", cascade="all, delete-orphan")
    logs = relationship("EventLog", back_populates="session", cascade="all, delete-orphan")


class Resident(Base):
    __tablename__ = "residents"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=False)
    name = Column(String(32), nullable=False)
    job = Column(String(32), nullable=False)  # 岗位: farmer/gardener/medic/engineer/general...
    health = Column(Float, nullable=False, default=100.0)  # 0-100
    morale = Column(Float, nullable=False, default=80.0)  # 0-100
    alive = Column(Integer, nullable=False, default=1)
    joined_day = Column(Integer, nullable=False, default=1)

    session = relationship("GameSession", back_populates="residents")


class Facility(Base):
    __tablename__ = "facilities"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=False)
    name = Column(String(32), nullable=False)
    # 类别: farm(产食物), water(产水), power(产电), oxygen(产氧), storage(仓库), med(医疗)
    category = Column(String(16), nullable=False)
    level = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="active")  # active/offline
    built_day = Column(Integer, nullable=False, default=1)

    session = relationship("GameSession", back_populates="facilities")


class EventLog(Base):
    __tablename__ = "event_logs"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=False)
    day = Column(Integer, nullable=False)
    event_type = Column(String(32), nullable=False)  # crisis/update/system
    title = Column(String(64), nullable=False)
    detail = Column(Text, nullable=False, default="")
    decision = Column(String(64), nullable=True)

    session = relationship("GameSession", back_populates="logs")