// 末日地堡生存 —— 应用入口
const { createApp, ref } = Vue;

const App = {
  components: { LobbyView, GameView },
  data() {
    return { currentSid: null, showLobby: true };
  },
  methods: {
    enter(sid) { this.currentSid = sid; this.showLobby = false; },
    exit() { this.showLobby = true; this.currentSid = null; },
  },
  template: `
    <div class="app">
      <LobbyView v-if="showLobby" :on-enter="enter" />
      <GameView v-else :sid="currentSid" :on-exit="exit" :key="currentSid" />
    </div>`,
};

createApp(App).mount("#app");