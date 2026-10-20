#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dingtalk_push.py — 钉钉出站推送(简报/提醒发给"你")

选了最简路径: 群自定义机器人 webhook —— 在钉钉群 → 群设置 → 智能群助手 →
添加机器人 → 自定义, 一分钟拿到 webhook 地址(和安全设置: 建议自定义关键词
如"简报", 或加签)。把地址填进 Agent/config.json:

  "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
  "dingtalk_webhook_secret": "SECxxx"        // 若机器人用"加签"则填, 关键词模式留空

企业应用工作通知(复用 work-main/dingtalk 凭据)需要 agent_id+userid,
等入站机器人开权限时一起做, 这里不猜。

用法:
  python dingtalk_push.py --title "测试" --text "hello" [--dry]
"""

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

# 钉钉 markdown 单条 text 官方上限 5000 字符, 留出标题/分片后缀的余量
MAX_MD_CHARS = 3800


def _load_push_config() -> dict:
    cfg_path = HERE / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8")) if cfg_path.exists() else {}
    return {"webhook": cfg.get("dingtalk_webhook") or "",
            "secret": cfg.get("dingtalk_webhook_secret") or ""}


def _signed_url(webhook: str, secret: str) -> str:
    ts = str(round(time.time() * 1000))
    sign = base64.b64encode(hmac.new(secret.encode("utf-8"),
                                     f"{ts}\n{secret}".encode("utf-8"),
                                     digestmod=hashlib.sha256).digest())
    return f"{webhook}&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"


def to_dingtalk_md(text: str) -> str:
    """把普通 Markdown 转成「钉钉渲染后真的换行」的版本。

    钉钉的 markdown 走标准 Markdown 语义: **段落内的单个 \\n 只是软换行,
    渲染时被折成空格** —— 所以 "• G598 …\\n• G599 …" 到群里就挤成一长行。
    而 "•" 不是 Markdown 列表标记(只有 - * + 是), 不会自动变成列表项。
    这里把每个非空行都变成独立段落(空行分隔); 表格行例外, 表格必须连续,
    否则表头和数据行会被拆散。
    """
    src = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    prev_table = False
    for raw in src.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        is_table = line.lstrip().startswith("|")
        if out:
            out.append("\n" if (is_table and prev_table) else "\n\n")
        out.append(line)
        prev_table = is_table
    return "".join(out)


def split_md(text: str, limit: int = MAX_MD_CHARS) -> list[str]:
    """按段落切分, 保证每片 ≤ limit 且不在行中间切断。"""
    paras = [p for p in (text or "").split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        cand = (cur + "\n\n" + p) if cur else p
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(p) <= limit:
            cur = p
            continue
        # 单段就超长(极罕见): 按行硬切, 仍不切半行
        for ln in p.split("\n"):
            if cur and len(cur) + len(ln) + 1 > limit:
                chunks.append(cur)
                cur = ln
            else:
                cur = (cur + "\n" + ln) if cur else ln
    if cur:
        chunks.append(cur)
    return chunks


def send(title: str, text: str, limit: int = MAX_MD_CHARS) -> tuple[bool, str]:
    """发送 markdown 消息到配置的群机器人. 返回 (是否成功, 说明).

    长文自动分片(钉钉单条有上限), 分片标题带 (i/n)。
    """
    pc = _load_push_config()
    if not pc["webhook"]:
        return False, "未配置 dingtalk_webhook(config.json)"
    url = _signed_url(pc["webhook"], pc["secret"]) if pc["secret"] else pc["webhook"]
    chunks = split_md(to_dingtalk_md(text), limit) or [""]
    n = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        head = f"### {title}" + (f" ({i}/{n})" if n > 1 else "")
        payload = {"msgtype": "markdown",
                   "markdown": {"title": title, "text": f"{head}\n\n{chunk}"}}
        try:
            r = requests.post(url, json=payload, timeout=15)
            data = r.json()
        except Exception as e:
            return False, f"第{i}/{n}片请求异常: {e}"
        if data.get("errcode") != 0:
            return False, (f"第{i}/{n}片 errcode={data.get('errcode')} "
                           f"{data.get('errmsg')}"
                           " (若为关键词校验失败, 请把机器人关键词加进消息标题)")
    return True, (f"已发送({n}片)" if n > 1 else "已发送")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="钉钉群机器人推送")
    ap.add_argument("--title", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--dry", action="store_true",
                    help="只打印转换后的 payload 不发送(校验换行/分片)")
    args = ap.parse_args()
    pc = _load_push_config()
    if args.dry:
        body = to_dingtalk_md(args.text)
        chunks = split_md(body)
        print(f"[dry] webhook: {pc['webhook'] or '(未配置)'}"
              f"{' (加签)' if pc['secret'] else ''}")
        print(f"[dry] 原始 {len(args.text)} 字 → 转换后 {len(body)} 字, "
              f"分 {len(chunks)} 片(每片≤{MAX_MD_CHARS})")
        for i, ch in enumerate(chunks, 1):
            print(f"[dry] ---- 第{i}片 {len(ch)} 字 ----")
            print(ch)
        return 0
    ok, msg = send(args.title, args.text)
    print(f"[推送] {'成功' if ok else '失败'}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
