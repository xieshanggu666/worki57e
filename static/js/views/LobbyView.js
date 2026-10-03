/* 末日地堡生存 —— 视界界面 */
window.LobbyView = {
  props: ["onEnter"],
  data() {
    return { sessions: [], name: "末日地堡档案", loading: false, error: "" };
  },
  created() { this.load(); },
  methods: {
    async load() {
      try { this.sessions = await Api.get("/api/sessions"); } catch (e) { this.error = e.message; }
    },
    async create() {
      this.error = "";
      this.loading = true;
      try {
        const s = await Api.post("/api/sessions", { name: this.name });
        this.onEnter(s.id);
      } catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
    async remove(sid) {
      if (!confirm("确定删除该档案？")) return;
      await Api.del(`/api/sessions/${sid}`);
      this.load();
    },
    statusZh(s) { return (s.status === "win" ? "胜利" : s.status === "over" ? "失败" : "进行中"); },
  },
  template: `
  <div class="lobby">
    <div class="lobby-hero">
      <h1>末日地堡生存</h1>
      <p>地表灰烬飘落，地堡深处，你们是最后的火种。管理资源、安置幸存者、应对危机，坚持到第120天。</p>
    </div>
    <div class="lobby-panel">
      <h2>新建档案</h2>
      <div class="row">
        <input v-model="name" placeholder="档案名称" class="input" />
        <button class="btn primary" :disabled="loading" @click="create">开始生存</button>
      </div>
      <div v-if="error" class="msg err">{{ error }}</div>
    </div>
    <div class="lobby-panel">
      <h2>已有档案</h2>
      <div v-if="!sessions.length" class="dim">暂无档案</div>
      <div v-for="s in sessions" :key="s.id" class="save-row">
        <div class="save-meta">
          <strong>{{ s.name }}</strong>
          <span class="chip" :class="s.status">{{ statusZh(s) }}</span>
          <span>第{{ s.day }}/{{ s.target_day }}天 · {{ s.survivors }}人 · 得分{{ s.score }}</span>
        </div>
        <div class="save-actions">
          <button v-if="s.status === 'running'" class="btn small" @click="onEnter(s.id)">继续</button>
          <button v-else class="btn small ghost" @click="onEnter(s.id)">查看</button>
          <button class="btn small danger" @click="remove(s.id)">删除</button>
        </div>
      </div>
    </div>
  </div>`,
};