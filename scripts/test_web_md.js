#!/usr/bin/env node
// scripts/test_web_md.js — 前端 markdown 渲染器纯函数断言测试(零依赖, 零 LLM)
// 从 static/index.html 抽出渲染器相关纯函数, 间接 eval 进全局后逐项断言。
"use strict";
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.resolve(__dirname, "..", "static", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/);
let fails = 0;
const ck = (name, cond) => { console.log((cond ? "PASS " : "FAIL ") + name); if (!cond) fails++; };

if (!script) { console.log("FAIL 未找到 <script> 段"); process.exit(1); }
const js = script[1];
const start = js.indexOf("const esc =");
const end = js.indexOf("function addBotMd");
if (start < 0 || end < 0 || end <= start) {
  console.log("FAIL 抽取锚点丢失(esc/addBotMd), 渲染器结构可能被改坏");
  process.exit(1);
}
(0, eval)(js.slice(start, end));  // 间接 eval → 函数声明进全局
const renderMarkdown = globalThis.renderMarkdown;
if (typeof renderMarkdown !== "function") { console.log("FAIL renderMarkdown 不存在"); process.exit(1); }

let h = renderMarkdown("# 标题\n\n**粗体** and *斜体* and `code`\n\n- 项目1\n- 项目2\n\n| A | B |\n|---|---|\n| 1 | 2 |\n");
ck("标题", h.includes("<h2>标题</h2>"));
ck("粗体", h.includes("<strong>粗体</strong>"));
ck("斜体", h.includes("<em>斜体</em>"));
ck("行内码", h.includes('<code class="ic">code</code>'));
ck("列表", h.includes("<ul>") && h.includes("<li>项目1</li>") && h.includes("<li>项目2</li>"));
ck("表格", h.includes("<th>A</th>") && h.includes("<td>1</td>"));

h = renderMarkdown("```python\nprint('<b>hi</b>')\n```");
ck("代码块语言头", h.includes('<div class="cbh">python</div>'));
ck("代码块内容转义", h.includes("&lt;b&gt;hi&lt;/b&gt;") && !h.includes("<b>hi</b>"));

h = renderMarkdown("<script>alert(1)</script>\n\n```html\n<script>x</script>\n```");
ck("正文脚本转义", h.includes("&lt;script&gt;") && !h.includes("<script>alert"));
ck("代码块内脚本转义", !h.includes("<script>x</script>"));

h = renderMarkdown("未闭合的 ```python\ncode = 1\n");
ck("未闭合围栏不崩溃", typeof h === "string" && h.length > 0);

h = renderMarkdown("段落一\n段落二\n\n> 引用行\n> 第二行\n");
ck("多行段落合并", h.includes("<p>段落一<br>段落二</p>"));
ck("引用多行合并", h.includes("<blockquote>引用行<br>第二行</blockquote>"));

h = renderMarkdown("[链接](https://example.com/a?b=1)");
ck("链接", h.includes('href="https://example.com/a?b=1"') && h.includes(">链接</a>"));

ck("空输入", renderMarkdown("") === "");

h = renderMarkdown("1. 有序A\n2. 有序B");
ck("有序列表", h.includes("<ol>") && h.includes("<li>有序A</li>"));

ck("产物识别正则在位", js.includes("已写入 (.+?) \\("));
ck("预览抽屉接入", js.includes("openPreview"));

console.log(fails ? "FAILED" : "ALL PASS");
process.exit(fails ? 1 : 0);
