#!/usr/bin/env node
// scripts/test_web_ui.js — Web 前端装配防线(零依赖, 零 LLM, node 运行)
//
// 背景(2026-10-13 故障): static/index.html 的内联脚本在顶层无条件执行
//   $("logoutbtn").onclick = ...
// 而 HTML 里从来没有 id="logoutbtn" 的元素 → null.onclick 抛 TypeError →
// 同一 <script> 里后续全部不执行(发送/停止/新会话/主题/文件面板全哑) →
// 用户端表现"发送点了没反应", 且页面看起来哪都正常(静态 HTML/CSS 照常渲染)。
// 75d4d85 引入 JS 引用时漏加 HTML 元素, 没有任何测试覆盖前端装配, 带病运行多日。
//
// 防线三层:
//   T1 静态: 顶层 wiring(`$("id").prop=`/addEventListener)引用的 id 必须都在 HTML 里
//     定义 —— 缺一个就等于整段脚本判死刑, 这是本次故障的准确形状。
//   T2 静态: 任何 `$("id")` 引用必须存在于 HTML id 或 JS 动态创建的 id 集合里。
//   T3 动态: 把页面脚本原样塞进 vm 沙箱(桩 DOM/localStorage/fetch/EventSource)
//     真实执行到 newSession() 收尾: 不抛异常、无 unhandledRejection、
//     发送/停止/新会话按钮真的绑上了事件、history 真的被请求过。
//   T4 变异自检: 删掉发送按钮后, T1 与 T3 必须同时失败(证明防线测得出来,
//     防止以后有人把测试改弱)。
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML_PATH = path.resolve(__dirname, "..", "static", "index.html");
const html = fs.readFileSync(HTML_PATH, "utf8");

let fails = 0;
const ck = (name, cond, detail) => {
  console.log((cond ? "PASS " : "FAIL ") + name + (!cond && detail ? `  [${detail}]` : ""));
  if (!cond) fails++;
};

// 剥掉整行 // 注释与 /* */ 块注释, 防止注释文本里的示例代码污染静态分析
// (2026-10-13: 测试自己的警示注释 "$(\"id\")" 就被误扫成一次引用)
const stripComments = (s) => s
  .replace(/^\s*\/\/.*$/gm, "")
  .replace(/\/\*[\s\S]*?\*\//g, "");

// ---------- 抽取 ----------
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log("FAIL 未找到 <script> 段"); process.exit(1); }
const js = m[1];
const htmlPart = html.slice(0, m.index);          // <script> 之前=真 HTML
const jsClean = stripComments(js);                // 静态分析用(剥注释); T3 动态执行仍用原版

