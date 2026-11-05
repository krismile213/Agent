#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/test_part_stage.py — Part 环节名 / 素材通用 标注的离线单测(零 LLM)

背景(2026-11-05): 晨检 ⑥ 行动建议把「卡Part8」写成「完成Part8审批」。
Part8 是**渠道仓到货检查**(物流入库), 没有审批动作 —— 工具只给 Part 编号、
不给环节名时, 模型会自行脑补出一个动作, 然后催错对象。

同一轮业务确认了流程依赖: Part3(本地仓到货检查) 不通过 → Part6 单据建不出来;
素材通用的 SKU 可跳过 Part6 三段(寄送→拍摄交付→素材加工, 共 18 天),
由本地仓到货直接进上架 Listing + 广告。

覆盖:
  1. PART_STAGE 常量完整性(Part1~9 全覆盖, 环节名非空)
  2. _generic_material_map 判据(真实 Part6 数据: 通用 / 需拍摄 / 无记录)
  3. _material_tag 只在「未过 Part6」时标注(卡 Part8 不标, 避免噪声)
  4. pipeline_alerts 真实输出: 带环节名、带 Part 对照行与流程依赖说明
  5. _stage_of/_pending_flags: 双轨并行的「另挂」超目标段 + 分环节停留天数
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def test_part_stage_const():
    print("\n== PART_STAGE 常量完整性 ==")
    from plugins.workmain import PART_STAGE
    want = [f"Part{i}" for i in range(1, 10)]
    missing = [p for p in want if p not in PART_STAGE]
    check("Part1~Part9 全覆盖", not missing, f"缺 {missing}")
    empty = [p for p, v in PART_STAGE.items() if not str(v).strip()]
    check("每个 Part 都有环节名", not empty, f"空值 {empty}")
    # 关键语义: 这两个是到货/物流, 绝不是「审批」
    check("Part3 = 本地仓到货检查", "本地仓" in PART_STAGE["Part3"], PART_STAGE["Part3"])
    check("Part8 = 渠道仓到货检查", "渠道仓" in PART_STAGE["Part8"], PART_STAGE["Part8"])
    check("Part8 环节名不含「审批」", "审批" not in PART_STAGE["Part8"], PART_STAGE["Part8"])


def test_generic_material_map():
    print("\n== _generic_material_map 判据 ==")
    from plugins import workmain
    cfg_root = Path(workmain.DEFAULT_ROOT)
    d = cfg_root / "data" / "手机膜" / "手机膜"
    if not d.is_dir():
        print("  (跳过: 本地无手机膜同步目录)")
        return
    files = workmain._latest_parts(d)
    if not files:
        print("  (跳过: 无 Part 文件)")
        return
    gmat = workmain._generic_material_map(files)
    check("解析出 SKU 数 > 0", len(gmat) > 0, str(len(gmat)))
    # G593: 是否决定拍摄=否 + 素材参考 G511 → 通用
    if "G593" in gmat:
        ok, basis = gmat["G593"]
        check("G593 判为素材通用", ok, basis)
        check("G593 依据含参考 SKU", "G511" in basis, basis)
    # G587: 是否决定拍摄=是 → 不通用
    if "G587" in gmat:
        ok, basis = gmat["G587"]
        check("G587 判为需拍摄", not ok, basis)
    # 真实数据里通用/不通用都要有, 否则说明判据塌成一边
    generic_n = sum(1 for v in gmat.values() if v[0])
    check("通用与非通用都存在(判据没塌成一边)",
          0 < generic_n < len(gmat), f"通用 {generic_n}/{len(gmat)}")


