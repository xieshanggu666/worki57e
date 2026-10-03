// 末日地堡生存 —— 前端 API 封装
const Api = {
  async request(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      const err = new Error(j.detail || "请求失败");
      err.status = res.status; // 409=并发落败/状态过期，调用方据此拉取最新档案
      throw err;
    }
    return res.status === 204 || res.status === 200 ? res.json() : res.json();
  },
  get(url) { return this.request("GET", url); },
  post(url, body) { return this.request("POST", url, body); },
  del(url) { return this.request("DELETE", url); },
};

const zhResource = { food: "食物", water: "水源", power: "电力", oxygen: "氧气" };
const zhJob = { engineer: "工程师", farmer: "农民", general: "杂工" };
const zhStatus = { running: "进行中", win: "胜利", over: "失败" };