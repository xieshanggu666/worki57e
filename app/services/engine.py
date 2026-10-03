# -*- coding: utf-8 -*-
"""末日地堡生存核心引擎。

资源守恒循环：
  每日净变化 = 设施产出 - 人口消耗 - 运营损耗
  产出受设施等级 + 人力资源(工程师/农夫加成) + 士气系数影响
"""

from sqlalchemy.orm import Session

from ..models import GameSession, Resident, Facility, EventLog
from ..core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY

import uuid

# 资源键
FOOD, WATER, POWER, OXY = "food", "water", "power", "oxygen"
RESOURCE_KEYS = [FOOD, WATER, POWER, OXY]

# 每日人均基础消耗
BASE_CONSUME = {FOOD: 1.5, WATER: 1.3, POWER: 1.0, OXY: 0.8}

# 设施基础产出（等级1）
FACILITY_OUTPUT = {
    "farm": {FOOD: 6.0, POWER: -1.5},   # 菜园产食物，耗电
    "water": {WATER: 7.0, POWER: -1.0}, # 净水器产水，耗电
    "power": {POWER: 8.0},              # 发电机产电
    "oxygen": {OXY: 6.0, POWER: -1.0},  # 水培/制氧耗电产氧
    "med": {},                          # 医疗舱：全体被动回复健康，微耗电
    "clinic": {POWER: -2.0},            # 医疗救治中心：病例收治，运营耗电（随等级放大）
    "storage": {},                      # 仓库：降低损耗
}
FACILITY_LEVEL_SCALE = 1.6  # 升级产出按比例放大
FACILITY_COST = {  # 建造/升级消耗 builder cost
    1: {FOOD: 20, WATER: 10, POWER: 15},
    2: {FOOD: 35, WATER: 18, POWER: 28},
    3: {FOOD: 60, WATER: 30, POWER: 45},
}

# 岗位
JOB_EFFICIENCY = {"engineer": 1.25, "farmer": 1.3, "general": 1.0, "medic": 1.0}

# 危机事件概率
CRISIS_DAY_CHANCE = 0.45

# 探索队系统
EXPEDITION_SUPPLY_PER_DAY = {FOOD: 1.0, WATER: 1.0}  # 每人每日消耗自带物资
EXPEDITION_MAX_DAYS = 7       # 最长探索天数，期满强制返程
EXPEDITION_ENCOUNTER_CHANCE = 0.85  # 每日行军遭遇概率
EXPEDITION_MAX_MEMBERS = 4    # 每支探索队上限

# 贸易救援系统
TRADE_REVIEWING, TRADE_TRANSPORTING = "reviewing", "transporting"
TRADE_DELIVERED, TRADE_FAILED = "delivered", "failed"
TRADE_REJECTED, TRADE_CANCELLED = "rejected", "cancelled"
TRADE_TYPES = ("rescue", "procure")
TRADE_INITIAL_REPUTATION = 50      # 新档案初始信誉
TRADE_MAX_ESCORTS = 3              # 每笔订单押运队上限
TRADE_INCIDENT_CHANCE = 0.55       # 每个在途日触发途中事件的概率
TRADE_REP_MIN, TRADE_REP_MAX = 0, 100

# 医疗救治中心系统：病例状态链 登记(registered) → 治疗(treating)/隔离(isolated)
# → 康复(recovered)/病亡(deceased)。终态病例保留在档案中作为救治履历与健康结算依据。
MED_REGISTERED, MED_TREATING, MED_ISOLATED = "registered", "treating", "isolated"
MED_RECOVERED, MED_DECEASED = "recovered", "deceased"
MED_CARE_STATUSES = (MED_TREATING, MED_ISOLATED)
MED_ACTIVE_STATUSES = (MED_REGISTERED, MED_TREATING, MED_ISOLATED)
MED_MANUAL_REGISTER_HEALTH = 70.0   # 手动登记的健康上限：高于此值视为健康无需登记
MED_AUTO_REGISTER_HEALTH = 35.0     # 每日巡检/事件后自动登记病例的健康线
MED_RECOVER_HEALTH = 75.0           # 康复出院线：在治病例每日结算后跨过此线即康复
MED_HEAL = {MED_TREATING: 6.0, MED_ISOLATED: 4.0}          # 每日基础救治回复
MED_MEDIC_HEAL = {MED_TREATING: 1.0, MED_ISOLATED: 0.8}   # 每名当值医护的额外回复
MED_DECAY = {True: 4.0, False: 1.5}  # 登记未收治病例每日恶化：{传染: 4, 普通: 1.5}
# 每张在治病床的每日资源消耗（治疗/隔离两档）
MED_CARE_COST = {
    MED_TREATING: {FOOD: 0.6, WATER: 0.5, POWER: 0.8},
    MED_ISOLATED: {FOOD: 0.8, WATER: 0.7, POWER: 1.2},
}
MED_BEDS_PER_LEVEL = 2             # 救治中心每级提供的床位（另 + 当值医护人数）
MED_SPREAD_CHANCE = 0.35           # 未隔离传染病例每日传播基础概率
MED_SPREAD_MEDIC_REDUCE = 0.05     # 每名当值医护降低的传播概率
MED_SPREAD_MIN_CHANCE = 0.10       # 传播概率下限（医护再多也不归零）
MED_SPREAD_DAMAGE = 8.0            # 传播对被感染者造成的即时健康损伤
# 各类抉择事件中具备传染性的事件 key：受袭者健康跌到登记线以下即立为传染病例
MED_INFECTIOUS_CRISIS_EVENTS = {"sick"}
MED_INFECTIOUS_ENCOUNTER_EVENTS = {"weather"}
MED_INFECTIOUS_INCIDENT_EVENTS = {"duststorm"}


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _rng():
    """简单投影式随机数，便于测试时可注入 seed。"""
    import random
    return random.Random()


class BunkerEngineError(Exception):
    pass


class BunkerEngineConflict(BunkerEngineError):
    """并发冲突（乐观锁版本不匹配），HTTP 层映射为 409。"""


# 档案状态机阶段：
#   daily  —— 每日阶段，可建造/升级/调岗，可推进一天
#   crisis —— 危机阶段，存在待处理危机，除结算危机外拒绝一切推进与经营动作
#   ended  —— 终局（win/over），拒绝任何状态变更
PHASE_DAILY, PHASE_CRISIS, PHASE_ENDED = "daily", "crisis", "ended"
# 探索阶段：探索队在外且存在待处理遭遇，状态机拒绝一切经营/推进动作
PHASE_EXPEDITION = "expedition"
# 贸易阶段：押运队在途且存在待处理途中事件，状态机拒绝一切经营/推进动作
PHASE_TRADE = "trade"


