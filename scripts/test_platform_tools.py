#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_platform_tools.py — AI助手嵌入 T06 platform_* 工具 离线测试(零 LLM)

覆盖: 注册契约(10个 platform_* 全 level=read) / 字段白名单脱敏(剔人名键+
      11位手机号掩码, 双保险) / _plat_brief 渲染与截断 / 降级返回(后端不可用
      →人话文本, 不抛异常) / trust_env=False(防本机代理劫持, 任务书 §5.1-4) /
      真实联动(平台后端在跑时才执行, 否则 SKIP)。
"""
import os
import sys
from pathlib import Path

# ⚠️ 摘掉沙箱注入的代理(同 test_auth.py)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "plugins"))   # load_plugins 同款: 插件按目录导入
os.chdir(HERE)

import json  # noqa: E402

import agentcore as core          # noqa: E402
import workmain as wm             # noqa: E402  (plugins/workmain.py)

fails = []
skip_real = False


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


class _Reg:
    """最小 registry 桩: 记录注册的 Tool。"""
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool


def _mk_tools(cfg):
    r = _Reg()
    wm.register(r, cfg)
    return r


# ---------- 注册契约 ----------

def test_contract():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    core.set_root(cfg.get("workmain_root"))
    reg = core.build_registry()
    core.load_plugins(reg, cfg)
    plat = [t for n, t in reg._tools.items() if n.startswith("platform_")]
    names = {t.name for t in plat}
    expect = {"platform_batches", "platform_overview", "platform_sku_route",
              "platform_alerts", "platform_transport", "platform_records",
              "platform_inflight_overview", "platform_inflight_alerts",
              "platform_daily_report", "platform_deltas"}
    check("契约: 10个platform_*齐全", names == expect,
          extra=str(names ^ expect))
    check("契约: 全部 level=read", all(t.level == "read" for t in plat))
    check("契约: 原有8个workmain工具保留",
          all(n in reg._tools for n in
              ("query_batches", "query_sku_route", "run_route_check",
               "pipeline_alerts", "scan_sync_data")))
    check("契约: platform_root 从 config 读取",
          cfg.get("platform_root") == "http://127.0.0.1:8000")


# ---------- 脱敏(双保险) ----------

def test_sanitize():
    d = wm._plat_sanitize({"sku": "G531", "型号": "Poco X8",
                           "当前负责人": "胡浩明", "创建人": "林宇炫",
                           "owner": "someone", "remark": "内部备注",
                           "records": [{"操作人": "张三", "节点": "签样"}]})
    check("脱敏: 中文敏感键剔除", "当前负责人" not in d and "创建人" not in d)
    check("脱敏: 英文敏感键剔除", "owner" not in d and "remark" not in d)
    check("脱敏: 业务键保留", d.get("sku") == "G531" and d.get("型号") == "Poco X8")
    check("脱敏: 嵌套list内键剔除",
          d.get("records") == [{"节点": "签样"}])

    txt = wm._plat_sanitize("联系 13812345678 处理, 备用号 15987654321")
    check("脱敏: 11位手机号掩码",
          "138****5678" in txt and "159****4321" in txt
          and "13812345678" not in txt and "15987654321" not in txt)
    check("脱敏: 非11位手机号形状不掩码",
          wm._plat_sanitize("单号 12345678901 前缀") == "单号 12345678901 前缀")
    check("脱敏: 非字符串标量原样", wm._plat_sanitize(42) == 42)


# ---------- 渲染 ----------

def test_brief():
    lines = wm._plat_brief({"a": 1, "b": [1, 2], "c": {"d": "x"}, "e": None})
    txt = "\n".join(lines)
    check("渲染: 标量键值", "a: 1" in txt)
    check("渲染: None显示—", "e: —" in txt)
    check("渲染: 嵌套dict缩进", any(l.startswith("  d: x") for l in lines))
    check("渲染: list条目", "- 1" in txt)
    many = wm._plat_brief({"rows": [{"i": i} for i in range(200)]}, budget=10)
    check("渲染: budget截断", len(many) <= 12 and any("截断" in l for l in many))


# ---------- 降级(后端不可用 → 人话文本, 不抛异常) ----------

def test_degrade():
    reg = _mk_tools({"platform_root": "http://127.0.0.1:9",
                     "workmain_root": ""})
    check("降级: 10个工具已注册", len([n for n in reg.tools
                                       if n.startswith("platform_")]) == 10)
    cases = [("platform_batches", {}),
             ("platform_overview", {"batch": "20261008"}),
             ("platform_sku_route", {"sku": "G531"}),
             ("platform_alerts", {}),
             ("platform_transport", {}),
             ("platform_records", {"batch": "20261008", "part": "Part1"}),
             ("platform_inflight_overview", {}),
             ("platform_inflight_alerts", {}),
             ("platform_daily_report", {}),
             ("platform_deltas", {})]
    for name, kwargs in cases:
        try:
            out = reg.tools[name].func(**kwargs)
            check(f"降级: {name} 返回人话文本",
                  isinstance(out, str) and "[平台接口不可用]" in out)
        except Exception as e:  # 不应走到这里
            check(f"降级: {name} 不抛异常", False, extra=repr(e))


# ---------- trust_env(防本机代理劫持) ----------

def test_trust_env():
    check("trust_env: session.trust_env=False",
          wm._plat_session().trust_env is False)


# ---------- 真实联动(平台后端在跑时) ----------

def test_real_backend():
    global skip_real
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    base = (cfg.get("platform_root") or "http://127.0.0.1:8000").rstrip("/")
    import requests
    s = requests.Session()
    s.trust_env = False
    try:
        r = s.get(f"{base}/api/health", timeout=2)
        up = r.status_code == 200
    except Exception:
        up = False
    if not up:
        print("SKIP 真实联动: 平台后端未启动(仅离线用例已覆盖)")
        skip_real = True
        return
    reg = _mk_tools(cfg)
    out = reg.tools["platform_batches"].func("手机膜")
    check("真实: 批次列表可用", "[平台接口不可用]" not in out and out.strip())
    out2 = reg.tools["platform_alerts"].func("手机膜")
    import re
    check("真实: 输出无完整11位手机号",
          not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", out2))
    check("真实: 输出无'负责人'键", "负责人" not in out2)


def main() -> int:
    test_contract()
    test_sanitize()
    test_brief()
    test_trust_env()
    test_degrade()
    test_real_backend()
    tag = "全部通过" if not fails else f"失败 {len(fails)} 项"
    print(f"\n== {tag} ==")
    for f in fails:
        print("  FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
