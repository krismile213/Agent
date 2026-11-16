# -*- coding: utf-8 -*-
"""P1 只读探针：实测钉钉增量同步（T1 脉冲）的可行性。

回答分层同步框架 docs/分层同步框架.md 里 P1 的三个问题：
  1. 新版 instanceIds/query 的 statuses 过滤是否真的可用（RUNNING / COMPLETED）？
  2. 在途单详情拉取的真实耗时（抽样外推 A/B 片总耗时）？
  3. 上游限流迹象（响应头 ratelimit / errcode / 配额）？

安全约束（硬编码，不可关闭）：
  - 只调只读查询接口（gettoken / 新版 query / 老版 get），不写任何钉钉数据。
  - 不写归档库、不写 Part*.xlsx、不写 sync_cache.db（后者以 mode=ro 打开）。
  - 总 API 调用数硬上限 --max-calls（默认 40），分页上限 --max-pages（默认 2），
    详情抽样上限 --sample（默认 10）。绝不重放 5095 单全量扫描。

用法（需网络；本机 requests 在用户站点，见文末 PYTHONPATH）：
  python scripts/probe_incremental.py --dry            # 只打印计划，不发请求
  python scripts/probe_incremental.py --live --out r.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("[FATAL] 需要 requests；沙箱里请设 PYTHONPATH=C:/Users/dell/AppData/Roaming/Python/Python314/site-packages")
    raise

BASE_OAPI = "https://oapi.dingtalk.com"
BASE_NEW = "https://api.dingtalk.com"
QUERY_PATH = "/v1.0/workflow/processes/instanceIds/query"

DT_CFG = Path("D:/Desktop/work-main/dingtalk/config.json")
CACHE_DB = Path("D:/Desktop/work-main/dingtalk/sync_cache.db")


class BudgetExceeded(RuntimeError):
    pass


class ProbeClient:
    """精简只读客户端：只实现探针需要的三个只读调用，带调用预算与限流采样。"""

    def __init__(self, app_key: str, app_secret: str, interval: float,
                 max_calls: int, timeout: float, log=print):
        self.key = app_key
        self.secret = app_secret
        self.interval = interval
        self.max_calls = max_calls
        self.timeout = timeout
        self.log = log
        self.calls = 0
        self.total_api_sec = 0.0
        self.ratelimit_headers: dict[str, str] = {}
        self.qps_limit_hits: list[dict] = []   # 抓到的接口级 QPS 限流证据
        self.errors: list[dict] = []
        self._token = None
        self._s = requests.Session()

    def _budget(self):
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"调用预算耗尽 ({self.calls}/{self.max_calls})")

    def _record_headers(self, resp):
        for k, v in resp.headers.items():
            lk = k.lower()
            if any(t in lk for t in ("ratelimit", "rate-limit", "x-ratelimit", "retry-after")):
                self.ratelimit_headers[k] = v

    def token(self) -> str:
        if self._token:
            return self._token
        self._budget()
        t0 = time.time()
        r = self._s.get(
            f"{BASE_OAPI}/gettoken",
            params={"appkey": self.key, "appsecret": self.secret},
            timeout=self.timeout,
        )
        self.calls += 1
        self.total_api_sec += time.time() - t0
        j = r.json()
        if j.get("errcode") != 0:
            self.errors.append({"call": "gettoken", "resp": str(j)[:300]})
            raise RuntimeError(f"gettoken 失败: {j}")
        self._token = j["access_token"]
        self.log(f"[token] ok, expires_in={j.get('expires_in')}")
        return self._token

    def query_ids(self, process_code: str, start_ms: int, end_ms: int,
                  statuses: list[str] | None = None, max_results: int = 100,
                  max_pages: int = 2) -> dict:
        """新版 query；返回 {ids, pages, success, errs, sec, status_codes}。"""
        tok = self.token()
        ids: list[str] = []
        next_token = 0
        pages = 0
        errs: list[str] = []
        status_codes: list[int] = []
        t0 = time.time()
        while pages < max_pages:
            self._budget()
            body = {
                "processCode": process_code,
                "startTime": start_ms,
                "endTime": end_ms,
                "nextToken": next_token,
                "maxResults": max_results,
            }
            if statuses:
                body["statuses"] = statuses
            if self.interval:
                time.sleep(self.interval)
            try:
                r = self._s.post(
                    BASE_NEW + QUERY_PATH,
                    headers={"x-acs-dingtalk-access-token": tok,
                             "Content-Type": "application/json"},
                    json=body,
                    timeout=self.timeout,
                )
            except Exception as e:
                errs.append(f"net:{e}")
                break
            self.calls += 1
            status_codes.append(r.status_code)
            self._record_headers(r)
            try:
                j = r.json()
            except Exception:
                errs.append(f"nonjson:{r.status_code}:{r.text[:120]}")
                break
            if not j.get("success") or r.status_code != 200:
                msg = str(j.get("message") or "")
                errs.append(f"code={j.get('code')} msg={msg[:80]} http={r.status_code}")
                if "QpsLimit" in str(j.get("code")) or "qps" in msg or r.status_code == 429:
                    self.qps_limit_hits.append({
                        "code": j.get("code"),
                        "message": msg,
                        "http": r.status_code,
                    })
                break
            result = j.get("result") or {}
            batch = result.get("list") or []
            ids.extend(batch)
            pages += 1
            next_token = result.get("nextToken")
            if not batch or not next_token:
                break
        sec = time.time() - t0
        self.total_api_sec += sec
        return {"ids": ids, "pages": pages, "success": not errs,
                "errs": errs, "sec": round(sec, 3),
                "status_codes": status_codes}

    def get_instance(self, iid: str) -> dict:
        """老版 get；返回 {ok, status, sec}。"""
        tok = self.token()
        self._budget()
        if self.interval:
            time.sleep(self.interval)
        t0 = time.time()
        try:
            r = self._s.post(
                f"{BASE_OAPI}/topapi/processinstance/get",
                params={"access_token": tok},
                json={"process_instance_id": iid},
                timeout=self.timeout,
            )
        except Exception as e:
            self.total_api_sec += time.time() - t0
            return {"ok": False, "status": "", "sec": time.time() - t0, "err": str(e)}
        self.calls += 1
        self.total_api_sec += time.time() - t0
        self._record_headers(r)
        j = r.json()
        if j.get("errcode") != 0:
            self.errors.append({"call": "get", "errcode": j.get("errcode"),
                                "errmsg": j.get("errmsg")})
            return {"ok": False, "status": "", "sec": round(time.time() - t0, 3),
                    "err": f"{j.get('errcode')}:{j.get('errmsg')}"}
        inst = j.get("process_instance") or {}
        return {"ok": True, "status": inst.get("status", ""),
                "sec": round(time.time() - t0, 3)}


def load_config() -> dict:
    with open(DT_CFG, encoding="utf-8") as f:
        return json.load(f)


def local_status_distribution() -> dict:
    """只读 sync_cache.db，给出每个 (product, part) 的在途/完成分布。"""
    con = sqlite3.connect(f"file:{CACHE_DB.as_posix()}?mode=ro", uri=True)
    out: dict[str, dict] = {}
    try:
        rows = con.execute(
            "SELECT product, part, status, COUNT(*) FROM instances "
            "GROUP BY product, part, status"
        ).fetchall()
        for product, part, status, cnt in rows:
            d = out.setdefault(f"{product}|{part}", {"running": 0, "done": 0, "other": 0, "total": 0})
            d["total"] += cnt
            if status in ("RUNNING", "NEW"):
                d["running"] += cnt
            elif status == "COMPLETED":
                d["done"] += cnt
            else:
                d["other"] += cnt
    finally:
        con.close()
    return out


def pick_probe_parts(local: dict, cfg: dict) -> tuple[str, str, str]:
    """选 A 片高频模板做主探针，返回 (product, part, process_code)。"""
    preferred = "Part5_新品渠道仓发货"
    for product, meta in cfg["products"].items():
        parts = meta["parts"]
        if preferred in parts:
            return product, preferred, parts[preferred]
    # 兜底任意 Part5
    for product, meta in cfg["products"].items():
        for part, code in meta["parts"].items():
            if part.startswith("Part5"):
                return product, part, code
    raise RuntimeError("config 里找不到 Part5 模板")


def ms(d: dt.datetime) -> int:
    return int(d.replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp() * 1000)


def build_plan(cfg: dict) -> dict:
    now = dt.datetime.now()
    return {
        "window_days": cfg.get("recent_days", 45),
        "window_start": (now - dt.timedelta(days=cfg.get("recent_days", 45))).strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": now.strftime("%Y-%m-%d %H:%M:%S"),
        "max_calls_per_probe": 40,
    }


def run_live(args, cfg: dict, local: dict) -> dict:
    product, part, code = pick_probe_parts(local, cfg)
    ld = local.get(f"{product}|{part}", {})
    now = dt.datetime.now()
    start_ms = ms(now - dt.timedelta(days=args.window_days))
    end_ms = ms(now)
    client = ProbeClient(cfg["app_key"], cfg["app_secret"],
                         interval=args.interval, max_calls=args.max_calls,
                         timeout=args.timeout)

    report: dict = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "probe_part": f"{product}|{part}",
        "window_days": args.window_days,
        "local_distribution": ld,
        "status_filter": {},
        "detail_latency": {},
        "ratelimit_headers": {},
        "qps_limit_hits": [],
        "errors": [],
        "call_summary": {},
    }

    # --- Q1: 状态过滤是否生效：同一时间窗，三次查询对比 ---
    # 注意：max_pages 必须足够大翻到底，否则三者都被截断在同一页数会误判"相等"。
    # 实测这里的正确判据是「集合互斥 + 反查状态」，不是 id_count 大小。
    sets: dict[str, set[str]] = {}
    for label, statuses in (("no_filter", None),
                            ("running", ["RUNNING"]),
                            ("completed", ["COMPLETED"])):
        try:
            res = client.query_ids(code, start_ms, end_ms, statuses=statuses,
                                   max_results=args.page_size,
                                   max_pages=args.verify_pages)
            sets[label] = set(res["ids"])
            report["status_filter"][label] = {
                "statuses": statuses,
                "id_count": len(res["ids"]),
                "pages": res["pages"],
                "truncated": res["pages"] >= args.verify_pages,
                "sec": res["sec"],
                "success": res["success"],
                "errs": res["errs"],
                "sample_ids": res["ids"][:3],
            }
        except BudgetExceeded as e:
            report["status_filter"][label] = {"skipped": str(e)}
            break

    # 判据 1：RUNNING 与 COMPLETED 两个集合必须互斥；且都是 NO_FILTER 的子集
    verdict: dict = {}
    all_ok = all(report["status_filter"].get(k, {}).get("success")
                 for k in ("running", "completed", "no_filter"))
    verdict["all_queries_success"] = all_ok
    if all_ok and {"running", "completed", "no_filter"} <= sets.keys():
        sr, sc, sa = sets["running"], sets["completed"], sets["no_filter"]
        verdict["running∩completed"] = len(sr & sc)
        verdict["running⊆all"] = sr <= sa
        verdict["completed⊆all"] = sc <= sa
        verdict["exclusive_and_subset"] = (not (sr & sc) and sr <= sa and sc <= sa)
    else:
        verdict["subset_verdict_skipped"] = "有查询被限流/中断, 集合不完整, 不下子集结论"

    # 判据 2（最强）：反查 RUNNING 集合前几个 ID 的真实 status
    # statuses 过滤若只是"看起来不同"，反查会立刻露馅。
    reverse: list[dict] = []
    for iid in list(sets.get("running", []))[:args.verify_ids]:
        try:
            r = client.get_instance(iid)
            reverse.append({"id": iid[:16], "status": r.get("status"),
                            "ok": r.get("ok"),
                            "match": r.get("status") in ("RUNNING", "NEW")})
        except BudgetExceeded:
            break
    verdict["reverse_check"] = reverse
    verdict["reverse_all_match"] = bool(reverse) and all(x["match"] for x in reverse)
    report["status_filter"]["_verdict"] = verdict

    # --- Q2: 详情耗时抽样：用本地在途 ID，只拉 sample 个 ---
    con = sqlite3.connect(f"file:{CACHE_DB.as_posix()}?mode=ro", uri=True)
    try:
        running_ids = [r[0] for r in con.execute(
            "SELECT instance_id FROM instances WHERE product=? AND part=? "
            "AND status IN ('RUNNING','NEW') LIMIT ?", (product, part, args.sample))]
    finally:
        con.close()
    samples = []
    for iid in running_ids:
        try:
            samples.append(client.get_instance(iid))
        except BudgetExceeded as e:
            report["detail_latency"]["skipped"] = str(e)
            break
    ok_secs = [s["sec"] for s in samples if s.get("ok")]
    avg = round(sum(ok_secs) / len(ok_secs), 4) if ok_secs else None
    running_total = ld.get("running", 0)
    report["detail_latency"] = {
        "sample_n": len(samples),
        "ok_n": len(ok_secs),
        "avg_sec": avg,
        "min_sec": min(ok_secs) if ok_secs else None,
        "max_sec": max(ok_secs) if ok_secs else None,
        "local_running_total": running_total,
        "extrapolated_sec": round(avg * running_total, 1) if avg else None,
        "note": "外推仅按单实例均值×在途数，未含 listids 分页与导出开销",
    }

    report["ratelimit_headers"] = client.ratelimit_headers
    report["qps_limit_hits"] = client.qps_limit_hits
    report["errors"] = client.errors[:10]
    report["call_summary"] = {
        "total_calls": client.calls,
        "max_calls": args.max_calls,
        "total_api_sec": round(client.total_api_sec, 2),
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="P1 钉钉增量同步只读探针")
    ap.add_argument("--live", action="store_true", help="真正发请求；默认只 dry run")
    ap.add_argument("--dry", action="store_true", help="仅打印计划")
    ap.add_argument("--window-days", type=int, default=45)
    # 实测 2026-11-16: maxResults 合法值仅 1~20，>=50 报 InvalidmaxResults
    ap.add_argument("--page-size", type=int, default=20)
    ap.add_argument("--max-pages", type=int, default=2)
    ap.add_argument("--sample", type=int, default=10, help="详情耗时抽样单数上限")
    ap.add_argument("--verify-pages", type=int, default=20,
                    help="过滤验证翻页上限(必须翻到底, 否则误判)")
    ap.add_argument("--verify-ids", type=int, default=5, help="反查状态的 ID 数")
    ap.add_argument("--max-calls", type=int, default=120, help="总 API 调用硬上限")
    ap.add_argument("--interval", type=float, default=0.15)
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--out", default="", help="报告 JSON 输出路径（可选）")
    args = ap.parse_args()

    cfg = load_config()
    local = local_status_distribution()
    plan = build_plan(cfg)
    product, part, code = pick_probe_parts(local, cfg)

    print("=== P1 只读探针计划 ===")
    print(f"  探针模板: {product} / {part}")
    print(f"  时间窗: {plan['window_start']} ~ {plan['window_end']} ({args.window_days} 天)")
    print(f"  调用预算: {args.max_calls} 次；过滤验证翻页 {args.verify_pages} 页；反查 {args.verify_ids} 单；详情抽样 {args.sample} 单")
    print("  三项实测: statuses 过滤 / 详情耗时 / 限流头")
    print("  安全: 只调只读接口, 不写归档库/xlsx/缓存库(sync_cache mode=ro)")
    print(f"  本地该模板在途分布: {local.get(f'{product}|{part}')}")

    if not args.live or args.dry:
        print("\n[DRY RUN] 未发任何请求。加 --live 执行。")
        return 0

    print("\n=== 执行实测 ===")
    report = run_live(args, cfg, local)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print("\n=== 实测结果 ===")
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[报告已写] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