def test_material_tag():
    print("\n== _material_tag 只在未过 Part6 时标注 ==")
    from plugins.workmain import _material_tag
    gmat = {"G900": (True, "是否决定拍摄=否"),
            "G901": (False, "是否决定拍摄=是(需真机拍摄)")}
    t_generic = _material_tag(gmat, "G900", "Part3")
    check("Part3 通用 → 提示可跳过拍摄", "素材通用" in t_generic, t_generic)
    check("Part3 通用 → 带节省天数", "18" in t_generic, t_generic)
    t_need = _material_tag(gmat, "G901", "Part3")
    check("Part3 不通用 → 提示需拍摄", "需拍摄" in t_need, t_need)
    for part in ("Part7", "Part8", "Part9", ""):
        check(f"卡{part or '空'} → 不标注(已过素材段)",
              _material_tag(gmat, "G900", part) == "",
              _material_tag(gmat, "G900", part))
    check("查不到的 SKU → 不标注(不臆断)",
          _material_tag(gmat, "G999", "Part3") == "")


def test_pending_flags_and_stage_of():
    """2026-11-05 用户纠正: Part3 本地仓到货检查也有时效、也要催。
    双轨并行 SKU(G599-601 同挂 Part3+5+8) 只报 max(pending)=Part8 会把卡了
    17~21 天的 Part3 掩盖; 且「已持续N天」要按当前环节自身的更新时间算。"""
    print("\n== _stage_of/_pending_flags: 多环节并行与分环节停留 ==")
    try:
        import pandas as pd
    except ImportError:
        print("  (跳过: 无 pandas)")
        return
    from plugins.workmain import _stage_of, _pending_flags, STALE_CAP_DAYS
    targets = {"Part3": 8, "Part5": 22, "Part8": 15}
    today = pd.Timestamp("2026-11-05")
    # 复刻 G599 真实形态: Part3/Part5 更新于 10-19, Part8 更新于 11-04
    info = {"pending": {"3", "5", "8"},
            "part_up": {"3": pd.Timestamp("2026-10-19 16:11"),
                        "5": pd.Timestamp("2026-10-19 16:11"),
                        "8": pd.Timestamp("2026-11-04 19:07")},
            "last_up": pd.Timestamp("2026-11-04 19:07")}
    st = _stage_of(info, targets, {}, "G599", today)
    check("当前环节仍是 max(pending)=Part8",
          st.startswith("现Part8(渠道仓到货检查)"), st)
    check("Part8 停留按自身更新(1天)", "已持续1天/时效15天" in st, st)
    check("超目标的 Part3 被标出(另挂17天/时效8天)",
          "另挂Part3(本地仓到货检查)已停17天/时效8天" in st, st)
    check("未超目标的 Part5(17天/22天) 不标", "另挂Part5" not in st, st)
    # 停留天数用当前环节自身更新, 不吃别的环节的 last_up(旧 bug: G602 显示 9 天,
    # 其实 9 天前更新的是它的 Part9 单, Part5 本身已停 10 天)
    info2 = {"pending": {"5"},
             "part_up": {"5": pd.Timestamp("2026-10-26 17:22")},
             "last_up": pd.Timestamp("2026-10-27 16:58")}
    st2 = _stage_of(info2, targets, {}, "G602", today)
    check("停留天数按该环节自身更新(10天而非9天)",
          "已持续10天/时效22天" in st2, st2)
    # 单环节在途: 行为与旧版一致, 无另挂
    info3 = {"pending": {"8"},
             "part_up": {"8": pd.Timestamp("2026-11-04 19:07")},
             "last_up": pd.Timestamp("2026-11-04 19:07")}
    st3 = _stage_of(info3, targets, {}, "G587", today)
    check("单环节在途 → 无另挂", "另挂" not in st3, st3)
    # 海运 Part8 目标 35 天: 16 天不超, 但 Part3 照标
    info4 = {"pending": {"3", "8"},
             "part_up": {"3": pd.Timestamp("2026-10-19 16:11"),
                         "8": pd.Timestamp("2026-10-20 10:00")},
             "last_up": pd.Timestamp("2026-10-20 10:00")}
    st4 = _stage_of(info4, targets, {"G900": "sea"}, "G900", today)
    check("海运 Part8 时效 35 天", "时效35天" in st4, st4)
    check("海运下 Part8 自身不另挂(16<35)",
          "另挂Part8" not in st4 and "另挂Part3" in st4, st4)
    # 120 天封顶: 更久的不进另挂(历史遗留)
    info5 = {"pending": {"3", "8"},
             "part_up": {"3": today - pd.Timedelta(days=200),
                         "8": pd.Timestamp("2026-11-04 19:07")},
             "last_up": pd.Timestamp("2026-11-04 19:07")}
    st5 = _stage_of(info5, targets, {}, "G901", today)
    check(f"超{STALE_CAP_DAYS}天封顶的段不进另挂", "另挂Part3" not in st5, st5)
    # exclude 参数直接生效
    check("_pending_flags exclude 排除当前环节",
          _pending_flags(info, targets, {}, "G599", today, exclude="8")
          == "⚠️另挂Part3(本地仓到货检查)已停17天/时效8天")


