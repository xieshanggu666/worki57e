# -*- coding: utf-8 -*-
"""医疗救治中心：病例登记/收治/隔离/康复状态链，医护岗位、床位设施、
资源消耗、疫病扩散与危机后健康结算的引擎级测试。"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from app.models import GameSession, Resident, Facility
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    CRISIS_POOL,
    FACILITY_ZH,
    MED_REGISTERED,
    MED_TREATING,
    MED_ISOLATED,
    MED_RECOVERED,
    MED_DECEASED,
    FOOD,
    WATER,
    POWER,
    OXY,
)
from tests.test_engine import make_session, FixedRand, arm_crisis  # noqa: F401


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


class NoSpreadRand(FixedRand):
    """random 恒为 0.9：不触发危机、也不触发疫病扩散（0.9 > 0.35）。"""


class AlwaysSpreadRand(NoSpreadRand):
    """random 恒为 0.0：不触发危机（0.0 <= 0.45 会触发危机，故仅用于医疗 tick
    内部；推进日测试需自行布置病例后直接调用 _apply_medical_tick），
    疫病扩散必然掷中。choice 恒取第一个。"""

    def random(self):
        return 0.0


def build_clinic(eng, level=1):
    eng.build_facility("clinic")
    if level > 1:
        f = next(x for x in eng.session.facilities if x.category == "clinic")
        for _ in range(level - 1):
            eng.upgrade_facility(f.id)
    return eng._active_clinic()


def open_case_directly(eng, resident, infectious=False, status=MED_REGISTERED):
    """直接建档（绕过手动登记的健康线与阶段校验），用于医疗 tick 测试。"""
    return eng._open_case(resident, infectious=infectious, reason="测试布置",
                          kind=("疫病" if infectious else "伤病"), status=status)


# ---- 登记 ----

def test_register_requires_clinic(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    r = gs.residents[0]
    r.health = 50
    with pytest.raises(BunkerEngineError):
        eng.register_case(r.id)
    assert gs.medical_cases is None


def test_register_requires_low_health(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 90
    with pytest.raises(BunkerEngineError):
        eng.register_case(r.id)


def test_register_creates_case(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 40
    eng.register_case(r.id, infectious=True)
    cases = gs.medical_cases
    assert len(cases) == 1
    c = cases[0]
    assert c["resident_id"] == r.id
    assert c["status"] == MED_REGISTERED
    assert c["infectious"] is True
    # 同一居民不能重复登记
    with pytest.raises(BunkerEngineError):
        eng.register_case(r.id)
    assert len(gs.medical_cases) == 1


def test_register_blocked_during_crisis(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 40
    arm_crisis(eng, "mutiny")
    with pytest.raises(BunkerEngineError):
        eng.register_case(r.id)


# ---- 收治与床位 ----

def test_admit_treating_consumes_bed_and_heals(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 40
    eng.register_case(r.id)
    assert eng.med_beds_used() == 0
    eng.admit_case(r.id, MED_TREATING)
    case = gs.medical_cases[0]
    assert case["status"] == MED_TREATING
    assert eng.med_beds_used() == 1
    power_before = gs.resources[POWER]
    eng._apply_medical_tick()
    # 治疗每日回血（Lv.1 基础 6，无医护），并消耗救治物资（耗电）
    assert r.health > 40
    assert gs.resources[POWER] < power_before
    assert gs.medical_cases[0]["days_cared"] == 1


def test_bed_capacity_scales_with_level_and_medics(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng, level=2)
    # Lv.2：每级 2 床 → 4 床
    assert eng.med_bed_capacity() == 4
    # 指派一名医护：+1 床
    eng.set_job(gs.residents[0].id, "medic")
    assert eng.med_bed_capacity() == 5


def test_admit_rejected_when_beds_full(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng, level=1)  # Lv.1 无医护 → 2 床
    eng.set_job(gs.residents[0].id, "farmer")  # 确保无人当值医护
    assert eng.med_bed_capacity() == 2
    r1, r2, r3 = gs.residents
    open_case_directly(eng, r1, status=MED_TREATING)
    open_case_directly(eng, r2, status=MED_ISOLATED)
    assert eng.med_beds_used() == 2
    c3 = open_case_directly(eng, r3)
    with pytest.raises(BunkerEngineError):
        eng.admit_case(r3.id, MED_TREATING)
    assert c3["status"] == MED_REGISTERED


def test_mode_switch_does_not_consume_new_bed(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng, level=1)
    r1, r2, r3 = gs.residents
    open_case_directly(eng, r1, status=MED_TREATING)
    open_case_directly(eng, r2, status=MED_TREATING)
    c3 = open_case_directly(eng, r3)
    assert eng.med_beds_used() == 2
    # 床位满：第三人不能收治，但在治者可切换治疗/隔离方案
    with pytest.raises(BunkerEngineError):
        eng.admit_case(r3.id, MED_ISOLATED)
    eng.admit_case(r1.id, MED_ISOLATED)
    assert gs.medical_cases[0]["status"] == MED_ISOLATED
    assert eng.med_beds_used() == 2
    assert c3["status"] == MED_REGISTERED


def test_medic_job_boosts_healing(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    eng.set_job(gs.residents[1].id, "medic")  # 1 名医护（患者本人是 0 号）
    r = gs.residents[0]
    r.health = 40
    open_case_directly(eng, r, status=MED_TREATING)
    eng._apply_medical_tick()
    # 基础 6 + 医护 1*1 = 7
    assert round(r.health, 1) == 47.0


def test_caring_resident_excluded_from_jobs(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.job = "farmer"
    open_case_directly(eng, r, status=MED_TREATING)
    # 躺上病床的农民不再计入农业岗位（暂停生产）
    assert eng.job_count("farmer") == 0
    # 登记待收治的病人尚未上床，仍计入岗位（与治疗/隔离口径区分）
    eng2_case = gs.medical_cases[0]
    eng2_case["status"] = MED_REGISTERED
    eng2_case["admitted_day"] = None
    eng._save_cases(gs.medical_cases)
    assert eng.job_count("farmer") == 1



def test_recovery_closes_case(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 72
    open_case_directly(eng, r, status=MED_TREATING)
    eng._apply_medical_tick()
    # 72 + 6 >= 75 康复线，病例收敛为 recovered
    c = gs.medical_cases[0]
    assert c["status"] == MED_RECOVERED
    assert c["close_day"] == gs.day
    assert eng.med_beds_used() == 0


# ---- 未收治病例：恶化与扩散 ----

def test_unadmitted_case_decays(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 30
    open_case_directly(eng, r, infectious=False)
    eng._apply_medical_tick()
    # 普通未收治病例每日 -1.5
    assert round(r.health, 1) == 28.5


def test_infectious_registered_spreads_when_not_isolated(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=AlwaysSpreadRand())
    build_clinic(eng)
    sick, victim = gs.residents[0], gs.residents[1]
    sick.health = 30
    victim.health = 60
    open_case_directly(eng, sick, infectious=True, status=MED_REGISTERED)
    eng._apply_medical_tick()
    # 未隔离传染源：被感染者 -8 → 52（未跌破 35，不建档）
    assert round(victim.health, 1) == 52.0
    assert all(c["resident_id"] != victim.id for c in gs.medical_cases)


def test_spread_registers_case_below_threshold(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=AlwaysSpreadRand())
    build_clinic(eng)
    sick, victim = gs.residents[0], gs.residents[1]
    sick.health = 30
    victim.health = 40
    open_case_directly(eng, sick, infectious=True, status=MED_REGISTERED)
    eng._apply_medical_tick()
    # 40 - 8 = 32 < 35：被感染者立为传染病例（登记态）
    victim_case = next(c for c in gs.medical_cases if c["resident_id"] == victim.id)
    assert victim_case["infectious"] is True
    assert victim_case["status"] == MED_REGISTERED


def test_isolation_blocks_spread(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=AlwaysSpreadRand())
    build_clinic(eng)
    sick, victim = gs.residents[0], gs.residents[1]
    sick.health = 30
    victim.health = 40
    open_case_directly(eng, sick, infectious=True, status=MED_ISOLATED)
    eng._apply_medical_tick()
    # 隔离阻断扩散：受害者健康不变
    assert victim.health == 40
    assert all(c["resident_id"] != victim.id for c in gs.medical_cases)


def test_death_closes_case_and_decrements_survivors(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 3
    survivors_before = gs.survivors
    open_case_directly(eng, r, infectious=True, status=MED_REGISTERED)
    eng._apply_medical_tick()
    # 传染未收治每日 -4：3 - 4 归零 → 病亡
    assert r.alive == 0
    assert gs.survivors == survivors_before - 1
    assert gs.medical_cases[0]["status"] == MED_DECEASED


def test_starved_resident_active_case_is_swept_to_deceased(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 30
    open_case_directly(eng, r, status=MED_TREATING)
    # 模拟匮乏死亡（_apply_starvation_deaths 路径）：人口与存活标志先行扣减
    r.alive = 0
    r.health = 0
    gs.survivors -= 1
    eng._apply_medical_tick()
    assert gs.medical_cases[0]["status"] == MED_DECEASED


# ---- 配给停诊 ----

def test_care_pauses_on_resource_shortage(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 40
    r.morale = 60
    open_case_directly(eng, r, status=MED_TREATING)
    # 电力不足以支付床位耗电（Lv.1 treating 每床 0.8）
    gs.resources = {FOOD: 999, WATER: 999, POWER: 0.2, OXY: 999}
    eng._apply_medical_tick()
    # 停诊：不回血，士气受挫
    assert r.health == 40
    assert r.morale == 55


# ---- 每日巡检自动登记 ----

def test_daily_inspection_auto_registers_critical_resident(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 20
    eng._apply_medical_tick()
    c = next(c for c in gs.medical_cases if c["resident_id"] == r.id)
    assert c["status"] == MED_REGISTERED
    assert c["infectious"] is False


# ---- 离堡冻结 ----

def test_away_case_is_frozen(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 30
    # 先离堡、后经内部途径建档（模拟离堡前已登记、随队伍冻结的病例）
    eng.send_expedition([r.id], {FOOD: 6, WATER: 6})
    c = open_case_directly(eng, r, infectious=True, status=MED_ISOLATED)
    assert eng.med_beds_used() == 0
    summary = eng.med_summary()
    assert summary["frozen_away"] == 1
    # 地堡其余居民健康不受冻结病例影响（直接跑医疗 tick，离堡病例被跳过）
    others = [x for x in gs.residents if x.id != r.id]
    health_before = [x.health for x in others]
    eng._apply_medical_tick()
    assert c["status"] == MED_ISOLATED
    assert [x.health for x in others] == health_before


def test_resident_with_active_case_cannot_join_expedition(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 40
    eng.register_case(r.id)
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([r.id], {FOOD: 5, WATER: 5})


def test_away_resident_cannot_be_admitted(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    r = gs.residents[0]
    r.health = 30
    eng.send_expedition([r.id], {FOOD: 6, WATER: 6})
    c = open_case_directly(eng, r)
    with pytest.raises(BunkerEngineError):
        eng.admit_case(r.id, MED_TREATING)
    assert c["status"] == MED_REGISTERED


# ---- 危机后健康结算 ----

def test_sick_crisis_target_becomes_infectious_case(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    target = gs.residents[0]
    target.health = 80
    crisis = arm_crisis(eng, "sick", target=target)
    # 隔离治疗：目标 -5（single），疫病事件 → 强制建为传染病例
    eng.resolve_crisis("sick", "quarantine", target_id=target.id, token=crisis["token"])
    c = gs.medical_cases[0]
    assert c["resident_id"] == target.id
    assert c["infectious"] is True
    assert c["status"] == MED_REGISTERED


def test_no_clinic_no_case_after_crisis(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    target = gs.residents[0]
    target.health = 80
    crisis = arm_crisis(eng, "sick", target=target)
    eng.resolve_crisis("sick", "quarantine", target_id=target.id, token=crisis["token"])
    assert not gs.medical_cases


def test_non_infectious_crisis_only_registers_severe(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    build_clinic(eng)
    target = gs.residents[0]
    target.health = 30  # 盗匪抵抗 -8 后 22 < 35 → 普通病例
    crisis = arm_crisis(eng, "raid", target=target)
    eng.resolve_crisis("raid", "defend", target_id=target.id, token=crisis["token"])
    c = gs.medical_cases[0]
    assert c["infectious"] is False
    assert c["resident_id"] == target.id


# ---- 终局健康结算 ----

def test_finish_records_medical_stats(db):
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    # 拆除初始医疗舱，避免其被动回复干扰病例的恶化/病亡结算
    for f in [x for x in gs.facilities if x.category == "med"]:
        gs.facilities.remove(f)
        db.delete(f)
    db.flush()
    build_clinic(eng)
    r1, r2 = gs.residents[0], gs.residents[1]
    r1.health = 72
    c1 = open_case_directly(eng, r1, status=MED_TREATING)
    r2.health = 1
    open_case_directly(eng, r2, infectious=True, status=MED_REGISTERED)  # 传染未收治每日 -4 → 病亡
    # 推进到目标日触发终局（医疗 tick 在终局判定前执行：一康复一病亡）
    gs.day = SURVIVAL_TARGET_DAY - 1
    eng.advance_day()
    assert gs.status == "win"
    med = gs.outcome["medical"]
    assert med["total"] == 2
    assert med["recovered"] == 1
    assert med["deceased"] == 1
    assert med["care_days"] == 1  # r1 在终局前最后一次在治结算计 1 床日
    assert "avg_health" in gs.outcome


# ---- 概览 ----

def test_med_summary_counts(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=NoSpreadRand())
    assert eng.med_summary()["has_clinic"] is False
    build_clinic(eng, level=1)
    eng.set_job(gs.residents[0].id, "medic")  # 0 号当值医护，其余两人为病例
    r1, r2, r3 = gs.residents
    open_case_directly(eng, r2, status=MED_TREATING)
    open_case_directly(eng, r3, status=MED_ISOLATED)
    # 第三人即医护本人也登记一例（待收治），不影响在岗计数
    open_case_directly(eng, r1, infectious=True)
    s = eng.med_summary()
    assert s["has_clinic"] is True
    assert s["clinic_level"] == 1
    assert s["medics"] == 1
    assert s["bed_capacity"] == 3  # 2 床 + 1 医护
    assert s["beds_used"] == 2
    assert s["treating"] == 1
    assert s["isolated"] == 1
    assert s["registered"] == 1