class BunkerEngine:
    def __init__(self, db: Session, session: GameSession, rand=None):
        self.db = db
        self.session = session
        self.rand = rand or _rng()

    # ---- 状态机 ----
    @property
    def phase(self):
        if self.session.status != "running":
            return PHASE_ENDED
        if self.session.pending_crisis:
            return PHASE_CRISIS
        exp = self.session.expedition
        if exp and exp.get("status") == "away" and exp.get("pending_encounter"):
            return PHASE_EXPEDITION
        order = self.session.trade_order
        if order and order.get("status") == TRADE_TRANSPORTING and order.get("pending_incident"):
            return PHASE_TRADE
        return PHASE_DAILY

    def _require_phase(self, phase, message):
        if self.phase != phase:
            raise BunkerEngineError(message)

    # ---- 资源查询 ----
    def get_resources(self):
        return self.session.resources or {k: 0 for k in RESOURCE_KEYS}

    def _set_resource(self, key, val):
        # 复制后整体回写，确保 JSON 列的变更被 SQLAlchemy 追踪并落库
        res = dict(self.session.resources or {k: 0 for k in RESOURCE_KEYS})
        res[key] = round(max(0.0, val), 1)
        self.session.resources = res

    def _add_resource(self, key, delta):
        res = self.session.resources or {k: 0 for k in RESOURCE_KEYS}
        cur = res.get(key, 0.0)
        nxt = max(0.0, cur + delta)
        new_res = dict(res)
        new_res[key] = round(nxt, 1)
        self.session.resources = new_res
        return nxt

    # ---- 设施 ----
    def facility_output(self, facility: Facility):
        base = FACILITY_OUTPUT.get(facility.category, {})
        mult = FACILITY_LEVEL_SCALE ** (facility.level - 1)
        out = {k: v * mult for k, v in base.items()}
        # 农夫/工程师提升产出设施
        if facility.category in ("farm", "oxygen") and self.job_count("farmer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("farmer")
        if facility.category == "power" and self.job_count("engineer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("engineer")
        return out

    def job_count(self, job):
        away = self._away_resident_ids()
        caring = self._caring_resident_ids()
        # 在治病床上的治疗/隔离病例暂停生产：既不算在堡劳动力，也不再产出岗位加成
        return sum(
            1 for r in self.session.residents
            if r.alive and r.job == job and r.id not in away and r.id not in caring
        )

    def active_facilities(self):
        return [f for f in self.session.facilities if f.status == "active"]

    # ---- 离堡成员追踪（探索队 + 贸易押运队）----
    def _away_resident_ids(self):
        """当前离堡居民编号（无论生死）：探索队编制 + 在途押运队。

        订单处于 reviewing（审核中）时押运队尚未出发，仍在堡内正常生产/消耗；
        仅 transporting（在途）才按离堡口径结算。
        """
        ids = set()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            ids.update(exp.get("members", []))
        order = self.session.trade_order
        if order and order.get("status") == TRADE_TRANSPORTING:
            ids.update(order.get("escorts", []))
        return ids

    def _away_residents(self):
        """探索队编制内的全部居民（含已阵亡，用于返程结算）。"""
        ids = set()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            ids.update(exp.get("members", []))
        return [r for r in self.session.residents if r.id in ids]

    def _trade_escorts(self, order=None):
        """当前贸易押运队的全部居民（含已阵亡，用于交付/回退结算）。"""
        order = order or self.session.trade_order
        if not order:
            return []
        ids = set(order.get("escorts", []))
        return [r for r in self.session.residents if r.id in ids]

    def _in_bunker_residents(self):
        """地堡内存活居民（排除探索队成员）。"""
        away = self._away_resident_ids()
        return [r for r in self.session.residents if r.alive and r.id not in away]

    def _in_bunker_count(self):
        return len(self._in_bunker_residents())

    # ---- 每日推进 ----
    def advance_day(self):
        # 终局或存在待处理抉择（危机/探索遭遇）时都不能推进：抉择不可被"再点一天"跳过
        self._require_phase(PHASE_DAILY, "存在待处理抉择，必须先完成才能推进")
        self.session.day += 1
        # 在任何产出/探索队结算之前快照当日终局裁决：抵达目标日立即胜利；
        # 若推进前已全线枯竭，随后的当日产出或探索队带回的战利品/余粮
        # 都不得把败局“救回”——终局在当天只收敛一次
        pre_verdict = self._end_verdict()
        self._apply_production_and_consumption()
        self._apply_health_morale()
        # 医疗救治中心每日健康结算：病例治疗/隔离消耗、传染扩散、康复病亡收敛。
        # 离堡成员的病例随队伍冻结，只结算在堡病例，与产出/口粮同一口径
        self._apply_medical_tick()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            # 探索队在外出差：地堡按在堡人口结算，探索队消耗自带物资、行军并触发遭遇
            self._apply_expedition_travel(exp, pre_verdict=pre_verdict)
            # 强制返程（补给耗尽/期满/全员失联）会清除探索队状态：
            # 此时不得再用旧 exp 触发遭遇，否则会把已结算的队伍恢复成"在外"
            if self.session.expedition is None:
                self._check_end(forced_verdict=pre_verdict)
                return None
            # 终局优先：抵达目标日胜利，或地堡因在堡匮乏/人口归零失败时，
            # 在外队伍先安全返程（战利品入库、剩余物资归还、幸存者归队），
            # 再统一收敛到 ended——绝不在 ended 档案上留下无法处理的"僵尸队伍"
            if pre_verdict is not None or self._end_conditions_met():
                self._settle_expedition(
                    self.session.expedition, reason="终局已至，探索队返程"
                )
                self._check_end(forced_verdict=pre_verdict)
                return None
            # 探索队行军中：触发遭遇（替代地堡危机），遭遇挂起后进入 expedition 阶段
            return self._maybe_trigger_expedition_encounter(self.session.expedition)
        order = self.session.trade_order
        if order and order.get("status") in (TRADE_REVIEWING, TRADE_TRANSPORTING):
            # 贸易订单推进：审核（reviewing→transporting/rejected）或在途运输
            # （travel_days 累加 → 途中事件 / 抵达交付 / 失败回退）。
            # 与探索队同一口径：终局裁决先快照，成功入库的回礼也不得复活败局
            incident = self._progress_trade_order(order, pre_verdict=pre_verdict)
            if self.session.trade_order is None:
                # 订单已收敛（驳回/交付/失败回退）
                self._check_end(forced_verdict=pre_verdict)
                return None
            if incident is not None:
                # 途中事件挂起：进入 trade 阶段，替代当日地堡危机
                return incident
            if pre_verdict is not None or self._end_conditions_met():
                # 在途订单遇终局：强制安全交付（回礼/退款先入库、押运队归队），
                # 再统一收敛到 ended，不留"僵尸订单"
                self._deliver_trade_order(
                    self.session.trade_order, reason="终局已至，押运队返程",
                    forced_verdict=pre_verdict, force_success=True,
                )
                self._check_end(forced_verdict=pre_verdict)
                return None
        # 终局优先：抵达目标日或全面崩溃直接结算结局，不再凭空挂起一个
        # 永远无法处理的危机（统一每日推进 → 危机处理 → 终局的流转）
        if self._check_end(forced_verdict=pre_verdict):
            return None
        return self._maybe_trigger_crisis()

    def _end_conditions_met(self):
        """只判定终局条件、不写终局状态（用于终局前的探索队返程收敛）。"""
        return self._end_verdict() is not None

    def _end_verdict(self):
        """当前状态对应的终局裁决：返回 None（未终局）或 (win, reason)。

        纯判定、不写状态。返程结算在战利品/余粮入库前先快照一次裁决，
        保证“全线枯竭”的败局不会被随后入库的战利品抬过阈值而“复活”，
        终局状态只收敛一次且与结算路径（主动返程/强制返程/遭遇收敛）无关。
        """
        if self.session.day >= self.session.target_day:
            return True, f"坚持到第{self.session.day}天，末日阴影散去，幸存者们走向了新生。"
        if self.session.survivors <= 0:
            return False, "所有幸存者都已逝去，地堡陷入永恒的寂静。"
        res = self.get_resources()
        if all(res.get(k, 0) <= 1 for k in RESOURCE_KEYS):
            return False, "食物、水源、电力和氧气全线枯竭，地堡无法再维系生命。"
        return None

    def _apply_production_and_consumption(self):
        # 离堡人员不消耗地堡物资（吃自带口粮），地堡消耗只计在堡人口
        pop = self._in_bunker_count()
        # 士气系数(在堡人员平均士气)：低士气降低产出；探索队在外不参与地堡生产
        avg_morale = self.avg_morale(in_bunker_only=True)
        morale_factor = 0.6 + 0.4 * (avg_morale / 100.0)

        # 消耗
        consume = {}
        for k in RESOURCE_KEYS:
            consume[k] = BASE_CONSUME[k] * pop

        # 产出（累计设施净产）
        prod = {k: 0.0 for k in RESOURCE_KEYS}
        for f in self.active_facilities():
            for k, v in self.facility_output(f).items():
                prod[k] += v * morale_factor

        # 应用净变化（消耗优先，产出后）
        for k in RESOURCE_KEYS:
            net = prod.get(k, 0.0) - consume[k]
            self._add_resource(k, net)

        # 日志
        self._log(
            "update",
            f"第{self.session.day}天 · 生存更新",
            f"人口{pop}，食物净变{round(consume[FOOD]-prod[FOOD],1):+}、水{round(consume[WATER]-prod[WATER],1):+}、电力{round(consume[POWER]-prod[POWER],1):+}、氧气{round(consume[OXY]-prod[OXY],1):+}",
            decision="例行更新",
        )

    def _apply_health_morale(self):
        res = self.get_resources()
        away = self._away_resident_ids()
        # 在治病例（治疗/隔离中）的健康由医疗救治中心每日结算统一处理，
        # 这里跳过医疗舱被动回复，避免一份伤势回两次血
        caring = self._caring_resident_ids()
        # 资源不足影响（仅作用于在堡居民；探索队吃自带物资，不受地堡短缺波及）
        for r in self.session.residents:
            if not r.alive:
                continue
            if r.id in away:
                continue
            morale = r.morale
            # 资源不足影响
            for k, name in ((FOOD, "食物"), (WATER, "水源"), (OXY, "氧气"), (POWER, "电力")):
                if res.get(k, 0) <= 15:
                    morale -= 2.0
            # 医疗站回复 + 保持士气（在治病例的健康改由医疗结算处理）
            if self.has_category("med") and r.id not in caring:
                if r.health < 100:
                    r.health = _clamp(r.health + 1.2)
            # 低健康拖累士气
            if r.health < 30:
                morale -= 3.0
            # 士气自然衰减/恢复向基准 75
            if morale < 75:
                morale += 0.5
            elif morale > 80:
                morale -= 0.3
            r.morale = _clamp(morale)
        # 去除最严重短缺导致的死亡
        self._apply_starvation_deaths()

    def has_category(self, cat):
        return any(f.category == cat and f.status == "active" for f in self.session.facilities)

    def _apply_starvation_deaths(self):
        res = self.get_resources()
        critical = [k for k in RESOURCE_KEYS if res.get(k, 0) <= 0]
        if not critical:
            return
        # 每日最多因匮乏死 1 人，依次从在堡最弱居民开始（探索队不在堡内，不参与地堡匮乏判定）
        alive = self._in_bunker_residents()
        if not alive:
            return
        weakest = min(alive, key=lambda r: r.health)
        weakest.alive = 0
        weakest.health = 0
        self.session.survivors -= 1
        self._log("crisis", "生存危机：资源耗尽", f"{weakest.name} 因匮乏失去生命。", decision="自然事件")

    def avg_morale(self, in_bunker_only=False):
        """平均士气。

        in_bunker_only=True（地堡设施产出加成）只统计在堡存活居民：
        探索队在外时其士气不参与地堡生产结算；终局评分等全局口径仍统计全体存活者。
        """
        if in_bunker_only:
            alive = self._in_bunker_residents()
        else:
            alive = [r for r in self.session.residents if r.alive]
        if not alive:
            return 0.0
        return sum(r.morale for r in alive) / len(alive)

    def _log(self, etype, title, detail, decision=None):
        self.db.add(
            EventLog(
                session_id=self.session.id,
                day=self.session.day,
                event_type=etype,
                title=title,
                detail=detail,
                decision=decision,
            )
        )

    # ---- 危机轮盘 ----

    @staticmethod
    def _effect_scope(effect):
        """健康/士气效果的作用域：'single' 仅目标本人，'all' 全体存活者。

        数字简写默认为全体；单体效果须显式声明
        {"value": -5, "target": "single"}。
        """
        if isinstance(effect, dict):
            return effect.get("target", "all")
        return "all"

    @staticmethod
    def _effect_value(effect):
        return effect["value"] if isinstance(effect, dict) else effect

    def _event_needs_target(self, event):
        """事件是否存在只作用于单个居民的决策；只有这类事件才随机目标。"""
        for c in event["choices"]:
            effects = c.get("effects", {})
            for stat in ("health", "morale"):
                if stat in effects and self._effect_scope(effects[stat]) == "single":
                    return True
        return False

    def _maybe_trigger_crisis(self):
        if self.rand.random() > CRISIS_DAY_CHANCE:
            return None
        event = self.rand.choice(CRISIS_POOL)
        crisis = self._build_crisis(event)
        # 待处理危机整体写入存档：事件、目标、选项与一次性 token 一起绑定，
        # 刷新页面后凭档案即可恢复同一个决策
        self.session.pending_crisis = crisis
        return crisis

    def _build_crisis(self, event):
        # 仅当事件存在单体效果的决策时才抽取受影响居民；
        # 全体事件不产生目标，前端也无从回传 target_id。
        # 目标只从"在堡存活居民"中抽取：探索队外出期间不受地堡危机波及
        needs_target = self._event_needs_target(event)
        alive = self._in_bunker_residents()
        target = self.rand.choice(alive) if needs_target and alive else None
        return {
            "token": uuid.uuid4().hex,  # 本次待处理危机的一次性凭据
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    @classmethod
    def _choice_targeted(cls, choice):
        """该决策是否含只作用于目标本人的健康/士气效果。"""
        effects = choice.get("effects", {})
        return any(
            cls._effect_scope(effects[stat]) == "single"
            for stat in ("health", "morale")
            if stat in effects
        )

    def _ensure_running(self):
        """结算边界：游戏结束后拒绝一切状态变更。"""
        if self.session.status != "running":
            raise BunkerEngineError("游戏已结束，无法执行该操作")

    def _require_daily_phase(self, action):
        """经营/推进类动作只允许在每日阶段执行。"""
        self._ensure_running()
        if self.phase == PHASE_CRISIS:
            raise BunkerEngineError(f"存在待处理危机，必须先完成抉择才能{action}")
        if self.phase == PHASE_EXPEDITION:
            raise BunkerEngineError(f"存在待处理探索遭遇，必须先完成抉择才能{action}")
        if self.phase == PHASE_TRADE:
            raise BunkerEngineError(f"存在待处理途中事件，必须先完成抉择才能{action}")

    def _pending_event(self):
        """取出当前待处理危机对应的事件定义；存档损坏时视为无法结算。"""
        pending = self.session.pending_crisis
        if not pending:
            return None, None
        event_key = pending.get("event")
        event = next((e for e in CRISIS_POOL if e["key"] == event_key), None)
        if event is None:
            raise BunkerEngineError("待处理危机已失效，请刷新档案后重试")
        return pending, event

    @staticmethod
    def _matches_resolution(rec, event_key, choice_key, target_id, day=None):
        """判断落败/重试请求是否就是上一次已完成的那次结算（幂等回放）。

        除事件/选项/目标外还核对危机发生日，避免不同天的同类型危机被误重放；
        day 为 None（调用方拿不到上下文）时退化为不校验天数。
        """
        if not rec or rec.get("event") != event_key or rec.get("choice") != choice_key:
            return False
        if day is not None and rec.get("day") is not None and rec.get("day") != day:
            return False
        return (rec.get("target_id") or None) == (target_id or None)

    def _resolve_target(self, target_id, required):
        """统一解析目标居民。

        - required=True（所选决策含单体效果）：必须显式给出目标，且目标归属
          当前档案并存活；跨档案编号、不存在、已故或缺席一律报错。
        - required=False（全体/资源类决策）：忽略客户端传入的目标，返回 None，
          效果按全体结算，前端回传谁都不会把全体效果收窄成单体。
        """
        if not required:
            return None
        if target_id is None:
            raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
        target = next((r for r in self.session.residents if r.id == target_id), None)
        if target is None:
            raise BunkerEngineError("目标居民不存在或不属于当前档案")
        if not target.alive:
            raise BunkerEngineError("目标居民已故，无法作为效果目标")
        return target

    def resolve_crisis(self, event_key, choice_key, target_id=None, token=None):
        """结算待处理危机。

        结算必须命中档案里唯一的待处理危机：事件、选项、单体目标都与存档绑定，
        既不能凭空伪造一场危机（无待处理危机时拒绝），也不能重复结算
        （结算后待处理危机被清除并留下幂等凭据，重放只返回上次结果）。
        返回 (detail, replayed)：replayed=True 表示这是重复请求，未再次施加效果。
        """
        self._ensure_running()
        pending, event = self._pending_event()

        # 已有同一危机（事件/选项/目标/发生日一致）的结算记录：
        # 重复提交（含并发落败方）只回放，不二次结算
        pending_day = pending.get("day") if pending else None
        if self._matches_resolution(
            self.session.last_resolution, event_key, choice_key, target_id, day=pending_day
        ):
            return self.session.last_resolution.get("detail", ""), True

        if pending is None:
            raise BunkerEngineError("当前没有待处理的危机，无法结算")

        # 事件必须与存档中的待处理危机一致：不能用 A 事件的请求去结算 B
        if event_key != pending.get("event"):
            raise BunkerEngineError("危机事件与当前待处理事件不符")
        # token 用于区分“同一危机上一次的旧点击”与刷新后恢复的当前决策；
        # 旧客户端/旧档案没有 token 时退化为仅按事件匹配
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该危机决策已过期，请按当前危机重新选择")

        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")

        effects = choice.get("effects", {})

        # 作用域由所选决策的效果声明决定，客户端传入的 target_id 不能改变它：
        # 单体效果必须携带有效目标，全体效果一律忽略客户端目标
        targeted = self._choice_targeted(choice)
        if targeted:
            # 目标与待处理危机绑定：不能用任意/其他居民编号替换事件目标
            bound_id = pending.get("target_id")
            if target_id is None:
                raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
            if bound_id is not None and target_id != bound_id:
                raise BunkerEngineError("目标居民与本次危机指定的幸存者不符")
        # 在应用任何效果前完成目标校验，保证失败时档案状态不发生部分变更
        target = self._resolve_target(target_id, required=targeted)

        detail_parts = []

        # 资源效果
        for k, v in effects.get("resources", {}).items():
            self._add_resource(k, v)
            detail_parts.append(f"{RESOURCE_ZH.get(k,k)} {v:+.0f}")
        # 健康/士气效果：single 只作用于目标本人，all 作用于在堡全体存活者
        # （探索队外出期间不参与地堡危机结算，与每日短缺/生产口径一致）
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool = [target]
                scope = f"仅{target.name}"
            else:
                pool = self._in_bunker_residents()
                scope = "全体"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        if "add_resident" in effects:
            self._add_resident(effects["add_resident"])
            detail_parts.append(f"加入新幸存者 {effects['add_resident']}")
        if "reputation" in effects:
            rep = self._add_reputation(effects["reputation"])
            detail_parts.append(f"信誉 {effects['reputation']:+d}（现 {rep}）")
        if effects.get("trap"):
            detail_parts.append("（不良后果）")

        # 危机后健康结算：受伤/染疫居民由医疗救治中心承接为病例（未建中心则忽略）
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else self._in_bunker_residents()
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_CRISIS_EVENTS,
                reason=event["title"], force=True,
            )

        # 日志与实际结算同一作用域：单体写名，全体写明“全体幸存者”
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", event["title"], f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])

        # 清除待处理危机并记下幂等凭据——无论后续是否终局，本危机都已结算
        self.session.pending_crisis = None
        self.session.last_resolution = {
            "token": pending.get("token"),
            "event": event["key"],
            "choice": choice["key"],
            "target_id": target.id if targeted else None,
            "day": pending.get("day"),
            "detail": detail,
        }
        self._check_end()
        return detail, False

    def reconcile_stale_resolution(self, event_key, choice_key, target_id, token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次结算则安全回放。

        返回 (detail, replayed)；请求与任何已知结算都对不上时抛 409，
        由调用方提示“危机状态已变化”，杜绝并发重复结算。
        """
        rec = self.session.last_resolution
        if self._matches_resolution(rec, event_key, choice_key, target_id) and (
            token is None or not rec.get("token") or token == rec.get("token")
        ):
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("危机状态已被其他请求更新，请刷新后重试")

    def _add_resident(self, name):
        r = Resident(
            session_id=self.session.id,
            name=name,
            job="general",
            health=70.0,
            morale=60.0,
            alive=1,
            joined_day=self.session.day,
        )
        # 挂到关系集合：residents 已加载时仅 add+flush 不会让新成员出现在内存集合
        self.session.residents.append(r)
        self.db.add(r)
        self.session.survivors += 1

    # ---- 探索队 ----
    def _random_survivor_name(self):
        import random
        surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许")
        givens = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英华玉萍红斌")
        return random.choice(surnames) + random.choice(givens)

    def send_expedition(self, member_ids, supplies):
        """派遣探索队：选择在堡居民并分配自带物资，队伍出发后暂停地堡生产。

        离堡人员不参与设施产出、不消耗地堡口粮；行军消耗自带物资，
        途中遭遇由玩家抉择，返程时统一结算战利品与伤亡。
        """
        self._ensure_running()
        if self.phase != PHASE_DAILY:
            raise BunkerEngineError("当前状态无法派遣探索队")
        if self.session.expedition:
            raise BunkerEngineError("已有探索队在外，无法同时派遣第二支队伍")
        if self.session.trade_order:
            raise BunkerEngineError("已有在谈/在途贸易订单，无法同时派遣探索队")
        if not member_ids:
            raise BunkerEngineError("必须选择至少一名居民参加探索队")
        if len(set(member_ids)) != len(member_ids):
            raise BunkerEngineError("同一名居民不能重复编入探索队")
        if len(member_ids) > EXPEDITION_MAX_MEMBERS:
            raise BunkerEngineError(f"探索队最多 {EXPEDITION_MAX_MEMBERS} 人")
        # 校验队员：必须是在堡存活居民
        members = []
        for mid in member_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("队员不存在或已故，无法参加探索队")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已在探索队中")
            if self._has_active_case(r.id):
                raise BunkerEngineError(f"{r.name} 有未结病例，无法离堡参加探索队")
            members.append(r)
        # 校验并扣除自带物资
        supply_cost = {}
        for k, v in (supplies or {}).items():
            if k not in RESOURCE_KEYS:
                raise BunkerEngineError(f"未知物资 {k}")
            if v < 0:
                raise BunkerEngineError("物资数量不能为负")
            supply_cost[k] = float(v)
        if not self._can_afford(supply_cost):
            raise BunkerEngineError("物资不足，无法派遣")
        for k, v in supply_cost.items():
            self._add_resource(k, -v)
        # 写入探索队快照（含一次性 token，刷新后恢复同一支队伍）
        exp = {
            "token": uuid.uuid4().hex,
            "status": "away",
            "started_day": self.session.day,
            "members": [r.id for r in members],
            "supplies": dict(supply_cost),
            "travel_days": 0,
            "encounters_resolved": 0,
            "pending_encounter": None,
            "loot": {},
            "casualties": [],
        }
        self.session.expedition = dict(exp)
        names = "、".join(r.name for r in members)
        self._log("system", "探索队出发", f"{names} 携带物资外出探索。", decision="派遣探索队")
        return exp

    def _apply_expedition_travel(self, exp, pre_verdict=None):
        """探索队每日行军：消耗自带物资、累计天数，触发强制返程判定。

        pre_verdict 为当日推进开始时快照的终局裁决（如已全线枯竭），
        透传给返程结算，避免行军/入库把当日败局“救回”。
        """
        alive_members = [r for r in self._away_residents() if r.alive]
        if not alive_members:
            # 全员失联：强制返程（无人生还）
            self._settle_expedition(exp, reason="探索队全员失联", pre_verdict=pre_verdict)
            return
        exp["travel_days"] = exp.get("travel_days", 0) + 1
        # 消耗自带口粮（按存活人数；阵亡者不再消耗）
        n = len(alive_members)
        supplies = exp.get("supplies", {})
        for k in (FOOD, WATER):
            cost = EXPEDITION_SUPPLY_PER_DAY[k] * n
            supplies[k] = round(max(0.0, supplies.get(k, 0.0) - cost), 1)
        exp["supplies"] = supplies
        # 物资耗尽或达到最长探索天数：当日强制返程，补给不得出现负值快照
        if supplies.get(FOOD, 0) <= 0 or supplies.get(WATER, 0) <= 0:
            self._settle_expedition(
                exp, reason="补给耗尽，探索队被迫返程", pre_verdict=pre_verdict
            )
            return
        if exp["travel_days"] >= EXPEDITION_MAX_DAYS:
            self._settle_expedition(
                exp, reason="探索期满，探索队返程", pre_verdict=pre_verdict
            )
            return
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)

    def _maybe_trigger_expedition_encounter(self, exp):
        """每日行军后概率触发遭遇；已有待处理遭遇时不重复触发。"""
        if exp.get("pending_encounter"):
            return exp["pending_encounter"]
        if self.rand.random() > EXPEDITION_ENCOUNTER_CHANCE:
            return None
        event = self.rand.choice(EXPEDITION_ENCOUNTERS)
        encounter = self._build_expedition_encounter(event, exp)
        exp["pending_encounter"] = encounter
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)
        return encounter

    def _build_expedition_encounter(self, event, exp):
        alive_members = [r for r in self._away_residents() if r.alive]
        # 仅当存在单体健康效果的决策时才随机目标队员
        needs_target = any(
            self._choice_targeted(c) for c in event["choices"]
        )
        target = self.rand.choice(alive_members) if needs_target and alive_members else None
        return {
            "token": uuid.uuid4().hex,
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    # 探索队动作类型（用于档案级幂等凭据 last_expedition）
    _EXP_ACT_ENCOUNTER = "encounter"
    _EXP_ACT_RETURN = "return"

    @staticmethod
    def _matches_expedition(rec, action, token, exp_token=None, choice_key=None):
        """判断落败/重试请求是否就是上一次已完成的那次探索队动作（幂等回放）。

        - 遭遇：action=encounter，token=遭遇 token，再核对选项
        - 返程：action=return，exp_token=队伍 token（返程凭据挂在队伍上）
        """
        if not rec or rec.get("action") != action:
            return False
        if token is not None and rec.get("token") and token != rec["token"]:
            return False
        if action == "return" and exp_token is not None and rec.get("exp_token") and exp_token != rec["exp_token"]:
            return False
        if choice_key is not None and rec.get("choice") is not None and choice_key != rec["choice"]:
            return False
        return True

    def _last_expedition_replay(self, action, token, exp_token=None, choice_key=None):
        """命中档案级幂等记录则返回 (detail, True)，否则返回 (None, False)。"""
        rec = self.session.last_expedition
        if self._matches_expedition(rec, action, token, exp_token=exp_token, choice_key=choice_key):
            return rec.get("detail", ""), True
        return None, False

    def _encounter_settlement_replay(self, token, choice_key=None):
        """遭遇请求命中“由该遭遇直接触发的返程结算”凭据时安全回放。

        遭遇结算后若队伍当场收敛（补给耗尽/全员阵亡/终局），档案级凭据会被
        返程记录覆盖，但该记录仍挂着本次遭遇的一次性 token。并发落败或连点
        凭 token 命中这里：回放返程结算明细，绝不二次入库战利品。
        """
        rec = self.session.last_expedition
        if not token or not rec or rec.get("action") != self._EXP_ACT_RETURN:
            return None, False
        if rec.get("token") != token:
            return None, False
        if choice_key is not None and rec.get("choice") is not None and choice_key != rec["choice"]:
            return None, False
        return rec.get("detail", ""), True

    def _remember_expedition(self, action, token, detail, exp_token=None, choice_key=None):
        """把已完成的探索队动作写入档案级幂等凭据。

        队伍随后可能被清除（返程）或继续在外（遭遇），凭据独立保存在档案上，
        使并发落败/连点请求在队伍消失后仍能被识别并安全回放。
        """
        self.session.last_expedition = {
            "action": action,
            "token": token,
            "exp_token": exp_token,
            "choice": choice_key,
            "day": self.session.day,
            "detail": detail,
        }

    def resolve_expedition_encounter(self, choice_key, token=None):
        """处理探索队途中遭遇：抉择影响队员健康/士气、物资与战利品。

        结算必须命中央档案里唯一的待处理遭遇：事件、选项、单体目标都与存档绑定，
        token 用于识别过期/重复请求；结算后待处理遭遇被清除。
        返回 (detail, replayed)：replayed=True 表示重复/并发落败请求，未再次施加效果。
        """
        self._ensure_running()
        # 幂等回放优先：遭遇结算后、下一个探索队动作前的连点/并发落败只回放。
        # 若档案级凭据已被后续动作（如返程）覆盖，说明遭遇所属状态已前进，
        # 落到下方的“无在外队伍/无待处理遭遇”分支并按 409 拒绝
        replay = self._last_expedition_replay(
            self._EXP_ACT_ENCOUNTER, token, choice_key=choice_key
        )
        if replay[0] is not None:
            return replay
        # 遭遇已直接触发队伍收敛（补给耗尽/全员阵亡/终局）：凭据已被返程记录
        # 覆盖，但记录上仍挂着本次遭遇 token，命中则回放返程明细而非 409
        converged = self._encounter_settlement_replay(token, choice_key=choice_key)
        if converged[0] is not None:
            return converged
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            # 携带遭遇凭据却找不到在外队伍：队伍已被其他请求召回，状态已前进
            if token:
                raise BunkerEngineConflict("探索队状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有在外的探索队")
        pending = exp.get("pending_encounter")
        if not pending:
            # 队伍仍在但该遭遇已被其他请求结算：重复请求安全拒绝并引导刷新
            if token:
                raise BunkerEngineConflict("该遭遇已被处理，请刷新后重试")
            raise BunkerEngineError("当前没有待处理的探索遭遇")
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该遭遇决策已过期，请刷新后重试")
        event_key = pending.get("event")
        event = next((e for e in EXPEDITION_ENCOUNTERS if e["key"] == event_key), None)
        if not event:
            raise BunkerEngineError("探索遭遇已失效，请刷新档案后重试")
        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")
        effects = choice.get("effects", {})
        # 单体目标校验：必须是队内存活队员，且与待处理遭遇绑定
        targeted = self._choice_targeted(choice)
        target = None
        if targeted:
            bound_id = pending.get("target_id")
            if bound_id is None:
                raise BunkerEngineError("该决策需要指定一名队员作为目标")
            target = next((r for r in self._away_residents() if r.id == bound_id), None)
            if not target or not target.alive:
                raise BunkerEngineError("目标队员不在队中或已故，无法作为效果目标")
        # 在应用任何效果前完成校验，保证失败时档案状态不发生部分变更
        detail_parts = []
        exp.setdefault("casualties", [])
        alive_members = [r for r in self._away_residents() if r.alive]
        # 战利品（单独累计，返程时统一入库）
        loot = exp.get("loot", {})
        for k, v in effects.get("loot", {}).items():
            loot[k] = round(loot.get(k, 0.0) + v, 1)
            detail_parts.append(f"战利品 {RESOURCE_ZH.get(k, k)} +{v:g}")
        exp["loot"] = loot
        # 物资损失（从探索队自带物资中扣除，不为负）
        supplies = exp.get("supplies", {})
        for k, v in effects.get("supply_loss", {}).items():
            supplies[k] = round(max(0.0, supplies.get(k, 0.0) - v), 1)
            detail_parts.append(f"物资损失 {RESOURCE_ZH.get(k, k)} -{v:g}")
        exp["supplies"] = supplies
        # 健康/士气：单体作用于目标队员，全体作用于队内存活者
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool = [target]
                scope = f"仅{target.name}"
            else:
                pool = alive_members
                scope = "全体队员"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        # 全部效果施加完毕后统一收敛伤亡：健康归零即阵亡。单体/全体效果
        # （以及遭遇前已濒死、被本次效果带过零点的队员）在同一次扫描中处理，
        # 保证人口只扣一次、casualties 不重不漏
        casualties = exp.get("casualties", [])
        for r in alive_members:
            if r.health <= 0 and r.alive:
                r.alive = 0
                r.health = 0
                if r.id not in casualties:
                    casualties.append(r.id)
                    self.session.survivors = max(0, self.session.survivors - 1)
        exp["casualties"] = casualties
        # 健康结算：受伤队员登记为病例，随队伍冻结，回堡后续治
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else [
                r for r in alive_members if r.alive
            ]
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_ENCOUNTER_EVENTS,
                reason=f"探索遭遇·{event['title']}", force=True,
            )
        # 偶遇幸存者加入队伍
        if effects.get("add_resident"):
            name = self._random_survivor_name()
            self.db.flush()
            r = Resident(
                session_id=self.session.id, name=name, job="general",
                health=60.0, morale=50.0, alive=1, joined_day=self.session.day,
            )
            self.session.residents.append(r)
            self.db.add(r)
            self.db.flush()  # 取得新居民 id
            exp["members"].append(r.id)
            self.session.survivors += 1
            detail_parts.append(f"新幸存者 {name} 加入队伍")
        # 日志与实际结算同一作用域
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"探索遭遇·{event['title']}", f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        # 清除待处理遭遇、写入档案级幂等凭据，队伍继续在外行军
        enc_token = pending.get("token")
        exp["pending_encounter"] = None
        exp["encounters_resolved"] = exp.get("encounters_resolved", 0) + 1
        self.session.expedition = dict(exp)
        self._remember_expedition(
            self._EXP_ACT_ENCOUNTER, enc_token, detail,
            exp_token=exp.get("token"), choice_key=choice["key"],
        )
        # 遭遇结算后立即收敛，不把“零补给 / 全员阵亡 / 人口归零”的队伍留给下一步：
        #   1) 全员阵亡 —— 无人生还的队伍不能继续行军（僵尸队伍）
        #   2) 自带补给耗尽 —— 无需再等一次“推进一天”，当场被迫返程
        #   3) 人口归零/抵达目标日等终局 —— 先安全返程再收敛到 ended
        # 返程结算内部会在战利品入库前快照终局裁决，收敛只会发生一次
        alive_after = [r for r in self._away_residents() if r.alive]
        supplies_after = exp.get("supplies", {})
        supplies_out = supplies_after.get(FOOD, 0) <= 0 or supplies_after.get(WATER, 0) <= 0
        settle_reason = None
        if not alive_after:
            settle_reason = "探索队全员失联"
        elif supplies_out:
            settle_reason = "补给耗尽，探索队被迫返程"
        elif self._end_conditions_met():
            settle_reason = "终局已至，探索队返程"
        if settle_reason is not None:
            return_detail, _ = self._settle_expedition(
                self.session.expedition, reason=settle_reason,
                enc_token=enc_token, enc_choice=choice["key"],
            )
            # 遭遇响应同时承载遭遇效果与当场返程结算；返程凭据记录同一份
            # 明细，保证该遭遇的连点/并发落败回放结果逐字一致
            detail = f"{detail}；队伍返程：{return_detail}"
            self.session.last_expedition["detail"] = detail
            return detail, False
        return detail, False

    def reconcile_stale_expedition(self, action, token=None, choice_key=None, exp_token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次探索队动作则安全回放。

        对不上任何已知结算时抛 409，由调用方提示刷新，杜绝并发重复结算。
        """
        rec = self.session.last_expedition
        if action == self._EXP_ACT_ENCOUNTER:
            ok = self._matches_expedition(rec, action, token, choice_key=choice_key)
            if not ok:
                # 遭遇已直接触发队伍收敛（返程凭据覆盖了遭遇凭据）：
                # 凭遭遇 token 回放那次返程结算，落败方同样拿到 200 而非 409；
                # 对不上任何已知结算（token/选项不符）则落入统一的 409
                settled = self._encounter_settlement_replay(token, choice_key=choice_key)
                if settled[0] is not None:
                    return settled
        else:
            ok = self._matches_expedition(rec, action, token, exp_token=exp_token)
        if ok:
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("探索队状态已被其他请求更新，请刷新后重试")

    def return_expedition(self, token=None):
        """玩家主动召回探索队：结算战利品入库、伤亡扣减、剩余物资归还。

        返程必须命中央档案里唯一的在外探索队；队伍 token 用于识别过期/重复请求。
        结算后探索队状态被清除并在档案上留下幂等凭据，重复提交只回放。
        返回 (detail, replayed)。
        """
        self._ensure_running()
        # 幂等回放优先：返程后队伍已清除，凭据仍在档案上可识别连点/并发落败请求
        replay = self._last_expedition_replay(
            self._EXP_ACT_RETURN, None, exp_token=token
        )
        if replay[0] is not None:
            return replay
        # 返程属于地堡经营动作：危机/遭遇待处理阶段一律锁定（幂等回放除外）
        self._require_daily_phase("召回探索队")
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            # 队伍已不在外：通常是上一次返程已完成。携带不匹配 token 的请求
            # 属于过期/串档，明确报 409；完全无凭据时才按“无队伍”处理
            rec = self.session.last_expedition
            if token and rec and rec.get("action") == self._EXP_ACT_RETURN:
                raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
            raise BunkerEngineError("当前没有在外的探索队")
        if exp.get("pending_encounter"):
            raise BunkerEngineError("探索队还有未处理的遭遇，无法返程")
        if token is not None and exp.get("token") and token != exp["token"]:
            raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
        return self._settle_expedition(exp, reason="探索队安全返程")

    def _settle_expedition(self, exp, reason, enc_token=None, enc_choice=None, pre_verdict=None):
        """结算探索队返程：战利品入库、剩余自带物资归还、伤亡扣减。

        幂等：以队伍 token 为凭据写入档案级 last_expedition，重复调用只回放，
        不二次发放战利品。enc_token/enc_choice 非空表示本次返程由某次遭遇
        抉择直接触发（补给耗尽/全员失联/终局收敛），返程凭据同时挂住该
        遭遇的一次性 token，使该遭遇的连点/并发落败请求也能安全回放。
        pre_verdict 为状态变更前快照的终局裁决（如行军日推进开始时已枯竭），
        优先于本方法内部快照。返回 (detail, replayed)。
        """
        exp_token = exp.get("token")
        rec = self.session.last_expedition
        if rec and rec.get("action") == self._EXP_ACT_RETURN and rec.get("exp_token") == exp_token:
            return rec.get("detail", ""), True
        members = self._away_residents()
        dead_members = [r for r in members if not r.alive]
        # 终局裁决在战利品/余粮入库前快照：全线枯竭的败局不得被随后入库的
        # 战利品抬过阈值而“复活”，终局状态与本结算只收敛一次（主动返程、
        # 补给耗尽强制返程、遭遇收敛各路径口径一致）；调用方（行军日推进）
        # 在产出前快照的更早裁决同样优先
        verdict = pre_verdict if pre_verdict is not None else self._end_verdict()
        # 战利品入库
        loot = exp.get("loot", {})
        loot_parts = [f"{RESOURCE_ZH.get(k, k)} +{v:g}" for k, v in loot.items() if v > 0]
        for k, v in loot.items():
            if v > 0:
                self._add_resource(k, v)
        # 剩余自带物资归还地堡（行军消耗已先行扣减，只归还正值余额）
        supplies = exp.get("supplies", {})
        supply_parts = [f"剩余{RESOURCE_ZH.get(k, k)} +{round(v, 1):g}" for k, v in supplies.items() if v > 0]
        for k, v in supplies.items():
            if v > 0:
                self._add_resource(k, v)
        # 伤亡（阵亡队员已在遭遇结算时扣减过 survivors，此处不再重复扣减）
        casualty_names = [r.name for r in dead_members]
        # 组装日志
        detail_parts = []
        if loot_parts:
            detail_parts.append("战利品：" + "、".join(loot_parts))
        if supply_parts:
            detail_parts.append("归还物资：" + "、".join(supply_parts))
        if casualty_names:
            detail_parts.append(f"殉职：{'、'.join(casualty_names)}")
        else:
            detail_parts.append("全员平安归来")
        detail = "；".join(detail_parts)
        self._log("system", f"探索队返程（{reason}）", detail, decision="返程结算")
        # 先写档案级幂等凭据，再清除探索队状态：凭据在队伍消失后依然可查
        self._remember_expedition(
            self._EXP_ACT_RETURN, enc_token, detail,
            exp_token=exp_token, choice_key=enc_choice,
        )
        self.session.expedition = None
        # 用入库前快照收敛终局；无预设败局时再按结算后状态正常判定
        self._check_end(forced_verdict=verdict)
        return detail, False


    # ---- 贸易救援 ----
    # 订单状态链：
    #   reviewing    申请已提交，等待外部聚落审核（托管物资已冻结）
    #     ├─ rejected 审核驳回：全额退还托管，订单关闭
    #     └─ transporting 审核通过：押运队离堡在途（成员按离堡口径结算）
    #          ├─ delivered 按期抵达并交付：回礼/采购入库，信誉与士气上升
    #          ├─ failed    途中弃货/全损/全员失联或交付失败：剩余货物回退、降信誉
    #          └─ 途中事件挂起（trade 阶段）：抉择后继续运输或当场收敛为 failed
    #   cancelled 玩家在审核阶段主动撤单：全额退还托管
    def _add_reputation(self, delta):
        rep = int(_clamp(
            (self.session.reputation if self.session.reputation is not None else TRADE_INITIAL_REPUTATION)
            + delta, TRADE_REP_MIN, TRADE_REP_MAX,
        ))
        self.session.reputation = rep
        return rep

    def trade_market(self):
        """生成当日外部聚落的贸易/救援报价（确定性，无副作用）。

        以"日期 + 序号"为种子：同一天内重复打开市场报价一致，跨天自动轮换，
        不随玩家刷新页面变化；申请时引擎重新生成当日市场并核对 offer_id，
        过期（跨天/被轮换掉）的报价无法下单。

        两类报价：
          rescue  聚落求援：地堡押运 escrow 物资前往，成功交付后对方回礼 cargo + 信誉
          procure 地堡采购：地堡预付 escrow，对方在交付时运来 cargo（风险共担，
                  在途损失按比例退款）
        """
        import random
        rng = random.Random(f"bunker-trade-market-day-{self.session.day}")
        partners = list(TRADE_PARTNERS)
        rng.shuffle(partners)
        offers = []
        # 前两个聚落发出求援，后两个聚落开放采购，四个交易对手互不重复
        for i, p in enumerate(partners[:4]):
            kind = "rescue" if i < 2 else "procure"
            others = [k for k in RESOURCE_KEYS if k != p["favor"]]
            if kind == "rescue":
                want = rng.choice(others)
                amount = rng.randint(26, 48)
                # 回礼按物资相对价值折算（对方出产的 favor 物资计价），含商谈浮动
                factor = rng.uniform(0.95, 1.25)
                reward = max(6, round(amount * TRADE_VALUE[want] / TRADE_VALUE[p["favor"]] * factor))
                offers.append({
                    "id": f"r-{p['key']}-{self.session.day}",
                    "type": "rescue",
                    "partner": p["key"],
                    "partner_name": p["name"],
                    "eta": p["distance"],
                    "escrow": {want: float(amount)},
                    "cargo": {p["favor"]: float(reward)},
                    "hint": f"{p['name']} 急缺{RESOURCE_ZH[want]}，愿以{RESOURCE_ZH[p['favor']]}回礼，押运约 {p['distance']} 天",
                })
            else:
                give = p["favor"]
                cost_res = rng.choice(others)
                qty = rng.randint(15, 34)
                markup = rng.uniform(1.05, 1.3)
                cost_amt = max(8, round(qty * TRADE_VALUE[give] / TRADE_VALUE[cost_res] * markup) + 4)
                offers.append({
                    "id": f"p-{p['key']}-{self.session.day}",
                    "type": "procure",
                    "partner": p["key"],
                    "partner_name": p["name"],
                    "eta": p["distance"],
                    "escrow": {cost_res: float(cost_amt)},
                    "cargo": {give: float(qty)},
                    "hint": f"向{p['name']}采购{RESOURCE_ZH[give]}，预付{RESOURCE_ZH[cost_res]}，押运约 {p['distance']} 天",
                })
        return offers

    def _find_offer(self, offer_id):
        return next((o for o in self.trade_market() if o["id"] == offer_id), None)

    def apply_trade(self, offer_id, escort_ids):
        """提交贸易/救援订单申请：冻结托管物资、组建押运队，进入 reviewing。"""
        self._require_daily_phase("申请贸易订单")
        if self.session.expedition:
            raise BunkerEngineError("探索队在外期间无法办理贸易订单")
        if self.session.trade_order:
            raise BunkerEngineError("已有在谈/在途贸易订单，无法同时申请第二笔")
        offer = self._find_offer(offer_id)
        if offer is None:
            raise BunkerEngineError("报价已过期或不存在（市场每日轮换），请重新打开市场")
        # 押运队校验：在堡存活居民，人数 1-3，不可重复
        if not escort_ids:
            raise BunkerEngineError("必须指定至少一名押运队员")
        if len(set(escort_ids)) != len(escort_ids):
            raise BunkerEngineError("同一名居民不能重复编入押运队")
        if len(escort_ids) > TRADE_MAX_ESCORTS:
            raise BunkerEngineError(f"押运队最多 {TRADE_MAX_ESCORTS} 人")
        for mid in escort_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("押运队员不存在或已故")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已离堡，无法参加押运")
            if self._has_active_case(r.id):
                raise BunkerEngineError(f"{r.name} 有未结病例，无法参加押运")
        if not self._can_afford(offer["escrow"]):
            raise BunkerEngineError("托管物资不足，无法申请该订单")
        # 冻结托管物资（审核驳回/撤单/失败回退时按规则退还）
        for k, v in offer["escrow"].items():
            self._add_resource(k, -v)
        order = {
            "token": uuid.uuid4().hex,
            "offer_id": offer["id"],
            "type": offer["type"],
            "partner": offer["partner"],
            "partner_name": offer["partner_name"],
            "eta": offer["eta"],
            "status": TRADE_REVIEWING,
            "applied_day": self.session.day,
            "escorts": list(escort_ids),
            "escrow": dict(offer["escrow"]),      # 已冻结的托管物资
            "cargo": dict(offer["cargo"]),        # 成功交付时地堡应得物资
            "cargo_ratio": 1.0,                   # 在途货物残存比例（途中事件损耗）
            "travel_days": 0,
            "incidents_resolved": 0,
            "pending_incident": None,
        }
        self.session.trade_order = dict(order)
        names = "、".join(r.name for r in self._trade_escorts(order))
        kind_zh = "救援申请" if offer["type"] == "rescue" else "采购申请"
        self._log(
            "trade", f"{kind_zh}·{offer['partner_name']}",
            f"{names} 组成押运队，托管物资已冻结，等待对方审核。",
            decision="提交申请",
        )
        return order

    def cancel_trade(self, token=None):
        """审核阶段主动撤单：全额退还托管。进入运输后不可撤单。"""
        self._ensure_running()
        # 幂等回放优先：撤单后订单已清除，凭据仍在档案上可识别连点
        replay = self._last_trade_replay(self._TRADE_ACT_CANCEL, token)
        if replay[0] is not None:
            return replay
        self._require_daily_phase("撤销贸易订单")
        order = self.session.trade_order
        if not order:
            if token and self.session.last_trade:
                raise BunkerEngineConflict("贸易订单状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有在谈的贸易订单")
        if token is not None and order.get("token") and token != order["token"]:
            raise BunkerEngineConflict("贸易订单状态已过期，请刷新后重试")
        if order.get("status") != TRADE_REVIEWING:
            # 审核已通过、订单进入运输：撤单窗口关闭，按状态过期处理（409）
            raise BunkerEngineConflict("订单已进入运输阶段，无法撤销，请刷新后重试")
        detail = self._refund_escrow(order, ratio=1.0, label="撤单退还")
        self._log("trade", f"撤单·{order['partner_name']}", detail, decision="撤销申请")
        detail = detail or "托管物资已全额退还"
        self._remember_trade(self._TRADE_ACT_CANCEL, order.get("token"), detail)
        self.session.trade_order = None
        return detail, False

    def _refund_escrow(self, order, ratio, label="退还"):
        """按残存比例退还托管物资，返回明细文本。"""
        parts = []
        for k, v in order.get("escrow", {}).items():
            amt = round(v * ratio, 1)
            if amt > 0:
                self._add_resource(k, amt)
                parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
        return f"{label}：" + "、".join(parts) if parts else ""

    # -- 每日推进：审核 / 在途运输 / 抵达交付 --
    def _progress_trade_order(self, order, pre_verdict=None):
        """推进贸易订单一天。返回挂起的途中事件（或 None）。

        - reviewing：审核日。终局已锁定时直接取消（全额退款）；否则掷审核，
          通过则当日出发并立刻走第一个在途日
        - transporting：在途日累加，途中可能挂起事件；抵达 eta 则交付/回退
        """
        if order.get("status") == TRADE_REVIEWING:
            # 终局日不再进行审核：撤单退款后随档案收敛到 ended
            if pre_verdict is not None:
                detail = self._refund_escrow(order, ratio=1.0, label="终局撤单退还")
                self._log("trade", f"撤单·{order['partner_name']}", detail or "终局已至，申请撤销", decision="终局撤单")
                self._remember_trade(
                    self._TRADE_ACT_CANCEL, order.get("token"),
                    detail or "终局已至，申请撤销",
                )
                self.session.trade_order = None
                return None
            if not self._review_trade_order(order):
                return None  # 审核驳回：订单已关闭
            # 审核通过：当日出发，继续走第一个在途日
        # transporting（含当日刚通过审核的订单）
        return self._tick_trade_transport(self.session.trade_order, pre_verdict=pre_verdict)

    def _review_trade_order(self, order):
        """外部聚落审核：信誉越高越容易通过。返回是否通过。"""
        rep = self.session.reputation or TRADE_INITIAL_REPUTATION
        # 求援方更看重地堡过往信誉（0.50-1.00）；采购方对陌生地堡更谨慎（0.40-0.80）
        chance = 0.50 + rep / 200.0 if order["type"] == "rescue" else 0.40 + rep / 250.0
        if self.rand.random() >= chance:
            detail = self._refund_escrow(order, ratio=1.0, label="全额退还")
            self._log(
                "trade", f"审核驳回·{order['partner_name']}",
                (detail + "；" if detail else "") + "对方回绝了本次申请，托管物资已退回。",
                decision="审核驳回",
            )
            self.session.trade_order = None
            return False
        order["status"] = TRADE_TRANSPORTING
        self.session.trade_order = dict(order)
        names = "、".join(r.name for r in self._trade_escorts(order) if r.alive)
        self._log(
            "trade", f"审核通过·{order['partner_name']}",
            f"{order['partner_name']} 接受申请，{names} 押运物资出发。",
            decision="审核通过",
        )
        return True

    def _tick_trade_transport(self, order, pre_verdict=None):
        """在途运输一天：累计行程、判定抵达或触发途中事件。"""
        alive_escorts = [r for r in self._trade_escorts(order) if r.alive]
        if not alive_escorts:
            # 押运队全员失联（理论上事件结算时即收敛，这里兜底不留僵尸订单）
            self._fail_trade_order(order, "押运队全员失联", forced_verdict=pre_verdict)
            return None
        order["travel_days"] += 1
        # 抵达日：直接交付/回退，不再触发途中事件
        if order["travel_days"] >= order["eta"]:
            self._deliver_trade_order(order, reason="押运队抵达聚落", forced_verdict=pre_verdict)
            return None
        if self.rand.random() <= TRADE_INCIDENT_CHANCE:
            event = self.rand.choice(TRADE_INCIDENTS)
            incident = self._build_trade_incident(event, order)
            order["pending_incident"] = incident
            self.session.trade_order = dict(order)
            return incident
        self.session.trade_order = dict(order)
        return None

    def _build_trade_incident(self, event, order):
        """构造途中事件快照（与危机/探索遭遇同一结构，刷新后可恢复抉择）。"""
        alive_escorts = [r for r in self._trade_escorts(order) if r.alive]
        needs_target = any(self._choice_targeted(c) for c in event["choices"])
        target = self.rand.choice(alive_escorts) if needs_target and alive_escorts else None
        return {
            "token": uuid.uuid4().hex,
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    # 贸易动作类型（档案级幂等凭据 last_trade）
    _TRADE_ACT_INCIDENT = "incident"
    _TRADE_ACT_SETTLE = "settle"   # 订单收敛（交付成功 / 失败回退 / 撤单退款）
    _TRADE_ACT_CANCEL = "cancel"

    @staticmethod
    def _matches_trade(rec, action, token, order_token=None, choice_key=None):
        """判断落败/重试请求是否就是上一次已完成的贸易动作（幂等回放）。"""
        if not rec or rec.get("action") != action:
            return False
        if token is not None and rec.get("token") and token != rec["token"]:
            return False
        if order_token is not None and rec.get("order_token") and order_token != rec["order_token"]:
            return False
        if choice_key is not None and rec.get("choice") is not None and choice_key != rec["choice"]:
            return False
        return True

    def _last_trade_replay(self, action, token, order_token=None, choice_key=None):
        rec = self.session.last_trade
        if self._matches_trade(rec, action, token, order_token=order_token, choice_key=choice_key):
            return rec.get("detail", ""), True
        return None, False

    def _incident_failure_replay(self, token, choice_key=None):
        """途中事件抉择直接触发订单收敛（弃货/全损/全员失联）时，凭事件 token
        回放那次结算明细。"""
        rec = self.session.last_trade
        if not token or not rec or rec.get("action") != self._TRADE_ACT_SETTLE:
            return None, False
        if rec.get("token") != token:
            return None, False
        if choice_key is not None and rec.get("choice") is not None and choice_key != rec["choice"]:
            return None, False
        return rec.get("detail", ""), True

    def _remember_trade(self, action, token, detail, order_token=None, choice_key=None):
        self.session.last_trade = {
            "action": action,
            "token": token,
            "order_token": order_token,
            "choice": choice_key,
            "day": self.session.day,
            "detail": detail,
        }

    def resolve_trade_incident(self, choice_key, token=None):
        """结算押运途中的事件抉择。

        效果键：cargo_loss（在途货物损耗比例）、delay（延误天数）、
        reputation（信誉变化）、health/morale（押运队员，single/all）、
        abort（弃货撤回：效果结清后当场按失败回退收敛）。
        返回 (detail, replayed)。
        """
        self._ensure_running()
        replay = self._last_trade_replay(self._TRADE_ACT_INCIDENT, token, choice_key=choice_key)
        if replay[0] is not None:
            return replay
        # 事件抉择直接触发失败收敛：凭据已被失败记录覆盖，但仍挂着事件 token
        converged = self._incident_failure_replay(token, choice_key=choice_key)
        if converged[0] is not None:
            return converged
        order = self.session.trade_order
        if not order or order.get("status") != TRADE_TRANSPORTING:
            if token:
                raise BunkerEngineConflict("贸易订单状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有在途的贸易订单")
        pending = order.get("pending_incident")
        if not pending:
            if token:
                raise BunkerEngineConflict("该途中事件已被处理，请刷新后重试")
            raise BunkerEngineError("当前没有待处理的途中事件")
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该途中事件决策已过期，请按当前事件重新选择")
        event = next((e for e in TRADE_INCIDENTS if e["key"] == pending.get("event")), None)
        if not event:
            raise BunkerEngineError("途中事件已失效，请刷新档案后重试")
        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")
        effects = choice.get("effects", {})
        targeted = self._choice_targeted(choice)
        target = None
        if targeted:
            bound_id = pending.get("target_id")
            if bound_id is None:
                raise BunkerEngineError("该决策需要指定一名押运队员作为目标")
            target = next((r for r in self._trade_escorts(order) if r.id == bound_id), None)
            if not target or not target.alive:
                raise BunkerEngineError("目标队员不在押运队中或已故，无法作为效果目标")
        # 校验全部完成后再施加效果，失败不留部分变更
        detail_parts = []
        alive_escorts = [r for r in self._trade_escorts(order) if r.alive]
        if effects.get("cargo_loss"):
            loss = float(effects["cargo_loss"])
            order["cargo_ratio"] = round(max(0.0, order.get("cargo_ratio", 1.0) * (1.0 - loss)), 3)
            detail_parts.append(f"货物损耗 {int(loss * 100)}%（残存 {int(order['cargo_ratio'] * 100)}%）")
        if effects.get("delay"):
            d = int(effects["delay"])
            order["eta"] += d
            detail_parts.append(f"行程延误 {d} 天")
        if effects.get("reputation"):
            rep = self._add_reputation(int(effects["reputation"]))
            detail_parts.append(f"信誉 {int(effects['reputation']):+d}（现 {rep}）")
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool, scope = [target], f"仅{target.name}"
            else:
                pool, scope = alive_escorts, "全体押运队员"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        if effects.get("abort"):
            detail_parts.append("弃货撤回")
        # 统一收敛押运伤亡（与探索遭遇同一口径，人口只扣一次）
        casualties = order.setdefault("casualties", [])
        for r in alive_escorts:
            if r.health <= 0 and r.alive:
                r.alive = 0
                r.health = 0
                if r.id not in casualties:
                    casualties.append(r.id)
                    self.session.survivors = max(0, self.session.survivors - 1)
        # 健康结算：受伤押运队员登记为病例，随押运队冻结，归队后续治
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else [
                r for r in alive_escorts if r.alive
            ]
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_INCIDENT_EVENTS,
                reason=f"途中事件·{event['title']}", force=True,
            )
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"途中事件·{event['title']}", f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        inc_token = pending.get("token")
        order["pending_incident"] = None
        order["incidents_resolved"] += 1
        self.session.trade_order = dict(order)
        self._remember_trade(
            self._TRADE_ACT_INCIDENT, inc_token, detail,
            order_token=order.get("token"), choice_key=choice["key"],
        )
        # 与探索遭遇一致：事件结算后立即收敛，不把零货物/全员阵亡的队伍留给下一步
        alive_after = [r for r in self._trade_escorts(order) if r.alive]
        settle_reason = None
        if not alive_after:
            settle_reason = "押运队全员失联"
        elif order["cargo_ratio"] <= 0:
            settle_reason = "货物全部损失，押运队空车返程"
        elif effects.get("abort"):
            settle_reason = "押运队弃货撤回"
        elif self._end_conditions_met():
            # 人口归零/全线枯竭等终局：强制安全交付后再收敛到 ended
            self._deliver_trade_order(
                self.session.trade_order, reason="终局已至，押运队返程", force_success=True,
            )
            return_detail = self.session.last_trade.get("detail", "") if self.session.last_trade else ""
            detail = f"{detail}；订单结算：{return_detail}" if return_detail else detail
            return detail, False
        if settle_reason is not None:
            return_detail, _ = self._fail_trade_order(
                self.session.trade_order, settle_reason,
                inc_token=inc_token, inc_choice=choice["key"],
            )
            detail = f"{detail}；订单回退：{return_detail}"
            self.session.last_trade["detail"] = detail
            return detail, False
        return detail, False

    def reconcile_stale_trade(self, action, token=None, choice_key=None):
        """并发落败后核对贸易动作：同一次抉择/结算则安全回放，否则 409。"""
        rec = self.session.last_trade
        if action == self._TRADE_ACT_INCIDENT:
            ok = self._matches_trade(rec, action, token, choice_key=choice_key)
            if not ok:
                # 事件抉择直接触发订单收敛（结算凭据覆盖了事件凭据）：
                # 凭事件 token 回放那次结算，落败方同样拿到 200 而非 409
                settled = self._incident_failure_replay(token, choice_key=choice_key)
                if settled[0] is not None:
                    return settled
        else:
            ok = self._matches_trade(rec, action, token)
        if ok:
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("贸易订单状态已被其他请求更新，请刷新后重试")

    def _trade_rep_penalty(self, order):
        """失败回退的信誉扣减：求援订单失信代价更高。"""
        return -8 if order["type"] == "rescue" else -5

    def _deliver_trade_order(self, order, reason, forced_verdict=None, force_success=False):
        """抵达交付结算：成功则回礼/采购入库，失败则剩余货物回退。

        forced_verdict 为推进开始前快照的终局裁决，优先于本方法内部快照，
        保证入库的回礼不会复活当日已成立的败局（与探索队返程同一口径）。
        """
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        rep = self.session.reputation or TRADE_INITIAL_REPUTATION
        # 成功概率：求援 0.55-0.95，采购 0.60-1.00，均随信誉提高
        chance = (0.55 + rep / 250.0) if order["type"] == "rescue" else (0.60 + rep / 250.0)
        success = force_success or self.rand.random() < chance
        ratio = order.get("cargo_ratio", 1.0)
        if not success:
            return self._fail_trade_order(
                order, f"{reason}，但交易失败", inc_token=None, forced_verdict=verdict,
            )
        parts = []
        if order["type"] == "rescue":
            # 求援：援助物资已送达对方，原则上不退回；仅在途损耗的残份（ratio<1）
            # 随车带回；对方按实际送达比例回礼
            if ratio < 1.0:
                refund = self._refund_escrow(order, ratio=1.0 - ratio, label="未送达的援助物资带回")
                if refund:
                    parts.append(refund)
            gain_parts = []
            for k, v in order["cargo"].items():
                amt = round(v * ratio, 1)
                if amt > 0:
                    self._add_resource(k, amt)
                    gain_parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
            if gain_parts:
                parts.append("对方回礼：" + "、".join(gain_parts))
            rep_now = self._add_reputation(6)
            for r in self._in_bunker_residents():
                r.morale = _clamp(r.morale + 8)
            for r in self._trade_escorts(order):
                if r.alive:
                    r.morale = _clamp(r.morale + 10)
            parts.append(f"信誉 +6（现 {rep_now}），在堡全员士气 +8")
            title = f"救援送达·{order['partner_name']}"
        else:
            # 采购：按残存比例到货，损失部分对应托管按比例退还（风险共担）
            gain_parts = []
            for k, v in order["cargo"].items():
                amt = round(v * ratio, 1)
                if amt > 0:
                    self._add_resource(k, amt)
                    gain_parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
            parts.append("采购到货：" + "、".join(gain_parts))
            refund = self._refund_escrow(order, ratio=1.0 - ratio, label="损失部分退款")
            if refund:
                parts.append(refund)
            rep_now = self._add_reputation(3)
            for r in self._in_bunker_residents():
                r.morale = _clamp(r.morale + 5)
            for r in self._trade_escorts(order):
                if r.alive:
                    r.morale = _clamp(r.morale + 7)
            parts.append(f"信誉 +3（现 {rep_now}），在堡全员士气 +5")
            title = f"采购到货·{order['partner_name']}"
        detail = "；".join(parts)
        self._log("trade", title, f"{reason}。{detail}", decision="交付结算")
        self._remember_trade(self._TRADE_ACT_SETTLE, None, detail, order_token=order.get("token"))
        # 用交付前快照收敛终局
        self.session.trade_order = None
        self._check_end(forced_verdict=verdict)
        return detail, False

    def _fail_trade_order(self, order, reason, inc_token=None, inc_choice=None, forced_verdict=None):
        """失败回退：未送出的托管物资退回、扣信誉、押运队士气受挫。

        求援订单：援助未送达（交易失败/弃货撤回），托管物资按残存比例全额带回；
        采购订单：货到不了，预付托管按残存比例退回（其余视为共同损失）。
        inc_token 非空表示本次回退由某条途中事件抉择直接触发（弃货/全损/
        全员失联），回退凭据同时挂住该事件一次性 token，供连点/并发落败回放。
        返回 (detail, False)。
        """
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        ratio = order.get("cargo_ratio", 1.0)
        parts = []
        label = "未送达援助物资带回" if order["type"] == "rescue" else "预付物资退回"
        refund = self._refund_escrow(order, ratio=ratio, label=label)
        if refund:
            parts.append(refund)
        penalty = self._trade_rep_penalty(order)
        rep_now = self._add_reputation(penalty)
        morale_hit = -8 if order["type"] == "rescue" else -5
        for r in self._trade_escorts(order):
            if r.alive:
                r.morale = _clamp(r.morale + morale_hit)
        dead = [r.name for r in self._trade_escorts(order) if not r.alive]
        parts.append(f"信誉 {penalty:+d}（现 {rep_now}），押运队员士气 {morale_hit:+d}")
        if dead:
            parts.append(f"殉职：{'、'.join(dead)}")
        detail = "；".join(parts)
        self._log("trade", f"订单失败·{order['partner_name']}", f"{reason}。{detail}", decision="失败回退")
        self._remember_trade(
            self._TRADE_ACT_SETTLE, inc_token, detail,
            order_token=order.get("token"), choice_key=inc_choice,
        )
        self.session.trade_order = None
        self._check_end(forced_verdict=verdict)
        return detail, False


    # ---- 医疗救治中心 ----
    # 病例状态链：
    #   registered 登记：病例已建档但尚未收治（无床位/未安排），健康持续恶化；
    #                    传染病例未收治时每天可能在堡内扩散
    #     ├─ treating  治疗：占用救治床位，消耗物资，医护在岗时回复更快
    #     └─ isolated  隔离：消耗更高，但切断传染扩散；对传染病例是推荐处置
    #   recovered 康复（在治期间健康跨过康复线）/ deceased 病亡（健康归零）
    #   离堡成员（探索队/押运队）的病例随队伍冻结：不恶化、不扩散、不消耗床位
    def _cases(self):
        # 浅拷贝列表：元素仍是存档病例字典的同一引用，内部可原地修改病例状态；
        # 持久化时由 _save_cases 深拷贝后整体回写，保证 JSON 列追踪变更
        return list(self.session.medical_cases or [])

    def _save_cases(self, cases):
        # 深拷贝后整体回写：medical_cases 是 JSON 列，病例状态常以原地改字典方式
        # 变更（admit_case 等）。除赋一份全新 list+dict 外再 flag_modified，
        # 确保工作单元在跨请求/刷新场景下一定把病例簿刷入数据库
        from sqlalchemy.orm.attributes import flag_modified
        self.session.medical_cases = [dict(c) for c in cases]
        flag_modified(self.session, "medical_cases")

    def _resident_case_map(self):
        """居民编号 → 未终态病例（每名居民至多一份活跃病例）。"""
        return {c.get("resident_id"): c for c in self._cases()
                if c.get("status") in MED_ACTIVE_STATUSES}

    def _caring_resident_ids(self):
        """当前正在救治床位上（治疗/隔离）的居民编号，含离堡冻结者。"""
        return {c.get("resident_id") for c in self._cases()
                if c.get("status") in MED_CARE_STATUSES}

    def _has_active_case(self, resident_id):
        """该居民是否存在未终态病例（登记/治疗/隔离）。"""
        return resident_id in self._resident_case_map()

    def _active_clinic(self):
        """当前生效的医疗救治中心：取等级最高的一座（无则 None）。"""
        clinics = [f for f in self.session.facilities
                   if f.category == "clinic" and f.status == "active"]
        return max(clinics, key=lambda f: f.level, default=None)

    def medic_count(self):
        """当值医护（medic）人数：在堡、存活、本人未躺上病床。"""
        return self.job_count("medic")

    def med_bed_capacity(self):
        """救治床位上限：每级救治中心 2 张 + 每名当值医护 1 张；无中心则无床位。"""
        clinic = self._active_clinic()
        if clinic is None:
            return 0
        return MED_BEDS_PER_LEVEL * clinic.level + self.medic_count()

    def med_beds_used(self):
        """已占用床位：只计在堡病例（离堡冻结者不占地堡床位）。"""
        away = self._away_resident_ids()
        return sum(
            1 for c in self._cases()
            if c.get("status") in MED_CARE_STATUSES and c.get("resident_id") not in away
        )

    def med_summary(self):
        """医疗面板概览（无副作用）：中心等级、医护、床位与各状态病例数。"""
        cases = self._cases()
        counts = {s: 0 for s in (MED_REGISTERED, MED_TREATING, MED_ISOLATED,
                                 MED_RECOVERED, MED_DECEASED)}
        for c in cases:
            s = c.get("status")
            if s in counts:
                counts[s] += 1
        away = self._away_resident_ids()
        # 离堡冻结病例单独标注，便于前端提示"回堡后继续救治"
        frozen = sum(1 for c in cases
                     if c.get("status") in MED_ACTIVE_STATUSES and c.get("resident_id") in away)
        return {
            "has_clinic": self._active_clinic() is not None,
            "clinic_level": self._active_clinic().level if self._active_clinic() else 0,
            "medics": self.medic_count(),
            "bed_capacity": self.med_bed_capacity(),
            "beds_used": self.med_beds_used(),
            "registered": counts[MED_REGISTERED],
            "treating": counts[MED_TREATING],
            "isolated": counts[MED_ISOLATED],
            "recovered": counts[MED_RECOVERED],
            "deceased": counts[MED_DECEASED],
            "frozen_away": frozen,
            "total_registered": len(cases),
        }

    def register_case(self, resident_id, infectious=False, reason="手动登记"):
        """为居民登记新病例（仅每日阶段、已建救治中心、本人无活跃病例）。

        手动登记要求健康低于登记线；事件后的强制建档（force 路径）走
        _open_case，不受健康线限制。
        """
        self._require_daily_phase("登记病例")
        if self._active_clinic() is None:
            raise BunkerEngineError("尚未建造医疗救治中心，无法登记病例")
        r = self._find_in_bunker_resident(resident_id)
        if r.health >= MED_MANUAL_REGISTER_HEALTH:
            raise BunkerEngineError(f"{r.name} 健康状况良好（≥{MED_MANUAL_REGISTER_HEALTH:g}），无需登记")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError(f"{r.name} 已离堡，回堡后再登记病例")
        if self._has_active_case(r.id):
            raise BunkerEngineError(f"{r.name} 已有在档病例，请勿重复登记")
        self._open_case(r, infectious=infectious, reason=reason, kind=("疫病" if infectious else "伤病"))
        self._log(
            "medical", "病例登记",
            f"{r.name} 因{reason}登记为{('传染病例，需尽快隔离' if infectious else '伤病病例')}，等待收治。",
            decision="登记病例",
        )

    def admit_case(self, resident_id, mode):
        """把已登记病例收治上床：mode 为 treating（治疗）或 isolated（隔离）。

        已在治病例可在治疗/隔离之间切换（不重复占床）；床位不足时拒绝。
        离堡成员病例冻结，回堡前无法收治。
        """
        self._require_daily_phase("收治病例")
        if mode not in MED_CARE_STATUSES:
            raise BunkerEngineError("未知收治方式")
        if self._active_clinic() is None:
            raise BunkerEngineError("尚未建造医疗救治中心，无法收治")
        r = self._find_in_bunker_resident(resident_id)
        # 在同一份病例列表上找到目标并原地修改，避免"从 A 拷贝找字典、回写 B 拷贝"
        cases = self._cases()
        case = next((c for c in cases
                     if c.get("resident_id") == r.id and c.get("status") in MED_ACTIVE_STATUSES), None)
        if case is None:
            raise BunkerEngineError(f"{r.name} 没有待收治的病例")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError(f"{r.name} 已离堡，病例随队伍冻结，回堡后再收治")
        if case["status"] != mode and case["status"] == MED_REGISTERED \
                and self.med_beds_used() >= self.med_bed_capacity():
            raise BunkerEngineError("救治床位已满，可升级医疗救治中心或增派医护")
        old_status = case["status"]
        case["status"] = mode
        case["admitted_day"] = self.session.day
        self._save_cases(cases)
        mode_zh = "隔离" if mode == MED_ISOLATED else "治疗"
        if old_status == MED_REGISTERED:
            self._log(
                "medical", "病例收治",
                f"{r.name} 转入{mode_zh}床位，医护开始按日救治。",
                decision=f"收治·{mode_zh}",
            )
        else:
            old_zh = "隔离" if old_status == MED_ISOLATED else "治疗"
            self._log(
                "medical", "调整救治方案",
                f"{r.name} 由{old_zh}改为{mode_zh}处置。",
                decision=f"转·{mode_zh}",
            )

    def _find_in_bunker_resident(self, resident_id):
        r = next((x for x in self.session.residents if x.id == resident_id), None)
        if not r or not r.alive:
            raise BunkerEngineError("居民不存在或已故")
        return r

    def _build_case_dict(self, resident, infectious, reason, kind="伤病",
                         status=MED_REGISTERED):
        """构造一份新病例字典（默认登记态）。"""
        return {
            "id": f"med-{uuid.uuid4().hex[:12]}",
            "resident_id": resident.id,
            "resident_name": resident.name,
            "status": status,
            "infectious": bool(infectious),
            "kind": kind,
            "reason": reason,
            "open_day": self.session.day,
            "admitted_day": self.session.day if status in MED_CARE_STATUSES else None,
            "close_day": None,
            "days_cared": 0,
        }

    def _open_case(self, resident, infectious, reason, kind="伤病",
                   status=MED_REGISTERED):
        """内部建档：不校验阶段/健康线/是否在堡（事件结算、每日巡检均可用）。

        要求本人无活跃病例。返回新病例字典并立即整体回写。
        """
        cases = self._cases()
        for c in cases:
            if c.get("resident_id") == resident.id and c.get("status") in MED_ACTIVE_STATUSES:
                return c  # 已有活跃病例：事件损伤叠加在既有病例上，不重复建档
        case = self._build_case_dict(resident, infectious, reason, kind, status=status)
        cases.append(case)
        self._save_cases(cases)
        return case

    def _settle_health_aftermath(self, affected, infectious_event, reason, force=False):
        """危机/遭遇/途中事件健康结算：把受伤幸存者登记进医疗救治中心。

        affected 为已施加健康效果后的存活居民列表；infectious_event 表示本次
        抉择具传染性（疫病/恶劣天气）。health 低于自动登记线即建档；force 时
        （被点名的传染目标）即使尚未跌破线也强制建档，避免漏网传播。
        未建造救治中心时无医疗体系承接，仅返回（健康损失已由事件本身生效）。
        """
        if self._active_clinic() is None:
            return
        cases = self._cases()
        for r in affected:
            if not r.alive:
                continue
            already = any(
                c.get("resident_id") == r.id and c.get("status") in MED_ACTIVE_STATUSES
                for c in cases
            )
            if already:
                continue
            forced = force and infectious_event
            if forced or r.health < MED_AUTO_REGISTER_HEALTH:
                self._open_case(
                    r, infectious=infectious_event, reason=reason,
                    kind=("疫病" if infectious_event else "伤病"),
                )

    def _apply_medical_tick(self):
        """每日医疗结算：在治病例消耗/回复、未收治病例恶化与扩散、康复病亡收敛、
        每日巡检自动登记。仅作用于在堡病例；离堡病例冻结，等回堡后续治。
        """
        # 在存档病例簿（同一批字典引用）上结算，过程中所有 _open_case 与
        # days_cared 等原地修改都落在同一份列表，最后整体深拷贝回写一次落库
        cases = self._cases()
        away = self._away_resident_ids()
        residents = {r.id: r for r in self.session.residents}

        # 1) 收敛已死亡居民的悬空活跃病例（匮乏死亡等）
        for c in list(cases):
            if c.get("status") in MED_ACTIVE_STATUSES:
                r = residents.get(c.get("resident_id"))
                if r is None or not r.alive:
                    c["status"] = MED_DECEASED
                    c["close_day"] = self.session.day

        # 2) 床位物资配给：在堡治疗/隔离病例按日消耗；任一物资不足则当日停诊
        in_care = [c for c in cases
                   if c.get("status") in MED_CARE_STATUSES and c.get("resident_id") not in away]
        need = {FOOD: 0.0, WATER: 0.0, POWER: 0.0}
        for c in in_care:
            for k, v in MED_CARE_COST[c["status"]].items():
                need[k] += v
        need = {k: round(v, 1) for k, v in need.items()}
        res = self.get_resources()
        rationed = bool(in_care) and all(res.get(k, 0) >= v for k, v in need.items())
        if in_care:
            if rationed:
                for k, v in need.items():
                    self._add_resource(k, -v)
            else:
                # 停诊：物资缺口下全体在治病例士气受挫，当日无救治回复
                for c in in_care:
                    r = residents.get(c.get("resident_id"))
                    if r:
                        r.morale = _clamp(r.morale - 5)

        clinic = self._active_clinic()
        level_scale = FACILITY_LEVEL_SCALE ** (clinic.level - 1) if clinic else 1.0
        medics = self.medic_count()
        recovered, deceased = [], []
        spreading = []

        # 3) 逐病例每日进展（在治回复 / 未收治恶化，离堡病例冻结）
        for c in list(cases):
            if c.get("status") not in MED_ACTIVE_STATUSES:
                continue
            r = residents.get(c.get("resident_id"))
            if r is None or not r.alive:
                continue
            if r.id in away:
                continue  # 离堡冻结：不恶化、不扩散、不消耗
            status = c["status"]
            if status in MED_CARE_STATUSES:
                if rationed:
                    heal = MED_HEAL[status] * level_scale + MED_MEDIC_HEAL[status] * medics
                    r.health = _clamp(r.health + heal)
                    r.morale = _clamp(r.morale + 0.5)
                    c["days_cared"] = c.get("days_cared", 0) + 1
                if r.health >= MED_RECOVER_HEALTH:
                    c["status"] = MED_RECOVERED
                    c["close_day"] = self.session.day
                    recovered.append(r.name)
                    continue
            else:
                # 登记未收治：伤病每日缓慢恶化，传染病恶化更快、且可能扩散
                r.health = _clamp(r.health - MED_DECAY[bool(c.get("infectious"))])
                r.morale = _clamp(r.morale - (3 if c.get("infectious") else 2))
                if c.get("infectious"):
                    spreading.append(c)
            if r.health <= 0:
                r.alive = 0
                r.health = 0
                self.session.survivors = max(0, self.session.survivors - 1)
                c["status"] = MED_DECEASED
                c["close_day"] = self.session.day
                deceased.append(r.name)

        # 康复/病亡日志（在统一回写前记录）
        for name in recovered:
            self._log("medical", "病例康复", f"{name} 康复出院，重返岗位。", decision="康复出院")
        for name in deceased:
            self._log("medical", "病例病亡", f"{name} 不治。", decision="病亡")

        # 4) 传染扩散：未收治/未隔离的传染病例掷传播（在治病例被隔离阻断）
        if spreading:
            chance = max(
                MED_SPREAD_MIN_CHANCE,
                MED_SPREAD_CHANCE - MED_SPREAD_MEDIC_REDUCE * medics,
            )
            active_map = {c.get("resident_id"): c for c in cases
                          if c.get("status") in MED_ACTIVE_STATUSES}
            in_bunker = self._in_bunker_residents()
            # 只结算当日已在册的传染源：巡检/本次扩散新建病例当日不再继续扩散，
            # 避免一次推进里链式传染全堡
            for c in list(spreading):
                if c.get("status") != MED_REGISTERED:
                    continue
                if self.rand.random() > chance:
                    continue
                pool = [r for r in in_bunker if r.id not in active_map and r.alive]
                if not pool:
                    break
                victim = self.rand.choice(pool)
                victim.health = _clamp(victim.health - MED_SPREAD_DAMAGE)
                victim.morale = _clamp(victim.morale - 4)
                if victim.health < MED_AUTO_REGISTER_HEALTH:
                    new_case = self._build_case_dict(
                        victim, infectious=True, reason="接触未隔离疫病", kind="疫病",
                    )
                    cases.append(new_case)
                    active_map[victim.id] = new_case
                self._log(
                    "medical", "疫病扩散",
                    f"未隔离病例 {c.get('resident_name')} 传染了 {victim.name}（健康 -{MED_SPREAD_DAMAGE:g}）。",
                    decision="疫情蔓延",
                )

        # 5) 每日巡检：在堡、健康跌破登记线且无活跃病例者自动建档（普通伤病）
        if clinic is not None:
            active_map = {c.get("resident_id"): c for c in cases
                          if c.get("status") in MED_ACTIVE_STATUSES}
            for r in self._in_bunker_residents():
                if r.id in away or r.id in active_map:
                    continue
                if r.health < MED_AUTO_REGISTER_HEALTH:
                    cases.append(self._build_case_dict(
                        r, infectious=False, reason="每日巡检发现", kind="伤病",
                    ))
                    active_map[r.id] = cases[-1]

        # 6) 深拷贝整体回写，确保 JSON 列追踪到病例字典的原地变更
        self._save_cases(cases)

        # 7) 当日医疗日志（聚合一行，无变化则不刷屏）
        parts = []
        if in_care:
            if rationed:
                cost_txt = "、".join(f"{RESOURCE_ZH[k]} -{v:g}" for k, v in need.items() if v > 0)
                parts.append(f"在治 {len(in_care)} 例（消耗{cost_txt}）")
            else:
                parts.append(f"物资不足，{len(in_care)} 例停诊")
        if recovered:
            parts.append(f"康复：{'、'.join(recovered)}")
        if deceased:
            parts.append(f"病亡：{'、'.join(deceased)}")
        if parts:
            self._log("medical", f"第{self.session.day}天 · 医疗结算", "，".join(parts), decision="例行救治")

    def _medical_stats(self):
        """危机后健康结算用的全周期救治统计（写入终局 outcome）。"""
        cases = self._cases()
        active = [c for c in cases if c.get("status") in MED_ACTIVE_STATUSES]
        recovered = sum(1 for c in cases if c.get("status") == MED_RECOVERED)
        deceased = sum(1 for c in cases if c.get("status") == MED_DECEASED)
        care_days = sum(c.get("days_cared", 0) for c in cases)
        return {
            "total": len(cases),
            "active": len(active),
            "recovered": recovered,
            "deceased": deceased,
            "care_days": care_days,
        }

    # ---- 扩建 ----
    def build_facility(self, category):
        self._require_daily_phase("建造设施")
        cost = FACILITY_COST[1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法建造")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f = Facility(
            session_id=self.session.id,
            name=FACILITY_ZH.get(category, category),
            category=category,
            level=1,
            status="active",
            built_day=self.session.day,
        )
        # 同时挂到关系集合：若 facilities 已被加载进身份映射，仅设外键 + flush
        # 不会让新设施出现在内存集合中，显式挂关系保证随后查询立即可见
        self.session.facilities.append(f)
        self.db.add(f)
        self.db.flush()  # 让新设施立即反映到 session.facilities 集合
        self._log("system", "设施扩建", f"建造了{FACILITY_ZH.get(category, category)}。", decision="扩建")
        return f

    def upgrade_facility(self, facility_id):
        self._require_daily_phase("升级设施")
        f = next((x for x in self.session.facilities if x.id == facility_id), None)
        if not f:
            raise BunkerEngineError("设施不存在")
        if f.level >= max(FACILITY_COST.keys()):
            raise BunkerEngineError("已达最高等级")
        cost = FACILITY_COST[f.level + 1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法升级")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f.level += 1
        self._log("system", "设施升级", f"{FACILITY_ZH.get(f.category, f.category)} 提升到 Lv.{f.level}。", decision="升级")
        return f

    def _can_afford(self, cost):
        res = self.get_resources()
        return all(res.get(k, 0) >= v for k, v in cost.items())

    # ---- 任务分配（重分配岗位）----
    def set_job(self, resident_id, job):
        self._require_daily_phase("调整岗位")
        if job not in JOB_EFFICIENCY:
            raise BunkerEngineError("未知岗位")
        r = next((x for x in self.session.residents if x.id == resident_id), None)
        if not r or not r.alive:
            raise BunkerEngineError("居民不存在或已故")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError("探索队中的居民无法调整岗位")
        r.job = job

    # ---- 结局判定 ----
    def _check_end(self, forced_verdict=None):
        """判定并落终局状态。

        forced_verdict 为状态变更（如返程战利品入库）前快照的裁决：一旦在
        变更前已满足终局（尤其是全线枯竭），即便变更后资源回升也照样收敛，
        保证终局判定单调、不被中途入库的物资“救回”。返回是否处于终局。
        """
        if self.session.status != "running":
            return True
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        if verdict is None:
            return False
        win, reason = verdict
        self._finish(win=win, reason=reason)
        return True

    def _finish(self, win, reason):
        # 幂等：终局只结算一次。重复调用（多路径收敛）直接返回，
        # 不重算分数、不重复写结局日志
        if self.session.status != "running":
            return
        self.session.status = "win" if win else "over"
        # 进入终局后不存在悬而未决的抉择/在外队伍/在途订单，状态机统一收敛到 ended
        self.session.pending_crisis = None
        self.session.expedition = None
        self.session.trade_order = None
        alive = [r for r in self.session.residents if r.alive]
        # 计分：幸存者 * 天数 * 士气系数
        morale = self.avg_morale()
        score = int(self.session.survivors * self.session.day * (0.5 + morale / 200.0))
        self.session.score = score
        # 危机后健康结算：全周期医疗救治履历随终局归档（登记/康复/病亡/累计救治日）
        med_stats = self._medical_stats()
        avg_health = round(sum(r.health for r in alive) / len(alive), 1) if alive else 0.0
        self.session.outcome = {
            "win": win,
            "reason": reason,
            "survivors": len(alive),
            "day": self.session.day,
            "avg_health": avg_health,
            "medical": med_stats,
        }
        if med_stats["total"]:
            self._log(
                "medical", "医疗救治总结算",
                f"全周期登记 {med_stats['total']} 例：康复 {med_stats['recovered']} 人、"
                f"病亡 {med_stats['deceased']} 人、累计救治 {med_stats['care_days']} 床日，"
                f"幸存者平均健康 {avg_health}。",
                decision="健康结算",
            )
        self._log("system", "游戏结束", reason, decision="结局")


RESOURCE_ZH = {"food": "食物", "water": "水源", "power": "电力", "oxygen": "氧气"}
FACILITY_ZH = {"farm": "穹顶菜园", "water": "净水器", "power": "发电机", "oxygen": "水培制氧", "med": "医疗舱", "clinic": "医疗救治中心", "storage": "仓储区"}

# 物资跨类别折算的相对价值：食物/水更稀缺昂贵，电力次之，氧气最便宜
TRADE_VALUE = {FOOD: 1.2, WATER: 1.1, POWER: 0.9, OXY: 0.8}

# 外部聚落：distance 为单程在途天数（审核通过后），favor 为其出产/偏好物资
TRADE_PARTNERS = [
    {"key": "ridge",   "name": "岭上镇",   "distance": 2, "favor": FOOD},
    {"key": "dock",    "name": "旧港码头", "distance": 3, "favor": WATER},
    {"key": "station", "name": "变电站营地", "distance": 2, "favor": POWER},
    {"key": "dome",    "name": "七号穹顶", "distance": 4, "favor": OXY},
]


# ============ 危机事件池（决策树） ============
CRISIS_POOL = [
    {
        "key": "radstorm",
        "title": "辐射风暴来袭",
        "desc": "一场强辐射风暴正在逼近地堡。派工程师抢修屏蔽层，或让所有人避难并停电。",
        "choices": [
            {
                "key": "shield_repair",
                "label": "抢修屏蔽层",
                "hint": "消耗少量电力，成功则平安，失败有人员受伤",
                "effects": {"resources": {"power": -8}},
            },
            {
                "key": "shutdown",
                "label": "全员断电避难",
                "hint": "所有设施停摆一天，电力下降，无人员风险",
                "effects": {"resources": {"power": -15, "food": -5, "water": -4}},
            },
        ],
    },
    {
        "key": "mutiny",
        "title": "地堡内讧",
        "desc": "因食物分配不公，一部分人情绪失控，要求重新分配口粮。",
        "choices": [
            {
                "key": "double_ration",
                "label": "加倍发放食物",
                "hint": "士气+20，但食物储备大减",
                "effects": {"resources": {"food": -20}, "morale": 20},
            },
            {
                "key": "suppress",
                "label": "严令镇压",
                "hint": "食物不变，但士气大降",
                "effects": {"morale": -15},
            },
        ],
    },
    {
        "key": "leak",
        "title": "氧气泄漏",
        "desc": "水培舱密封圈老化，氧气正在泄漏。",
        "choices": [
            {
                "key": "emergency_repair",
                "label": "紧急封堵",
                "hint": "消耗食物与电力，防止气体外泄",
                "effects": {"resources": {"food": -6, "power": -6}},
            },
            {
                "key": "vent",
                "label": "先泄压再修",
                "hint": "氧气大降但更省资源",
                "effects": {"resources": {"oxygen": -20, "power": -3}},
            },
        ],
    },
    {
        "key": "sick",
        "title": "疫病袭来",
        "desc": "一名幸存者出现不明高热，可能是污染引发的疾病。",
        "choices": [
            {
                "key": "quarantine",
                "label": "隔离治疗",
                "hint": "该居民卸下工作，健康缓慢回复",
                "effects": {"resources": {"food": -4}, "health": {"value": -5, "target": "single"}},
            },
            {
                "key": "public_health",
                "label": "全员消毒",
                "hint": "消耗电力与水源消毒，保护大家",
                "effects": {"resources": {"power": -6, "water": -8}},
            },
        ],
    },
    {
        "key": "raid",
        "title": "盗匪袭扰",
        "desc": "地堡外传来敲击声，一伙流民试图破门而入抢夺物资。",
        "choices": [
            {
                "key": "defend",
                "label": "武装抵抗",
                "hint": "能耗物资，可能有人受伤，但守住粮食",
                "effects": {"resources": {"food": -2, "power": -4}, "health": {"value": -8, "target": "single"}},
            },
            {
                "key": "bribe",
                "label": "分粮和解",
                "hint": "交出部分食物换取平安",
                "effects": {"resources": {"food": -18}},
            },
        ],
    },
    {
        "key": "scavenge",
        "title": "发现物资舱",
        "desc": "侦察队在地堡深处发现一间废弃补给舱，但已部分损坏。",
        "choices": [
            {
                "key": "crack_open",
                "label": "强制开启",
                "hint": "可能获得大量补给，也可能毁坏",
                "effects": {"resources": {"food": 12, "water": 8}},
            },
            {
                "key": "careful",
                "label": "小心拆解",
                "hint": "稳定获得少量补给",
                "effects": {"resources": {"food": 6, "water": 5, "power": 3}},
            },
        ],
    },
    {
        "key": "blizzard",
        "title": "暴雪封门",
        "desc": "极寒暴雪掩盖了地堡入口，通风与采能都受影响。",
        "choices": [
            {
                "key": "burn_fuel",
                "label": "燃烧燃料保温",
                "hint": "消耗食物(燃料)维持温度",
                "effects": {"resources": {"food": -10}},
            },
            {
                "key": "huddle",
                "label": "集中避寒",
                "hint": "士气下降，但省下燃料",
                "effects": {"morale": -10},
            },
        ],
    },
    {
        "key": "caravan_help",
        "title": "路过商队求助",
        "desc": "一支外部商队在地堡附近抛锚，请求分享补给与维修零件。出手相助或许能换来口碑。",
        "choices": [
            {
                "key": "aid",
                "label": "慷慨接济",
                "hint": "消耗食物与电力，对外信誉与士气提升",
                "effects": {"resources": {"food": -10, "power": -6}, "reputation": 8, "morale": 6},
            },
            {
                "key": "trade_part",
                "label": "等价交换",
                "hint": "以物资换取对方的水源，信誉小升",
                "effects": {"resources": {"food": -6, "water": 8}, "reputation": 3},
            },
            {
                "key": "refuse",
                "label": "闭门不纳",
                "hint": "物资无损，但口碑与士气下降",
                "effects": {"reputation": -6, "morale": -5},
            },
        ],
    },
]


# ============ 探索队遭遇池（外出探索途中的遭遇决策树） ============
# 与地堡危机相互独立：探索队在外时，每日行军触发的是探索遭遇而非地堡危机。
# 效果键：
#   loot       —— 战利品，单独累计，返程时统一入库
#   supply_loss —— 从探索队自带物资中扣除
#   health/morale —— 队员健康/士气（single 仅作用于目标队员，all 作用于全体队员）
#   add_resident —— 有幸存者加入队伍
EXPEDITION_ENCOUNTERS = [
    {
        "key": "cache",
        "title": "废弃补给点",
        "desc": "探索队在一处废墟中发现半埋的废弃补给箱，外观尚可辨认。",
        "choices": [
            {
                "key": "search_carefully",
                "label": "仔细搜索",
                "hint": "耗时但可能获得更多物资",
                "effects": {"loot": {FOOD: 8, WATER: 6}},
            },
            {
                "key": "grab_quickly",
                "label": "快速搜刮",
                "hint": "安全但收获有限",
                "effects": {"loot": {FOOD: 4, WATER: 3}},
            },
        ],
    },
    {
        "key": "beast",
        "title": "异兽袭击",
        "desc": "一头变异巨兽从废墟中窜出，挡住了去路。",
        "choices": [
            {
                "key": "fight",
                "label": "武装驱赶",
                "hint": "可能有人受伤，但能保住物资并缴获战利品",
                "effects": {"health": {"value": -12, "target": "single"}, "loot": {FOOD: 5}},
            },
            {
                "key": "flee",
                "label": "绕道撤退",
                "hint": "损失部分物资，但无人受伤",
                "effects": {"supply_loss": {FOOD: 6, WATER: 4}, "morale": -5},
            },
        ],
    },
    {
        "key": "weather",
        "title": "恶劣天气",
        "desc": "辐射尘暴骤起，能见度极低，探索队被迫寻找掩体。",
        "choices": [
            {
                "key": "take_shelter",
                "label": "就地躲避",
                "hint": "消耗一日物资，士气下降",
                "effects": {"supply_loss": {FOOD: 3, WATER: 3}, "morale": -8},
            },
            {
                "key": "push_through",
                "label": "冒雨前进",
                "hint": "可能生病，但不耽误行程",
                "effects": {"health": -6, "morale": -3},
            },
        ],
    },
    {
        "key": "survivors",
        "title": "偶遇幸存者",
        "desc": "探索队遇到一群流离失所的幸存者，他们请求加入地堡。",
        "choices": [
            {
                "key": "accept",
                "label": "接纳加入",
                "hint": "新增一名幸存者，但消耗更多补给",
                "effects": {"add_resident": True, "supply_loss": {FOOD: 4, WATER: 3}},
            },
            {
                "key": "trade",
                "label": "交换物资",
                "hint": "用自带物资换取情报与小份补给",
                "effects": {"supply_loss": {FOOD: 3}, "loot": {POWER: 5}, "morale": 3},
            },
            {
                "key": "refuse",
                "label": "拒绝并离开",
                "hint": "保持警惕，安然离开",
                "effects": {"morale": -2},
            },
        ],
    },
    {
        "key": "ruins",
        "title": "废墟探索",
        "desc": "一座保存较完整的废弃建筑矗立在眼前，隐约有物资的气息。",
        "choices": [
            {
                "key": "deep_explore",
                "label": "深入探索",
                "hint": "高风险高回报，可能有重大伤亡",
                "effects": {"loot": {FOOD: 12, WATER: 8, POWER: 6}, "health": {"value": -15, "target": "single"}},
            },
            {
                "key": "outer_search",
                "label": "外围搜索",
                "hint": "安全获得少量物资",
                "effects": {"loot": {FOOD: 5, WATER: 4}},
            },
        ],
    },
    {
        "key": "lost",
        "title": "迷路",
        "desc": "复杂的废墟巷道让探索队迷失了方向，补给在不知不觉中消耗。",
        "choices": [
            {
                "key": "retrace",
                "label": "凭记忆折返",
                "hint": "消耗额外物资寻找归路",
                "effects": {"supply_loss": {FOOD: 5, WATER: 4}, "morale": -5},
            },
            {
                "key": "climb_high",
                "label": "登高辨认",
                "hint": "冒险登高，可能有意外收获",
                "effects": {"loot": {FOOD: 3}, "health": -4, "morale": 2},
            },
        ],
    },
    {
        "key": "airdrop",
        "title": "空投补给",
        "desc": "一架老旧的运输机残骸旁，探索队发现了未被开启的空投舱。",
        "choices": [
            {
                "key": "open_carefully",
                "label": "小心开启",
                "hint": "稳定获得补给",
                "effects": {"loot": {FOOD: 6, WATER: 6, POWER: 4, OXY: 4}},
            },
            {
                "key": "force_open",
                "label": "强行破开",
                "hint": "可能获得更多，也可能损坏物资",
                "effects": {"loot": {FOOD: 10, WATER: 8, POWER: 6}, "supply_loss": {OXY: 3}},
            },
        ],
    },
    {
        "key": "trap",
        "title": "陷阱",
        "desc": "探索队触发了一处老旧的捕兽夹，一名队员被夹住。",
        "choices": [
            {
                "key": "free_carefully",
                "label": "小心解救",
                "hint": "可能加重伤势，但能保全物资",
                "effects": {"health": {"value": -10, "target": "single"}},
            },
            {
                "key": "force_free",
                "label": "强行挣脱",
                "hint": "伤势更重，但不耽误行程",
                "effects": {"health": {"value": -18, "target": "single"}, "supply_loss": {FOOD: 2}},
            },
        ],
    },
]

# ============ 贸易押运途中事件池 ============
# 押运队（reviewing 通过后离堡）在每个在途日可能遭遇；事件挂起时进入 trade 阶段，
# 替代当日地堡危机。效果键（区别于地堡危机/探索遭遇）：
#   cargo_loss  —— 在途货物/托管残存比例乘法折损（0-1）
#   delay       —— 行程延误天数（eta 增加）
#   reputation  —— 地堡信誉变化
#   health/morale —— 押运队员健康/士气（single 仅目标，all 全体押运队员）
#   abort       —— 弃货撤回：效果结清后当场按失败回退收敛
TRADE_INCIDENTS = [
    {
        "key": "ambush",
        "title": "流民截道",
        "desc": "一伙武装流民在隘口设下路障，要求押运队留下货物买路。",
        "choices": [
            {
                "key": "fight_through",
                "label": "强行突围",
                "hint": "可能有人受伤、损失部分货物，但保住大部分订单",
                "effects": {"health": {"value": -14, "target": "single"}, "cargo_loss": 0.2, "morale": -4},
            },
            {
                "key": "pay_toll",
                "label": "缴纳货物买路",
                "hint": "折损三成货物，无人受伤",
                "effects": {"cargo_loss": 0.3, "morale": -3},
            },
            {
                "key": "abandon",
                "label": "弃货撤回",
                "hint": "放弃订单保命，剩余货物随车退回，订单判失败",
                "effects": {"abort": True, "cargo_loss": 0.0, "morale": -6},
            },
        ],
    },
    {
        "key": "duststorm",
        "title": "辐射沙暴",
        "desc": "灰黄色的辐射沙暴横扫荒原，能见度几乎为零。",
        "choices": [
            {
                "key": "shelter",
                "label": "就地掩蔽等待",
                "hint": "无人受伤，但行程延误 1 天且轻微货损",
                "effects": {"delay": 1, "cargo_loss": 0.1},
            },
            {
                "key": "push",
                "label": "冒沙暴赶路",
                "hint": "不延误，但队员可能病倒",
                "effects": {"health": -8, "morale": -3},
            },
        ],
    },
    {
        "key": "patrol",
        "title": "聚落巡逻队盘查",
        "desc": "一支陌生聚落的巡逻队持枪拦下押运车，怀疑你们是走私者。",
        "choices": [
            {
                "key": "papers",
                "label": "出示交易凭据交涉",
                "hint": "顺利放行，信誉在各聚落间传开",
                "effects": {"reputation": 4},
            },
            {
                "key": "bribe",
                "label": "分货打点",
                "hint": "交出两成货物换取放行",
                "effects": {"cargo_loss": 0.2},
            },
            {
                "key": "detour",
                "label": "绕开巡逻线",
                "hint": "行程延误，队员疲惫",
                "effects": {"delay": 2, "morale": -5},
            },
        ],
    },
    {
        "key": "breakdown",
        "title": "运输车故障",
        "desc": "老旧运输车在半路趴窝，货物还散落在辐射尘中。",
        "choices": [
            {
                "key": "repair",
                "label": "就地抢修",
                "hint": "消耗队员体力，大部分货物可救回",
                "effects": {"health": {"value": -8, "target": "single"}, "cargo_loss": 0.15},
            },
            {
                "key": "haul",
                "label": "人力拖拽前进",
                "hint": "全员疲惫、货损较多，但不延误",
                "effects": {"health": -5, "cargo_loss": 0.25},
            },
            {
                "key": "abandon",
                "label": "弃车撤回",
                "hint": "放弃订单，剩余货物撤回，订单判失败",
                "effects": {"abort": True, "cargo_loss": 0.0, "morale": -5},
            },
        ],
    },
]
