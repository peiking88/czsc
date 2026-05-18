#!/usr/bin/env python
"""一键预测脚本：为每只股票生成缠论趋势质量评估报告（1d/60m/30m/15m）

用法: uv run python scripts/predict.py 600519.SH 000001.SZ

输出: output/predict_<symbol>.md，每个文件包含 4 个周期的趋势质量评估。
"""

import os
import sys
from datetime import date, timedelta

from czsc.connectors.tdx_connector import get_raw_bars
from czsc.core import CZSC, Freq

FREQS = [
    ("1d", Freq.D),
    ("60m", Freq.F60),
    ("30m", Freq.F30),
    ("15m", Freq.F15),
]


def assess_trend(czsc_obj):
    """从 CZSC 对象提取趋势质量评估，返回 dict 或 None"""
    if not czsc_obj.bi_list:
        return None

    bi_list = czsc_obj.bi_list
    last_bi = bi_list[-1]

    cur_rsq = last_bi.rsq
    if cur_rsq > 0.8:
        rsq_msg = f"🟢 趋势规整 (R²={cur_rsq:.3f})，方向明确"
    elif cur_rsq > 0.6:
        rsq_msg = f"🟡 趋势一般 (R²={cur_rsq:.3f})，关注方向变化"
    else:
        rsq_msg = f"🔴 趋势散乱 (R²={cur_rsq:.3f})，方向不确定"

    accel = last_bi.acceleration
    if accel > 10:
        accel_msg = f"🟢 加速中 ({accel:.1f})，趋势强劲"
    elif accel > -10:
        accel_msg = f"🟡 匀速/减速 ({accel:.1f})，关注转折"
    else:
        accel_msg = f"🔴 反向加速 ({accel:.1f})，趋势可能反转"

    same_dir = [b for b in bi_list if b.direction == last_bi.direction]
    if len(same_dir) >= 3:
        powers = [b.power for b in same_dir[-3:]]
        if powers[-1] < powers[0] * 0.5:
            power_msg = "🔴 同向笔力度衰减 > 50%，动能衰竭"
        elif powers[-1] < powers[0] * 0.7:
            power_msg = "🟡 同向笔力度递减中，注意动能不足"
        else:
            power_msg = "🟢 力度稳定，趋势健康"
    else:
        power_msg = "⚪ 同向笔不足 3 根，无法评估力度变化"

    direction = "上升笔 📈" if last_bi.direction.value == "向上" else "下降笔 📉"

    ubi_info = ""
    if czsc_obj.bars_ubi:
        ubi_bars = czsc_obj.bars_ubi
        ubi_high = max(b.high for b in ubi_bars)
        ubi_low = min(b.low for b in ubi_bars)
        ubi_dir = "↓ 向下" if last_bi.direction.value == "向上" else "↑ 向上"
        ubi_info = (
            f"🔄 未完成笔 ({len(ubi_bars)} 根K线)："
            f"方向 {ubi_dir} | "
            f"起始 {str(ubi_bars[0].dt)[:10]} | "
            f"最高 {ubi_high:.2f} | 最低 {ubi_low:.2f}"
        )

    return {
        "rsq_msg": rsq_msg,
        "accel_msg": accel_msg,
        "power_msg": power_msg,
        "direction": direction,
        "ubi_info": ubi_info,
        "last_bi": last_bi,
        "bi_count": len(bi_list),
        "bar_count": len(czsc_obj.bars_raw),
    }


def predict_stock(symbol, sdt, edt, fq="前复权"):
    """对单只股票生成多周期预测结果"""
    results = {}
    for label, freq in FREQS:
        try:
            bars = get_raw_bars(symbol, freq.value, sdt, edt, fq=fq)
            if not bars:
                results[label] = {"error": "无数据"}
                continue
            czsc_obj = CZSC(bars)
            trend = assess_trend(czsc_obj)
            if trend is None:
                results[label] = {"error": "未检测到笔"}
            else:
                results[label] = trend
        except Exception as e:
            results[label] = {"error": str(e)}
    return results


def _overall_signal(results):
    """综合 4 个周期给出简单方向判断"""
    ups = 0
    downs = 0
    for label, _ in FREQS:
        r = results.get(label)
        if r and "error" not in r and r.get("direction"):
            if "上升" in r["direction"]:
                ups += 1
            else:
                downs += 1

    if ups + downs == 0:
        return "⚪ 数据不足，无法判断"
    if ups > downs:
        return f"🟢 偏多 ({ups}↑ {downs}↓)"
    elif downs > ups:
        return f"🔴 偏空 ({ups}↑ {downs}↓)"
    else:
        return f"🟡 多空均衡 ({ups}↑ {downs}↓)"


def format_md(symbol, results, sdt, edt):
    """格式化单只股票为 Markdown 报告"""
    lines = []
    lines.append(f"# {symbol} 缠论趋势预测")
    lines.append("")
    lines.append(f"> 数据范围: {sdt} ~ {edt} | 复权: 前复权")
    lines.append(f"> 综合信号: {_overall_signal(results)}")
    lines.append("")

    # ── 概览表 ──
    lines.append("## 多周期概览")
    lines.append("| 周期 | K线数 | 笔数 | 当前趋势 | 力度 | R² | 加速度 |")
    lines.append("|------|-------|------|----------|------|-----|--------|")
    for label, _ in FREQS:
        r = results.get(label)
        if r and "error" not in r:
            lines.append(
                f"| {label} | {r['bar_count']} | {r['bi_count']} | {r['direction']} "
                f"| {r['last_bi'].power:.1f} | {r['last_bi'].rsq:.3f} | {r['last_bi'].acceleration:.1f} |"
            )
        else:
            err = r.get("error", "N/A") if r else "N/A"
            lines.append(f"| {label} | - | - | - | - | - | {err} |")
    lines.append("")

    # ── 趋势质量评估 ──
    lines.append("## 趋势质量评估")
    lines.append("")
    for label, _ in FREQS:
        lines.append(f"### {label}")
        lines.append("")
        r = results.get(label)
        if r and "error" not in r:
            lines.append(f"- {r['rsq_msg']}")
            lines.append(f"- {r['accel_msg']}")
            lines.append(f"- {r['power_msg']}")
            lines.append(f"- 当前趋势：**{r['direction']}** (力度={r['last_bi'].power:.1f})")
            if r["ubi_info"]:
                lines.append(f"- {r['ubi_info']}")
        else:
            err = r.get("error", "数据获取失败") if r else "未知错误"
            lines.append(f"⚠️ {err}")
        lines.append("")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/predict.py <股票代码1> [股票代码2] ...")
        print("示例: uv run python scripts/predict.py 600519.SH 000001.SZ 600000.SH")
        sys.exit(1)

    symbols = sys.argv[1:]
    edt = date.today().strftime("%Y-%m-%d")
    sdt = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    print(f"数据范围: {sdt} ~ {edt}")
    print(f"待预测({len(symbols)}): {', '.join(symbols)}")
    print("=" * 60)

    os.makedirs("output", exist_ok=True)

    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] {symbol} ...")
        results = predict_stock(symbol, sdt, edt)
        md = format_md(symbol, results, sdt, edt)

        filename = f"output/czsc_{symbol.replace('.', '_')}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  → {filename}")

    print(f"\n完成，共生成 {len(symbols)} 份报告 → output/")


if __name__ == "__main__":
    main()