def test_pipeline_output_real():
    print("\n== pipeline_alerts 真实输出 ==")
    try:
        import agentcore as core
        cfg = core.load_config()
        core.set_root(cfg.get("workmain_root"))
        reg = core.build_registry()
        core.load_plugins(reg, cfg)
        out = reg.execute("pipeline_alerts", {})
    except Exception as e:      # noqa: BLE001
        print(f"  (跳过: 工具不可用 {e})")
        return
    if out.startswith("[错误]"):
        print(f"  (跳过: {out[:60]})")
        return
    check("输出含 Part→环节 对照行", "Part编号=环节" in out)
    check("对照行说明 Part8 是到货", "Part8渠道仓到货检查" in out)
    check("输出含流程依赖说明(Part3 拖 Part6)", "拖死素材段" in out)
    check("输出说明两个到货检查的区别(本地仓/渠道仓)",
          "Part3=本地仓到货检查" in out and "Part8=渠道仓到货检查" in out)
    # 双轨并行: G599-601 同挂 Part3+5+8, Part3 超目标必须以「另挂」浮出
    check("签样追踪出现「另挂Part3」(双轨并行不被掩盖)",
          "另挂Part3(本地仓到货检查)" in out)
    # 停滞段: 按环节去重汇总行, Part3 超目标单数可见(TOP8 被 Part8 老单占满也看得到)
    seg_line = next((l for l in out.split("\n") if "[按环节去重]" in l), "")
    check("停滞段带按环节汇总行", bool(seg_line), "未找到 [按环节去重] 行")
    check("汇总行含 Part3 计数", "Part3" in seg_line, seg_line)
    check("条目带环节名「卡Part8(渠道仓到货检查)」",
          "卡Part8(渠道仓到货检查)" in out)
    # 回归: 不能再出现裸的「卡Part8)」——那是模型脑补「审批」的入口
    check("不再出现裸编号「卡Part8)」", "卡Part8)" not in out)
    # 素材标记不能污染已过 Part6 的条目
    check("卡 Part8 的条目不标素材通用",
          "卡Part8(渠道仓到货检查)) [素材通用" not in out)


def test_scan_sync_data_labels():
    print("\n== scan_sync_data 同样带环节名 ==")
    try:
        import agentcore as core
        cfg = core.load_config()
        core.set_root(cfg.get("workmain_root"))
        reg = core.build_registry()
        core.load_plugins(reg, cfg)
        out = reg.execute("scan_sync_data", {"line": "手机膜", "top": 3})
    except Exception as e:      # noqa: BLE001
        print(f"  (跳过: 工具不可用 {e})")
        return
    if out.startswith("[错误]"):
        print(f"  (跳过: {out[:60]})")
        return
    check("停滞TOP 带环节名", "Part8(渠道仓到货检查) 停滞" in out)
    check("停滞TOP 无裸编号", " Part8 停滞" not in out)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test_part_stage_const()
    test_pending_flags_and_stage_of()
    test_generic_material_map()
    test_material_tag()
    test_pipeline_output_real()
    test_scan_sync_data_labels()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[Part环节名/素材通用单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
