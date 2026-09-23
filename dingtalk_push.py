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


def send(title: str, text: str) -> tuple[bool, str]:
    """发送 markdown 消息到配置的群机器人. 返回 (是否成功, 说明)."""
    pc = _load_push_config()
    if not pc["webhook"]:
        return False, "未配置 dingtalk_webhook(config.json)"
    url = _signed_url(pc["webhook"], pc["secret"]) if pc["secret"] else pc["webhook"]
    payload = {"msgtype": "markdown",
               "markdown": {"title": title, "text": f"### {title}\n\n{text}"}}
    try:
        r = requests.post(url, json=payload, timeout=15)
        data = r.json()
    except Exception as e:
        return False, f"请求异常: {e}"
    if data.get("errcode") == 0:
        return True, "已发送"
    return False, (f"errcode={data.get('errcode')} {data.get('errmsg')}"
                   " (若为关键词校验失败, 请把机器人关键词加进消息标题)")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="钉钉群机器人推送")
    ap.add_argument("--title", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--dry", action="store_true", help="只打印不发送")
    args = ap.parse_args()
    pc = _load_push_config()
    if args.dry:
        print(f"[dry] webhook: {pc['webhook'] or '(未配置)'}"
              f"{' (加签)' if pc['secret'] else ''}")
        print(f"[dry] payload: title={args.title!r} text={args.text[:80]!r}")
        return 0
    ok, msg = send(args.title, args.text)
    print(f"[推送] {'成功' if ok else '失败'}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
