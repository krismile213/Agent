"""在无窗口模式下启动 .bat / .py（供 Windows 计划任务使用）。

为什么需要这个文件
------------------
计划任务如果直接执行 .bat（或 console 版 python.exe），Task Scheduler 会
经由 cmd.exe / 控制台子系统启动；在 ``LogonType=Interactive`` 下那个窗口
**显示在桌面上**。

体感问题：``AgentPulseIncrement`` 每 15 分钟一轮、单轮约 165~180 秒，
桌面上几乎一直挂着一个黑窗口。

``Settings.Hidden`` 只管任务自身的窗口样式，**不会**隐藏派生的子进程，
所以改 Hidden 没用（实测仍会弹）。彻底的做法：

1. 任务的 Execute 换成 ``pythonw.exe`` —— GUI 子系统，**自身没有控制台**，
   从进程创建那刻起就没有窗口；
2. 由本脚本用 ``CREATE_NO_WINDOW`` 拉起真正的目标，让 cmd.exe / python.exe
   也不分配控制台。

用法（任务 Action）
------------------
    pythonw.exe "<repo>\\run_hidden.py" [选项] <目标> [目标参数...]

选项
----
    --exe <解释器>   目标为 .py 时用哪个解释器（默认 sys.executable，即 pythonw）
    --log <日志文件>  追加捕获目标的 stdout+stderr（默认丢弃）
    --cwd <目录>     工作目录（默认目标所在目录）

示例
----
    # 跑仓库根目录的 bat（相对路径按脚本所在目录解析）
    pythonw run_hidden.py "run_pulse.bat"

    # 跨仓库: 用 venv 解释器跑 ETL 脚本并把输出落到日志
    pythonw run_hidden.py --exe "C:\\venv\\Scripts\\python.exe" ^
        --log "D:\\proj\\logs\\etl.log" --cwd "D:\\proj" ^
        "backend\\etl\\auto_pipeline.py" --skip-mail

注意：pythonw 下 ``sys.stdout`` 是 ``None``，本脚本**不得 print 任何东西**
（否则直接抛异常导致任务失败），所以全程静默、只返回退出码。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Windows: 不给子进程分配控制台窗口
CREATE_NO_WINDOW = 0x08000000
HERE = Path(__file__).resolve().parent

_OPTS = ("--exe", "--log", "--cwd")


def _parse(argv):
    """手写解析：pythonw 下 argparse 报错会往 None 写 stderr 直接崩。"""
    opts = {"exe": None, "log": None, "cwd": None}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _OPTS and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
            continue
        rest.append(a)
        i += 1
    return opts, rest


def main(argv) -> int:
    opts, rest = _parse(argv)
    if not rest:
        return 2

    cwd = Path(opts["cwd"]) if opts["cwd"] else None
    target = Path(rest[0])
    if not target.is_absolute():
        target = (cwd or HERE) / target
    if not target.exists():
        return 3

    suffix = target.suffix.lower()
    if suffix in (".bat", ".cmd"):
        # .bat 必须交给 cmd.exe 解释；直接 CreateProcess 会失败
        cmd = ["cmd.exe", "/c", str(target), *rest[1:]]
    elif suffix == ".py":
        cmd = [opts["exe"] or sys.executable, str(target), *rest[1:]]
    else:
        cmd = [str(target), *rest[1:]]

    log_fh = None
    if opts["log"]:
        try:
            log_path = Path(opts["log"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "ab")
        except Exception:                  # noqa: BLE001 - 日志失败不影响主流程
            log_fh = None

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or target.parent),
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
        )
        return proc.returncode
    except Exception:                      # noqa: BLE001 - 静默降级，只回退出码
        return 1
    finally:
        if log_fh:
            try:
                log_fh.close()
            except Exception:              # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
