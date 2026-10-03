#!/usr/bin/env node
/**
 * 末日地堡生存 —— 一键管理脚本（npm scripts 与 Python 环境之间的桥）。
 *
 * 用法（推荐通过 npm 调用）：
 *   npm run setup    安装 Python 依赖并初始化数据库（首次使用执行一次）
 *   npm start        一键启动前后端（生产模式，默认 http://127.0.0.1:8000）
 *   npm run dev      开发模式启动（代码变更自动重载）
 *   npm run init-db  仅初始化/补齐数据库（幂等，可重复执行）
 *   npm test         运行 pytest 测试套件（-- 之后的参数透传给 pytest）
 *
 * 环境变量：
 *   PYTHON  指定 Python 解释器（默认依次探测 python3 / python）
 *   HOST    监听地址（默认 127.0.0.1）
 *   PORT    监听端口（默认 8000）
 */
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const HOST = process.env.HOST || "127.0.0.1";
const PORT = process.env.PORT || "8000";

// ---------- 基础工具 ----------

function fail(msg) {
  console.error(`\n✗ ${msg}`);
  process.exit(1);
}

/** 依次尝试候选命令，返回可用的 Python 解释器。 */
function findPython() {
  const candidates = [process.env.PYTHON, "python3", "python"].filter(Boolean);
  for (const cmd of candidates) {
    const r = spawnSync(cmd, ["--version"], { stdio: "ignore" });
    if (!r.error && r.status === 0) return cmd;
  }
  return null;
}

const PYTHON = findPython();

function requirePython() {
  if (!PYTHON) {
    fail("未找到 Python 解释器。请先安装 Python 3.10+，或通过 PYTHON=/path/to/python 指定。");
  }
}

/** 同步执行并透传 stdio；非零退出码直接终止。 */
function run(args) {
  const r = spawnSync(args[0], args.slice(1), { cwd: ROOT, stdio: "inherit" });
  if (r.error) fail(`无法执行 ${args[0]}：${r.error.message}`);
  if (r.status !== 0) process.exit(r.status == null ? 1 : r.status);
}

const py = (args) => run([PYTHON, ...args]);

/** 关键依赖是否已安装。 */
function depsInstalled() {
  const r = spawnSync(PYTHON, ["-c", "import fastapi, uvicorn, sqlalchemy, pydantic"], {
    cwd: ROOT,
    stdio: "ignore",
  });
  return r.status === 0;
}

function requireDeps() {
  if (!depsInstalled()) {
    fail("检测到 Python 依赖未安装，请先执行：npm run setup");
  }
}

// ---------- 子命令 ----------

/** 确保 pip 可用（部分精简系统未自带）。 */
function ensurePip() {
  const r = spawnSync(PYTHON, ["-m", "pip", "--version"], { cwd: ROOT, stdio: "ignore" });
  if (r.status === 0) return;
  console.log("▶ 未检测到 pip，尝试通过 ensurepip 引导……");
  const boot = spawnSync(PYTHON, ["-m", "ensurepip", "--upgrade"], { cwd: ROOT, stdio: "inherit" });
  if (boot.status !== 0) {
    fail("无法自动安装 pip，请手动安装（如 Debian/Ubuntu：sudo apt install python3-pip）后重试。");
  }
}

/** PEP 668：系统 Python 被标记为 externally-managed 时，pip 需要显式授权参数。 */
function pipNeedsBreakFlag() {
  const code =
    "import os,sys,sysconfig;" +
    "sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path('stdlib'),'EXTERNALLY-MANAGED')) else 1)";
  const r = spawnSync(PYTHON, ["-c", code], { cwd: ROOT, stdio: "ignore" });
  return r.status === 0;
}

function setup() {
  requirePython();
  console.log(`▶ 使用 Python 解释器：${PYTHON}`);
  ensurePip();
  const args = ["-m", "pip", "install", "-r", "requirements.txt"];
  if (pipNeedsBreakFlag()) args.push("--break-system-packages");
  console.log("\n▶ 安装 Python 依赖（pip install -r requirements.txt）……");
  py(args);
  initDb();
  console.log("\n✓ 环境就绪！执行 npm start 即可一键启动游戏。");
}

function initDb() {
  requirePython();
  console.log("▶ 初始化数据库（幂等，可重复执行）……");
  py(["scripts/init_db.py"]);
}

/** 以前台进程方式启动 uvicorn，转发退出信号并透传退出码。 */
function serve(extraArgs) {
  requirePython();
  requireDeps();
  initDb();
  const args = ["-m", "uvicorn", "app.main:app", "--host", HOST, "--port", PORT, ...extraArgs];
  console.log("▶ 启动服务（前端静态资源由后端同端口托管，无需单独启动前端）");
  console.log(`  ➜ 游戏入口: http://${HOST}:${PORT}\n`);
  const child = spawn(PYTHON, args, { cwd: ROOT, stdio: "inherit" });
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => child.kill(sig));
  }
  child.on("error", (err) => fail(`启动失败：${err.message}`));
  child.on("exit", (code) => process.exit(code == null ? 0 : code));
}

function test(extra) {
  requirePython();
  requireDeps();
  py(["-m", "pytest", "tests", "-q", ...extra]);
}

// ---------- 入口 ----------

function usage() {
  console.log(`用法：node scripts/manage.js <命令>（一般通过 npm scripts 调用）

命令：
  setup     安装 Python 依赖并初始化数据库（首次使用执行一次）
  start     一键启动前后端（生产模式）
  dev       开发模式启动（uvicorn --reload）
  init-db   仅初始化/补齐数据库（幂等）
  test      运行 pytest 测试套件（可透传 pytest 参数）

环境变量：PYTHON（解释器路径）/ HOST（默认 127.0.0.1）/ PORT（默认 8000）`);
}

const [, , cmd = "help", ...rest] = process.argv;
const commands = {
  setup: () => setup(),
  "init-db": () => initDb(),
  start: () => serve(rest),
  dev: () => serve(["--reload", ...rest]),
  test: () => test(rest),
};

if (Object.prototype.hasOwnProperty.call(commands, cmd)) {
  commands[cmd]();
} else {
  if (cmd !== "help" && cmd !== "--help" && cmd !== "-h") {
    console.error(`未知命令：${cmd}\n`);
  }
  usage();
  process.exit(cmd === "help" || cmd === "--help" || cmd === "-h" ? 0 : 1);
}
