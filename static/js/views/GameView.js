/* 末日地堡生存 —— 主游戏界面 */
window.GameView = {
  props: ["sid", "onExit"],
  data() {
    return {
      s: null,
      crisis: null,
      loading: false,
      error: "",
      tab: "overview",
      config: null,
      buildings: [],
      selectedJob: {},
      showExpeditionDialog: false,
      expMembers: [],
      expSupplies: { food: 0, water: 0 },
      // 贸易救援
      market: null,
      marketLoading: false,
      showTradeDialog: false,
      tradeOffer: null,
      tradeEscorts: [],
      // 医疗救治中心
      medRegistering: {},
    };
  },
  created() { this.init(); },
  methods: {
    async init() {
      this.error = "";
      try {
        const [s, cfg, bld] = await Promise.all([
          Api.get(`/api/sessions/${this.sid}`),
          Api.get("/api/config"),
          Api.get("/api/buildings"),
        ]);
        this.s = s; this.config = cfg; this.buildings = bld;
        // 待处理危机已随存档持久化：刷新/重进档案后恢复同一个决策弹层
        this.crisis = s.pending_crisis || null;
      } catch (e) { this.error = e.message; }
    },
    async loadSession() {
      this.s = await Api.get(`/api/sessions/${this.sid}`);
      // 以服务端为准恢复待处理危机（并发落败回放时也可能带回）
      this.crisis = this.s.pending_crisis || null;
    },
    async advance() {
      this.error = "";
      if (this.s.status !== "running" || this.actionLocked) return;
      this.loading = true;
      try {
        const r = await Api.post(`/api/sessions/${this.sid}/advance`);
        this.s = r.session;
        // 两个抉择弹层统一只从服务端会话快照派生（不读返回里的 pending_event/crisis）：
        // 该字段可能是地堡危机也可能是探索遭遇，直接复用会把遭遇渲染成危机。
        // 地堡危机 → s.pending_crisis；探索遭遇 → s.expedition.pending_encounter（模板另判）
        this.crisis = this.s.pending_crisis || null;
      } catch (e) {
        this.error = e.message;
        // 并发落败等 409 场景：拉取最新状态，避免覆盖掉已挂起的抉择
        await this.loadSession();
      }
      finally { this.loading = false; }
    },
    async resolve(c) {
      this.error = "";
      this.loading = true;
      try {
        // 目标语义以后端下发的 c.targeted 为准：
        // 仅单体决策回传 target_id；全体决策显式传 null，
        // 避免危机事件的随机目标被无条件带回、把全体效果收窄成一人。
        // token 绑定本次待处理危机：重复/并发请求由后端识别为同一次结算
        const body = {
          event_key: this.crisis.event,
          choice_key: c.key,
          target_id: c.targeted ? this.crisis.target_id : null,
          token: this.crisis.token,
        };
        this.s = await Api.post(`/api/sessions/${this.sid}/resolve`, body);
        this.crisis = this.s.pending_crisis || null;
      } catch (e) {
        this.error = e.message;
        // 409（过期/并发）或危机已被其他标签页结算：刷新为最新状态
        await this.loadSession();
      }
      finally { this.loading = false; }
    },
    async build(cat) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/build`, { category: cat });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async upgrade(fid) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/upgrade/${fid}`);
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async assignJob(rid, job) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/resident/${rid}/job`, { job });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async loadSessionOn409(e) {
      // 409（并发落败/状态过期）统一以服务端为准，防止旧标签页继续按过期状态操作
      if (e && e.status === 409) await this.loadSession();
    },
    setJobSel(rid, job) { this.selectedJob[rid] = job; },
    // ---- 探索队 ----
    openExpeditionDialog() {
      this.error = "";
      this.expMembers = [];
      this.expSupplies = { food: 0, water: 0 };
      this.showExpeditionDialog = true;
    },
    toggleMember(id) {
      const i = this.expMembers.indexOf(id);
      if (i >= 0) this.expMembers.splice(i, 1);
      else {
        if (this.expMembers.length >= 4) { this.error = "探索队最多 4 人"; return; }
        this.expMembers.push(id);
      }
    },
    async sendExpedition() {
      this.error = "";
      if (!this.expMembers.length) { this.error = "必须选择至少一名居民"; return; }
      if (this.actionLocked) return;
      this.loading = true;
      try {
        const supplies = {};
        for (const k of ["food", "water"]) {
          const v = Number(this.expSupplies[k]) || 0;
          if (v > 0) supplies[k] = v;
        }
        this.s = await Api.post(`/api/sessions/${this.sid}/expedition/send`, {
          member_ids: this.expMembers,
          supplies,
        });
        this.showExpeditionDialog = false;
      } catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
    async resolveExpeditionEncounter(c) {
      this.error = "";
      this.loading = true;
      try {
        const body = { choice_key: c.key, token: this.s.expedition.pending_encounter.token };
        this.s = await Api.post(`/api/sessions/${this.sid}/expedition/resolve`, body);
      } catch (e) { this.error = e.message; await this.loadSession(); }
      finally { this.loading = false; }
    },
    async returnExpedition() {
      this.error = "";
      this.loading = true;
      try {
        const body = { token: this.s.expedition.token };
        this.s = await Api.post(`/api/sessions/${this.sid}/expedition/return`, body);
      } catch (e) { this.error = e.message; await this.loadSession(); }
      finally { this.loading = false; }
    },
    expMemberNames() {
      if (!this.s || !this.s.expedition) return "";
      const ids = this.s.expedition.members || [];
      return ids.map(id => {
        const r = this.s.residents.find(x => x.id === id);
        return r ? r.name : "?";
      }).join("、");
    },
    expLootText() {
      if (!this.s || !this.s.expedition) return "";
      const loot = this.s.expedition.loot || {};
      const parts = [];
      for (const k of ["food", "water", "power", "oxygen"]) {
        if (loot[k] > 0) parts.push(`${{food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k]}+${Math.round(loot[k])}`);
      }
      return parts.join("、") || "暂无";
    },
    // ---- 贸易救援 ----
    tradeStatusZh(st) {
      return { reviewing: "申请审核中", transporting: "押运在途", delivered: "已交付",
               failed: "已失败回退", rejected: "已驳回", cancelled: "已撤销" }[st] || st;
    },
    async openTradeMarket() {
      this.error = "";
      this.marketLoading = true;
      try {
        this.market = await Api.get(`/api/sessions/${this.sid}/trade/market`);
        this.showTradeDialog = true;
      } catch (e) { this.error = e.message; }
      finally { this.marketLoading = false; }
    },
    pickTradeOffer(o) {
      this.tradeOffer = o;
      this.tradeEscorts = [];
    },
    toggleTradeEscort(id) {
      const i = this.tradeEscorts.indexOf(id);
      if (i >= 0) this.tradeEscorts.splice(i, 1);
      else {
        if (this.tradeEscorts.length >= 3) { this.error = "押运队最多 3 人"; return; }
        this.tradeEscorts.push(id);
      }
    },
    async submitTrade() {
      this.error = "";
      if (!this.tradeOffer) { this.error = "请选择一项报价"; return; }
      if (!this.tradeEscorts.length) { this.error = "必须指定至少一名押运队员"; return; }
      this.loading = true;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/trade/apply`, {
          offer_id: this.tradeOffer.id,
          escort_ids: this.tradeEscorts,
        });
        this.showTradeDialog = false;
        this.tradeOffer = null;
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
      finally { this.loading = false; }
    },
    async cancelTrade() {
      this.error = "";
      this.loading = true;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/trade/cancel`, {
          token: this.s.trade_order.token,
        });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
      finally { this.loading = false; }
    },
    async resolveTradeIncident(c) {
      this.error = "";
      this.loading = true;
      try {
        const body = { choice_key: c.key, token: this.s.trade_order.pending_incident.token };
        this.s = await Api.post(`/api/sessions/${this.sid}/trade/resolve`, body);
      } catch (e) { this.error = e.message; await this.loadSession(); }
      finally { this.loading = false; }
    },
    tradeEscortNames() {
      if (!this.s || !this.s.trade_order) return "";
      return (this.s.trade_order.escorts || []).map(id => {
        const r = this.s.residents.find(x => x.id === id);
        return r ? r.name : "?";
      }).join("、");
    },
    fmtTradeBags(bag) {
      return Object.entries(bag || {}).map(([k, v]) =>
        `${{food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] || k} ${Math.round(v)}`
      ).join("、");
    },
    // ---- 医疗救治中心 ----
    medCaseOf(rid) {
      const cases = (this.s && this.s.medical_cases) || [];
      return cases.find(c => c.resident_id === rid &&
        ["registered", "treating", "isolated"].includes(c.status)) || null;
    },
    caseStatusZh(st) {
      return { registered: "待收治", treating: "治疗中", isolated: "隔离中",
               recovered: "已康复", deceased: "已病亡" }[st] || st;
    },
    medCaseTitle(c) {
      if (!c) return "";
      const base = (c.infectious ? "传染病例" : (c.kind || "伤病病例"));
      return `${base} · ${this.caseStatusZh(c.status)}`;
    },
    async registerCase(rid, infectious) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/medical/register/${rid}`, { infectious });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async admitCase(rid, mode) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/medical/admit/${rid}`, { mode });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    medActiveCases() {
      const cases = (this.s && this.s.medical_cases) || [];
      return cases.filter(c => ["registered", "treating", "isolated"].includes(c.status));
    },
    medHistoryCases() {
      const cases = (this.s && this.s.medical_cases) || [];
      return cases.filter(c => ["recovered", "deceased"].includes(c.status))
        .slice(-12).reverse();
    },
    canRegister(r) {
      // 在堡、存活、无活跃病例且健康低于 70 才可登记
      return r.alive && !r.away && !this.medCaseOf(r.id) && r.health < 70;
    },
    resPct(k) {
      const cap = { food: 300, water: 300, power: 200, oxygen: 200 };
      const c = cap[k] || 100;
      return Math.min(100, Math.round((this.s.resources[k] / c) * 100));
    },
    clazz(st) {
      return st === "win" ? "win" : st === "over" ? "over" : "running";
    },
    fmt(v) { return v == null ? "-" : Math.round(v); },
  },
  computed: {
    alive() { return this.s ? this.s.residents.filter(r => r.alive) : []; },
    expPending() {
      return !!(this.s && this.s.expedition && this.s.expedition.pending_encounter);
    },
    tradeOrder() {
      return this.s ? this.s.trade_order : null;
    },
    tradePending() {
      return !!(this.s && this.s.trade_order && this.s.trade_order.pending_incident);
    },
    // 抉择锁：地堡危机/探索遭遇/途中事件待处理时，推进与一切经营动作统一禁用
    actionLocked() {
      return !!(this.crisis || this.expPending || this.tradePending);
    },
    pendingTitle() {
      if (this.crisis) return "请先处理当前危机";
      if (this.expPending) return "请先处理探索遭遇";
      if (this.tradePending) return "请先处理途中事件";
      return "";
    },
    inBunkerAlive() {
      return this.s ? this.s.residents.filter(r => r.alive && !r.away) : [];
    },
    expMemberCount() {
      // 只统计档案中仍在编制内的成员，兼容旧快照里夹杂已移除编号的情况
      if (!this.s || !this.s.expedition) return 0;
      const ids = this.s.expedition.members || [];
      return ids.filter(id => this.s.residents.some(r => r.id === id)).length;
    },
    medActiveCount() {
      return this.medActiveCases().length;
    },
    medCareCostText() {
      // 治疗床位/隔离床位的每日人均消耗（与引擎 MED_CARE_COST 同口径）
      const zh = { food: "食物", water: "水源", power: "电力" };
      const treating = { food: 0.6, water: 0.5, power: 0.8 };
      const isolated = { food: 0.8, water: 0.7, power: 1.2 };
      const fmt = bag => Object.entries(bag).map(([k, v]) => `${zh[k]} ${v}`).join("、");
      return { treating: fmt(treating), isolated: fmt(isolated) };
    },
  },
  template: `
  <div v-if="s" class="game" :class="clazz(s.status)">
    <!-- 顶栏 -->
    <header class="game-top">
      <div class="brand">末日地堡<i class="bar"></i></div>
      <div class="day">{{ s.day }}<small>/{{ s.target_day }} 天</small></div>
      <div class="top-right">
        <span class="chip rep" title="对外信誉：影响外部聚落的审核与交付">信誉 {{ s.reputation }}</span>
        <span class="chip" :class="s.status">{{ s.status === 'running' ? '进行中' : s.status === 'win' ? '胜利' : '失败' }}</span>
        <button class="btn ghost small" @click="onExit">返回档案</button>
      </div>
    </header>

    <!-- 资源条 -->
    <section class="resbar">
      <div v-for="k in ['food','water','power','oxygen']" :key="k" class="res" :class="{ low: s.resources[k] < 20 && s.status==='running' }">
        <div class="res-name">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }}</div>
        <div class="res-val">{{ fmt(s.resources[k]) }}</div>
        <div class="res-track"><div class="res-fill" :class="k" :style="{ width: resPct(k)+'%' }"></div></div>
      </div>
      <button class="btn primary advance" :disabled="loading || s.status!=='running' || actionLocked" :title="pendingTitle" @click="advance">
        {{ crisis ? '等待危机抉择' : expPending ? '等待探索遭遇抉择' : tradePending ? '等待途中事件抉择' : loading ? '推进中…' : '推进一天' }}
      </button>
    </section>
    <div v-if="error" class="msg err global">{{ error }}</div>

    <!-- 主区 -->
    <div class="game-body">
      <nav class="tabs">
        <button :class="{ active: tab==='overview' }" @click="tab='overview'">总览</button>
        <button :class="{ active: tab==='residents' }" @click="tab='residents'">幸存者 ({{ alive.length }})</button>
        <button :class="{ active: tab==='expedition' }" @click="tab='expedition'">探索队<template v-if="s.expedition"> ({{ expMemberCount }})</template></button>
        <button :class="{ active: tab==='trade' }" @click="tab='trade'">贸易救援<template v-if="s.trade_order"> ●</template></button>
        <button :class="{ active: tab==='medical' }" @click="tab='medical'">医疗救治<template v-if="medActiveCount"> ({{ medActiveCount }})</template></button>
        <button :class="{ active: tab==='build' }" @click="tab='build'">设施扩建</button>
        <button :class="{ active: tab==='log' }" @click="tab='log'">大事记</button>
      </nav>

      <!-- 总览 -->
      <div v-if="tab==='overview'">
        <div class="cards">
          <div class="card"><div class="k">幸存者</div><div class="v">{{ s.survivors }}</div><div class="hint">人口即火种</div></div>
          <div class="card"><div class="k">平均健康</div><div class="v">{{ s.residents.length ? fmt(alive.reduce((a,r)=>a+r.health,0)/alive.length) : 0 }}</div><div class="hint">救治中心维系</div></div>
          <div class="card"><div class="k">士气</div><div class="v">{{ s.residents.length ? fmt(alive.reduce((a,r)=>a+r.morale,0)/alive.length) : 0 }}</div><div class="hint">影响产出效率</div></div>
          <div class="card"><div class="k">得分</div><div class="v">{{ s.score }}</div><div class="hint">生存评分</div></div>
        </div>
        <div class="fac-grid">
          <div v-for="f in s.facilities" :key="f.id" class="fac">
            <span class="fac-name">{{ f.name }}</span>
            <span class="chip">Lv.{{ f.level }}</span>
            <span class="dim">{{ {farm:'产食物',water:'产水源',power:'发电',oxygen:'产氧',med:'医疗',clinic:'救治中心',storage:'仓储'}[f.category] }}</span>
            <button v-if="s.status==='running'" class="btn tiny" :disabled="actionLocked" @click="upgrade(f.id)">升级</button>
          </div>
        </div>
      </div>

      <!-- 幸存者 -->
      <div v-if="tab==='residents'">
        <div v-for="r in s.residents" :key="r.id" class="person" :class="{ dead: !r.alive, away: r.away }">
          <div class="p-avatar">{{ r.name[0] }}</div>
          <div class="p-info">
            <div class="p-name">{{ r.name }} <span class="dim">{{ r.job_zh }}</span>
              <span v-if="r.case_status" class="chip med-tag" :class="r.case_status">
                {{ r.case_infectious ? '疫' : '病' }}·{{ caseStatusZh(r.case_status) }}
              </span>
              <span v-if="r.away" class="chip away-tag">{{ r.trade_status === 'transporting' ? '押运中' : '探索中' }}</span><span v-if="r.trade_status==='reviewing'" class="chip review-tag">待押运</span>
            </div>
            <div class="meter"><i>健康</i><span class="track"><span class="fill" :style="{width: r.health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(r.health) }}</b></div>
            <div class="meter"><i>士气</i><span class="track"><span class="fill" :style="{width: r.morale+'%', background:'#ffb300'}"></span></span><b>{{ fmt(r.morale) }}</b></div>
          </div>
          <div class="p-actions" v-if="r.alive && s.status==='running'">
            <select :value="r.job" :disabled="actionLocked || r.away || !!r.case_status" @change="assignJob(r.id, $event.target.value)">
              <option value="engineer">工程师</option>
              <option value="farmer">农民</option>
              <option value="medic">医护</option>
              <option value="general">杂工</option>
            </select>
            <button v-if="s.medical_summary && s.medical_summary.has_clinic && !r.away && !r.case_status && r.health < 70"
                    class="btn tiny" :disabled="actionLocked" @click="tab='medical'">登记病例</button>
          </div>
        </div>
      </div>

      <!-- 探索队 -->
      <div v-if="tab==='expedition'">
        <!-- 无在外队伍：派遣 -->
        <div v-if="!s.expedition" class="exp-panel">
          <div class="exp-empty">
            <p>派遣幸存者携带物资外出探索，途中可能遭遇事件，返程时统一结算战利品与伤亡。</p>
            <p class="dim">离堡人员暂停地堡生产，不消耗地堡口粮；探索队消耗自带物资。</p>
            <button class="btn primary" :disabled="s.status!=='running' || actionLocked" @click="openExpeditionDialog">派遣探索队</button>
          </div>
        </div>
        <!-- 有在外队伍：状态 -->
        <div v-else class="exp-panel">
          <div class="exp-status">
            <div class="exp-row"><span class="k">队员</span><span class="v">{{ expMemberNames() }}</span></div>
            <div class="exp-row"><span class="k">行军</span><span class="v">第 {{ s.expedition.travel_days }} 天 / 上限 7 天</span></div>
            <div class="exp-row"><span class="k">自带物资</span><span class="v">食物 {{ Math.round(s.expedition.supplies.food||0) }} · 水 {{ Math.round(s.expedition.supplies.water||0) }}</span></div>
            <div class="exp-row"><span class="k">战利品（未结算）</span><span class="v loot">{{ expLootText() }}</span></div>
            <div class="exp-row" v-if="s.expedition.encounters_resolved"><span class="k">已处理遭遇</span><span class="v">{{ s.expedition.encounters_resolved }} 次</span></div>
          </div>
          <div class="exp-actions">
            <button class="btn primary" :disabled="s.status!=='running' || actionLocked" @click="returnExpedition">
              {{ crisis ? '请先处理危机' : expPending ? '请先处理遭遇' : '立即返程' }}
            </button>
            <span class="dim" v-if="!actionLocked">返程时统一结算战利品与伤亡</span>
          </div>
        </div>
      </div>

      <!-- 贸易救援 -->
      <div v-if="tab==='trade'">
        <div class="exp-panel">
          <!-- 无在谈订单：打开市场 -->
          <div v-if="!s.trade_order" class="exp-empty">
            <p>与外部聚落协商救援或采购订单：托管物资、组建押运队，
               经对方审核后离堡运输，途中可能遭遇截道与沙暴，抵达后交付结算。</p>
            <p class="dim">审核/交付成功率受信誉影响；成功换来物资与口碑，失败则剩余货物退回、信誉下降。</p>
            <button class="btn primary" :disabled="s.status!=='running' || actionLocked || !!s.expedition"
                    @click="openTradeMarket">
              {{ marketLoading ? '联络中…' : '联络外部聚落' }}
            </button>
            <div v-if="s.expedition" class="dim" style="margin-top:8px">探索队在外期间无法办理贸易订单</div>
          </div>
          <!-- 在谈/在途订单 -->
          <div v-else class="exp-status">
            <div class="exp-row">
              <span class="k">状态</span>
              <span class="v">
                <span class="chip" :class="s.trade_order.status">{{ tradeStatusZh(s.trade_order.status) }}</span>
                <b style="margin-left:8px">{{ s.trade_order.type === 'rescue' ? '紧急救援' : '对外采购' }} · {{ s.trade_order.partner_name }}</b>
              </span>
            </div>
            <div class="exp-row"><span class="k">押运队员</span><span class="v">{{ tradeEscortNames() }}</span></div>
            <div class="exp-row"><span class="k">托管物资（已冻结）</span><span class="v">{{ fmtTradeBags(s.trade_order.escrow) }}</span></div>
            <div class="exp-row"><span class="k">{{ s.trade_order.type === 'rescue' ? '对方回礼' : '采购到货' }}</span><span class="v loot">{{ fmtTradeBags(s.trade_order.cargo) }}</span></div>
            <div class="exp-row" v-if="s.trade_order.status==='transporting'">
              <span class="k">在途进度</span>
              <span class="v">第 {{ s.trade_order.travel_days }} / {{ s.trade_order.eta }} 天
                · 货物残存 {{ Math.round((s.trade_order.cargo_ratio || 1) * 100) }}%</span>
            </div>
            <div class="exp-row" v-if="s.trade_order.incidents_resolved">
              <span class="k">已处理途中事件</span><span class="v">{{ s.trade_order.incidents_resolved }} 次</span>
            </div>
            <div class="exp-actions">
              <button v-if="s.trade_order.status==='reviewing'" class="btn danger"
                      :disabled="s.status!=='running' || actionLocked" @click="cancelTrade">撤单并退还托管</button>
              <span class="dim" v-if="s.trade_order.status==='reviewing'">审核结果将在下一次推进时公布；撤单全额退还</span>
              <span class="dim" v-if="s.trade_order.status==='transporting' && !tradePending">押运队在途，推进一天以继续运输</span>
              <span class="dim" v-if="tradePending" style="color:var(--warn)">途中出现突发状况，请先抉择</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 医疗救治中心 -->
      <div v-if="tab==='medical'">
        <div class="exp-panel">
          <!-- 未建造救治中心 -->
          <div v-if="!s.medical_summary || !s.medical_summary.has_clinic" class="exp-empty">
            <p>建造「医疗救治中心」后，受伤与染疫的居民可经历 <b>登记 → 治疗/隔离 → 康复</b> 的完整救治流程。</p>
            <p class="dim">救治床位按中心等级提供，指派「医护」岗位可增加床位与救治效率；治疗/隔离每日消耗食物、水源与电力，未隔离的传染病例可能在堡内扩散。</p>
            <button class="btn primary" :disabled="s.status!=='running' || actionLocked"
                    @click="s && (tab='build')">前往设施扩建</button>
          </div>
          <!-- 救治中心面板 -->
          <div v-else>
            <div class="med-head">
              <span class="chip">医疗救治中心 Lv.{{ s.medical_summary.clinic_level }}</span>
              <span class="chip">当值医护 {{ s.medical_summary.medics }}</span>
              <span class="chip">床位 {{ s.medical_summary.beds_used }} / {{ s.medical_summary.bed_capacity }}</span>
              <span class="chip registered">待收治 {{ s.medical_summary.registered }}</span>
              <span class="chip reviewing">隔离中 {{ s.medical_summary.isolated }}</span>
            </div>
            <p class="dim" v-if="s.medical_summary.frozen_away" style="color:var(--info)">
              {{ s.medical_summary.frozen_away }} 名病例随探索/押运队伍离堡，病例冻结，回堡后续治。
            </p>

            <!-- 活跃病例 -->
            <h3 class="med-h3" v-if="medActiveCases().length">在档病例（{{ medActiveCases().length }}）</h3>
            <div class="med-cases">
              <div v-for="c in medActiveCases()" :key="c.id" class="med-case" :class="c.status">
                <template v-for="r in s.residents" :key="r.id">
                  <div v-if="r.id === c.resident_id" class="med-case-inner">
                    <span class="p-avatar">{{ r.name[0] }}</span>
                    <div class="mc-info">
                      <div class="mc-name">
                        {{ r.name }}
                        <span class="chip" :class="c.status">{{ medCaseTitle(c) }}</span>
                        <span v-if="r.away" class="chip away-tag">离堡冻结</span>
                      </div>
                      <div class="meter"><i>健康</i><span class="track"><span class="fill" :style="{width: r.health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(r.health) }}</b></div>
                      <div class="dim">{{ c.reason }} · 第 {{ c.open_day }} 天登记<span v-if="c.days_cared"> · 已救治 {{ c.days_cared }} 床日</span></div>
                    </div>
                    <div class="mc-actions" v-if="s.status==='running' && !r.away">
                      <button class="btn tiny primary" :disabled="actionLocked"
                              @click="admitCase(r.id, 'treating')">
                        {{ c.status === 'treating' ? '治疗中' : '转入治疗' }}
                      </button>
                      <button class="btn tiny danger" :disabled="actionLocked"
                              @click="admitCase(r.id, 'isolated')">
                        {{ c.status === 'isolated' ? '隔离中' : '转入隔离' }}
                      </button>
                    </div>
                  </div>
                </template>
              </div>
            </div>

            <!-- 可登记的在堡居民 -->
            <h3 class="med-h3">健康巡查</h3>
            <p class="dim">治疗每日人均消耗 {{ medCareCostText.treating }}；隔离每日人均消耗 {{ medCareCostText.isolated }}（隔离可阻断疫病扩散）。健康低于 70 可手动登记，低于 35 每日巡检自动建档。</p>
            <div class="med-watch">
              <div v-for="r in inBunkerAlive.filter(x => canRegister(x))" :key="r.id" class="med-watch-item">
                <span class="p-avatar">{{ r.name[0] }}</span>
                <span>{{ r.name }}</span>
                <span class="dim">{{ r.job_zh }} · 健康 {{ fmt(r.health) }}</span>
                <button class="btn tiny" :disabled="actionLocked"
                        @click="registerCase(r.id, false)">登记伤病</button>
                <button class="btn tiny danger" :disabled="actionLocked"
                        @click="registerCase(r.id, true)">登记疫病</button>
              </div>
              <div v-if="!inBunkerAlive.some(x => canRegister(x))" class="dim">在堡居民健康状况良好，暂无需登记。</div>
            </div>

            <!-- 救治履历 -->
            <h3 class="med-h3" v-if="medHistoryCases().length">救治履历（最近 {{ medHistoryCases().length }} 例）</h3>
            <div class="med-history" v-if="medHistoryCases().length">
              <div v-for="c in medHistoryCases()" :key="c.id" class="med-hist-item" :class="c.status">
                <span class="chip" :class="c.status">{{ caseStatusZh(c.status) }}</span>
                <b>{{ c.resident_name }}</b>
                <span class="dim">{{ c.infectious ? '疫病' : (c.kind || '伤病') }} · {{ c.reason }}
                  · 第 {{ c.open_day }} 天登记<template v-if="c.close_day"> / 第 {{ c.close_day }} 天结案</template>
                  <template v-if="c.days_cared"> · 救治 {{ c.days_cared }} 床日</template></span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 扩建 -->
      <div v-if="tab==='build'">
        <div class="build-grid">
          <div v-for="b in buildings" :key="b.category" class="build-card">
            <span class="bc-name">{{ b.name }}</span>
            <span class="dim">等级加成 x1.6</span>
            <div class="cost" v-for="(v,k) in b.cost" :key="k">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }}</div>
            <button class="btn small primary" :disabled="s.status!=='running' || actionLocked" @click="build(b.category)">建造</button>
          </div>
        </div>
      </div>

      <!-- 大事记 -->
      <div v-if="tab==='log'" class="logs">
        <div v-for="l in [...s.logs].reverse()" :key="l.id" class="log" :class="l.event_type">
          <span class="log-day">D{{ l.day }}</span>
          <div class="log-txt"><strong>{{ l.title }}</strong><p>{{ l.detail }}</p></div>
        </div>
      </div>
    </div>

    <!-- 结局弹层 -->
    <div v-if="s.status !== 'running'" class="overlay">
      <div class="ending" :class="s.status">
        <h2>{{ s.status === 'win' ? '曙光降临' : '地堡永寂' }}</h2>
        <p>{{ s.outcome.reason }}</p>
        <div class="end-stats">
          <div><span>存活天数</span><b>{{ s.outcome.day }}</b></div>
          <div><span>幸存者</span><b>{{ s.outcome.survivors }}</b></div>
          <div><span>平均健康</span><b>{{ s.outcome.avg_health == null ? '-' : s.outcome.avg_health }}</b></div>
          <div><span>得分</span><b>{{ s.score }}</b></div>
        </div>
        <div v-if="s.outcome.medical && s.outcome.medical.total" class="med-end">
          医疗救治：登记 {{ s.outcome.medical.total }} 例 · 康复 {{ s.outcome.medical.recovered }} 人 ·
          病亡 {{ s.outcome.medical.deceased }} 人 · 累计 {{ s.outcome.medical.care_days }} 床日
        </div>
        <button class="btn primary" @click="onExit">返回档案列表</button>
      </div>
    </div>

    <!-- 危机弹层 -->
    <div v-if="crisis" class="overlay">
      <div class="crisis">
        <h2>⚡ {{ crisis.title }}</h2>
        <p class="crisis-desc">{{ crisis.desc }}</p>
        <div v-if="crisis.needs_target" class="crisis-tgt">
          相关居民：{{ crisis.target_name }}<span class="dim">（仅标注「单人」的决策作用于本人，其余对全体生效）</span>
        </div>
        <div class="choices">
          <button v-for="c in crisis.choices" :key="c.key" class="choice" @click="resolve(c)">
            <strong>{{ c.label }}</strong>
            <span class="scope-tag" :class="{ solo: c.targeted }">{{ c.targeted ? '单人' : '全体' }}</span>
            <span class="hint">{{ c.hint }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 探索遭遇弹层 -->
    <div v-if="expPending" class="overlay">
      <div class="crisis expedition">
        <h2>🧭 {{ s.expedition.pending_encounter.title }}</h2>
        <p class="crisis-desc">{{ s.expedition.pending_encounter.desc }}</p>
        <div v-if="s.expedition.pending_encounter.needs_target" class="crisis-tgt">
          相关队员：{{ s.expedition.pending_encounter.target_name }}<span class="dim">（仅标注「单人」的决策作用于本人，其余对全体队员生效）</span>
        </div>
        <div class="choices">
          <button v-for="c in s.expedition.pending_encounter.choices" :key="c.key" class="choice" @click="resolveExpeditionEncounter(c)">
            <strong>{{ c.label }}</strong>
            <span class="scope-tag" :class="{ solo: c.targeted }">{{ c.targeted ? '单人' : '全体' }}</span>
            <span class="hint">{{ c.hint }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 贸易途中事件弹层 -->
    <div v-if="tradePending" class="overlay">
      <div class="crisis trade">
        <h2>🤝 {{ s.trade_order.pending_incident.title }}</h2>
        <p class="crisis-desc">{{ s.trade_order.pending_incident.desc }}</p>
        <div v-if="s.trade_order.pending_incident.needs_target" class="crisis-tgt">
          相关队员：{{ s.trade_order.pending_incident.target_name }}<span class="dim">（仅标注「单人」的决策作用于本人，其余对全体押运队员生效）</span>
        </div>
        <div class="choices">
          <button v-for="c in s.trade_order.pending_incident.choices" :key="c.key" class="choice" @click="resolveTradeIncident(c)">
            <strong>{{ c.label }}</strong>
            <span class="scope-tag" :class="{ solo: c.targeted }">{{ c.targeted ? '单人' : '全队' }}</span>
            <span class="hint">{{ c.hint }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 贸易市场弹层 -->
    <div v-if="showTradeDialog" class="overlay">
      <div class="crisis trade market-dialog">
        <h2>📡 外部聚落通讯</h2>
        <p class="crisis-desc">第 {{ market.day }} 天的报价（市场每日轮换）。当前信誉 <b>{{ market.reputation }}</b>，信誉越高审核与交付越顺利。</p>
        <!-- 报价列表 / 押运队员选择 两级视图 -->
        <template v-if="!tradeOffer">
          <div class="market-list">
            <div v-for="o in market.offers" :key="o.id" class="market-offer" :class="o.type" @click="pickTradeOffer(o)">
              <div class="mo-head">
                <span class="chip" :class="o.type">{{ o.type === 'rescue' ? '求援' : '采购' }}</span>
                <strong>{{ o.partner_name }}</strong>
                <span class="dim">单程约 {{ o.eta }} 天</span>
              </div>
              <div class="mo-flow">
                <span>付出：<b>{{ fmtTradeBags(o.escrow) }}</b></span>
                <span>→</span>
                <span class="loot">{{ o.type === 'rescue' ? '回礼' : '到货' }}：<b>{{ fmtTradeBags(o.cargo) }}</b></span>
              </div>
              <div class="dim">{{ o.hint }}</div>
            </div>
          </div>
          <div class="choices">
            <button class="choice" @click="showTradeDialog=false"><strong>关闭</strong></button>
          </div>
        </template>
        <template v-else>
          <div class="market-offer" :class="tradeOffer.type">
            <div class="mo-head">
              <span class="chip" :class="tradeOffer.type">{{ tradeOffer.type === 'rescue' ? '求援' : '采购' }}</span>
              <strong>{{ tradeOffer.partner_name }}</strong>
              <span class="dim">单程约 {{ tradeOffer.eta }} 天</span>
            </div>
            <div class="mo-flow">
              <span>托管：<b>{{ fmtTradeBags(tradeOffer.escrow) }}</b></span>
              <span>→</span>
              <span class="loot">{{ tradeOffer.type === 'rescue' ? '回礼' : '到货' }}：<b>{{ fmtTradeBags(tradeOffer.cargo) }}</b></span>
            </div>
          </div>
          <p class="dim" style="margin:10px 0 4px">选择押运队员（1-3 人，须在堡且存活）：</p>
          <div class="exp-member-pick">
            <div v-for="r in inBunkerAlive" :key="r.id" class="exp-member"
                 :class="{ selected: tradeEscorts.includes(r.id) }" @click="toggleTradeEscort(r.id)">
              <span class="p-avatar">{{ r.name[0] }}</span>
              <span>{{ r.name }}</span>
              <span class="dim">{{ r.job_zh }}</span>
            </div>
            <div v-if="!inBunkerAlive.length" class="dim">没有可派出的在堡居民</div>
          </div>
          <div class="choices">
            <button class="choice primary-choice" :disabled="loading || !tradeEscorts.length" @click="submitTrade">
              <strong>{{ loading ? '提交中…' : '冻结托管并提交申请' }}</strong>
            </button>
            <button class="choice" @click="tradeOffer=null"><strong>返回报价列表</strong></button>
          </div>
        </template>
      </div>
    </div>

    <!-- 派遣探索队弹层 -->
    <div v-if="showExpeditionDialog" class="overlay">
      <div class="crisis expedition">
        <h2>派遣探索队</h2>
        <p class="crisis-desc">选择在堡居民（最多 4 人）并分配自带物资。离堡人员暂停地堡生产，不消耗地堡口粮。</p>
        <div class="exp-member-pick">
          <div v-for="r in inBunkerAlive" :key="r.id" class="exp-member" :class="{ selected: expMembers.includes(r.id) }" @click="toggleMember(r.id)">
            <span class="p-avatar">{{ r.name[0] }}</span>
            <span>{{ r.name }}</span>
            <span class="dim">{{ r.job_zh }}</span>
          </div>
          <div v-if="!inBunkerAlive.length" class="dim">没有可派遣的在堡居民</div>
        </div>
        <div class="exp-supplies">
          <label>自带食物 <input type="number" min="0" v-model.number="expSupplies.food" /></label>
          <label>自带饮水 <input type="number" min="0" v-model.number="expSupplies.water" /></label>
          <span class="dim">每人每日消耗 1 食物 + 1 水</span>
        </div>
        <div class="choices">
          <button class="choice primary-choice" @click="sendExpedition" :disabled="loading">
            <strong>{{ loading ? '派遣中…' : '出发' }}</strong>
          </button>
          <button class="choice" @click="showExpeditionDialog=false"><strong>取消</strong></button>
        </div>
      </div>
    </div>
  </div>`,
};