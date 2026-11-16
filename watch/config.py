"""配置解析：默认从 Agent 的 config.json 取 workmain_root / platform_root。

路径一律走 Path；环境变量可覆盖，便于测试和换机。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SOURCES = ("sqlite_rows", "export_runs", "part_files", "http_transport")
ALL_SOURCES = DEFAULT_SOURCES


@dataclass
class WatchConfig:
    workmain_root: Path
    db_path: Path
    platform_root: str
    interval: float
    sources: tuple[str, ...]

    @property
    def archive_db(self) -> Path:
        return self.workmain_root / "dingtalk" / "export_archive.db"


def _read_agent_config() -> dict:
    p = AGENT_ROOT / "config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_config(root: str | None = None, db: str | None = None,
                 platform: str | None = None, interval: float | None = None,
                 sources: tuple[str, ...] | None = None) -> WatchConfig:
    raw = _read_agent_config()
    env = os.environ

    workmain = Path(
        root or env.get("AGENT_WATCH_ROOT") or raw.get("workmain_root")
        or (AGENT_ROOT.parent / "work-main")
    )
    db_path = Path(
        db or env.get("AGENT_WATCH_DB") or (AGENT_ROOT / "watch" / "watch.db"))
    platform_root = (platform or env.get("AGENT_WATCH_PLATFORM")
                     or raw.get("platform_root") or "http://127.0.0.1:8000")
    interval_s = float(interval or env.get("AGENT_WATCH_INTERVAL") or 60)

    if sources:
        picked = tuple(sources)
    else:
        picked = tuple(
            s.strip() for s in env.get("AGENT_WATCH_SOURCES", "").split(",")
            if s.strip()) or DEFAULT_SOURCES

    unknown = [s for s in picked if s not in ALL_SOURCES]
    if unknown:
        raise ValueError(f"未知数据源: {unknown}; 可选: {list(ALL_SOURCES)}")

    return WatchConfig(workmain_root=workmain, db_path=db_path,
                       platform_root=platform_root, interval=interval_s,
                       sources=picked)


def build_sources(cfg: WatchConfig) -> list:
    """按配置实例化采集器（延迟导入，避免 CLI 只读 tail 时加载无关模块）。"""
    from .sources import (ExportRunsSource, HttpTransportSource,
                          PartFilesSource, SqliteRowsSource)

    out = []
    for name in cfg.sources:
        if name == "sqlite_rows":
            out.append(SqliteRowsSource(cfg.archive_db))
        elif name == "export_runs":
            out.append(ExportRunsSource(cfg.archive_db))
        elif name == "part_files":
            out.append(PartFilesSource(cfg.workmain_root))
        elif name == "http_transport":
            out.append(HttpTransportSource(cfg.platform_root))
    return out
