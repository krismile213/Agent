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
    test_generic_material_map()
    test_material_tag()
    test_pipeline_output_real()
    test_scan_sync_data_labels()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'=' * 46}\n[Part环节名/素材通用单测] {n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