const htmlIds = new Set([...htmlPart.matchAll(/id="([^"]+)"/g)].map((x) => x[1]));
// JS 动态创建的 id: 模板字符串里的 id="..." 与 .id = "..."(过滤 ${...} 占位符误捕获)
const jsIds = new Set([
  ...[...jsClean.matchAll(/id="([\w-]+)"/g)].map((x) => x[1]),
  ...[...jsClean.matchAll(/\.id\s*=\s*"([\w-]+)"/g)].map((x) => x[1]),
]);

// ---------- T1: 顶层 wiring 的 id 必须都在 HTML 里 ----------
// 顶层 = 行首无缩进的 `$("id").prop =` / `$("id").addEventListener(`
const topWiring = [...jsClean.matchAll(/^\$\("([\w-]+)"\)\.(?:onclick|onchange|addEventListener)\b/gm)]
  .map((x) => x[1]);
ck("T1 顶层 wiring 非空(抽样: send/stopbtn/newbtn)", topWiring.length >= 5);
const missingTop = topWiring.filter((id) => !htmlIds.has(id));
ck("T1 顶层 wiring 引用的 id 全部存在于 HTML", missingTop.length === 0,
   "缺失: " + missingTop.join(","));
ck("T1 显式钉住: 退出登录按钮存在于 HTML(鉴权退出路径依赖它)",
   htmlIds.has("logoutbtn"));

// ---------- T2: 所有 $("id") 引用有出处 ----------
const used = new Set([...jsClean.matchAll(/\$\("([\w-]+)"\)/g)].map((x) => x[1]));
const orphans = [...used].filter((id) => !htmlIds.has(id) && !jsIds.has(id));
ck("T2 所有 $() 引用均有出处(HTML 或 JS 动态创建)", orphans.length === 0,
   "无出处: " + orphans.join(","));

// ---------- T3: 沙箱里真实执行页面脚本 ----------
function makeEl() {
  const listeners = [];
  return {
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    innerHTML: "", textContent: "", value: "", disabled: false, checked: false,
    title: "", href: "", scrollTop: 0, scrollHeight: 0,
    appendChild() {}, prepend() {}, remove() {},
    setAttribute() {}, removeAttribute() {},
    addEventListener(ev) { listeners.push(ev); },
    querySelector() { return null; },
    querySelectorAll() { return { forEach() {} }; },
    onclick: null, onchange: null, onkeydown: null,
    _listeners: listeners,
  };
}

function runPage(htmlText) {
  const script = htmlText.match(/<script>([\s\S]*?)<\/script>/);
  if (!script) return { threw: new Error("no <script>") };
  const part = htmlText.slice(0, script.index);
  const ids = new Set([...part.matchAll(/id="([^"]+)"/g)].map((x) => x[1]));
  const els = new Map();
  const fetchCalls = [];
  const unhandled = [];
  const onUnhandled = (e) => unhandled.push(e);

  const fetchStub = async (url) => {
    const u = String(url);
    fetchCalls.push(u);
    let data = {};
    if (u.includes("/api/me")) data = { user: "admin" };
    else if (u.includes("/history")) data = { items: [], running: false,
      usage: { calls: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } };
    else if (u.startsWith("/api/sessions")) data = [];
    else if (u.includes("/api/pending")) data = { items: [] };
    else if (u.includes("/api/interrupted")) data = { items: [] };
    else if (u.includes("/api/files")) data = { items: [] };
    return { ok: true, status: 200, json: async () => data, text: async () => "" };
  };

  class EventSourceStub {
    constructor(u) { this.url = u; this.onmessage = null; }
    close() {}
  }

  const documentStub = {
    body: makeEl(),
    getElementById(id) {
      if (!ids.has(id)) return null;          // 与真实 DOM 一致: 不存在→null
      if (!els.has(id)) els.set(id, makeEl());
      return els.get(id);
    },
    createElement() { return makeEl(); },
  };
  const localStorageStub = {
    _s: {},
    getItem(k) { return k in this._s ? this._s[k] : null; },
    setItem(k, v) { this._s[k] = String(v); },
  };

  const sandbox = {
    document: documentStub,
    localStorage: localStorageStub,
    fetch: fetchStub,
    EventSource: EventSourceStub,
    location: { href: "" },
    CSS: { escape: (s) => s },
    prompt: () => "", confirm: () => true, alert: () => "",
    console,
    setTimeout, clearTimeout,
  };
  sandbox.window = sandbox;                    // 浏览器里 window 即全局对象

  process.on("unhandledRejection", onUnhandled);
  let threw = null;
  try {
    vm.runInNewContext(script[1], sandbox, { filename: "index.html<script>" });
  } catch (e) {
    threw = e;
  }
  return { threw, els, fetchCalls, unhandled, sandbox,
           detach: () => process.removeListener("unhandledRejection", onUnhandled) };
}

(async () => {
  const r = runPage(html);
  await new Promise((res) => setTimeout(res, 200));   // 等异步 IIFE/openSession 收尾
  ck("T3 页面脚本执行不抛异常(整段跑完, 无 null 绑定)", r.threw === null,
     r.threw && String(r.threw));
  ck("T3 无 unhandledRejection", r.unhandled.length === 0,
   r.unhandled.map(String).join(" | "));
  ck("T3 发送按钮绑定了事件", r.els.get("send") && typeof r.els.get("send").onclick === "function");
  ck("T3 停止按钮绑定了事件", r.els.get("stopbtn") && typeof r.els.get("stopbtn").onclick === "function");
  ck("T3 新会话按钮绑定了事件", r.els.get("newbtn") && typeof r.els.get("newbtn").onclick === "function");
  ck("T3 输入框绑定了键盘事件",
     r.els.get("msg") && r.els.get("msg")._listeners.includes("keydown"));
  ck("T3 newSession→openSession 真的请求了 history",
     r.fetchCalls.some((u) => u.includes("/history")));
  ck("T3 退出登录按钮可见(鉴权下 initAuth 显示它)",
     r.els.get("logoutbtn") && r.els.get("logoutbtn").style.display === "");
  r.detach();

  // ---------- T4: 变异自检(删发送按钮 → T1/T3 必须失败) ----------
  const mutated = html.replace(/<button id="send"[^>]*>[\s\S]*?<\/button>/, "");
  const mutScript = mutated.match(/<script>([\s\S]*?)<\/script>/);
  const mutJs = stripComments(mutScript ? mutScript[1] : "");
  const mutHtmlPart = mutated.slice(0, mutated.indexOf("<script>"));
  const mutHtmlIds = new Set([...mutHtmlPart.matchAll(/id="([^"]+)"/g)].map((x) => x[1]));
  const mutWiring = [...mutJs.matchAll(/^\$\("([\w-]+)"\)\.(?:onclick|onchange|addEventListener)\b/gm)]
    .map((x) => x[1]);
  const mutMissing = mutWiring.filter((id) => !mutHtmlIds.has(id));
  const mr = runPage(mutated);
  await new Promise((res) => setTimeout(res, 100));
  ck("T4 变异(删发送按钮)被静态检出", mutMissing.includes("send"));
  ck("T4 变异(删发送按钮)被动态检出(脚本抛错)", mr.threw !== null,
     mr.threw && String(mr.threw));
  mr.detach();

  if (fails) { console.log(`\n${fails} 项失败`); process.exit(1); }
  console.log("\n全部通过 — 前端装配防线有效");
})();
