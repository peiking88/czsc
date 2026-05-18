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


def _comprehensive_interpretation(results):
    """对各周期缠论分析进行综合解读，返回 Markdown 文本"""
    valid = {}
    for label, _ in FREQS:
        r = results.get(label)
        if r and "error" not in r:
            valid[label] = r

    if not valid:
        return ""

    lines = []
    lines.append("## 综合解读")
    lines.append("")

    # ── 1. 大级别定方向 ──
    daily = valid.get("1d")
    intraday_labels = [l for l, _ in FREQS if l != "1d" and l in valid]

    if daily:
        daily_dir = "向上" if "上升" in daily["direction"] else "向下"
        daily_rsq = daily["last_bi"].rsq
        daily_accel = daily["last_bi"].acceleration
        daily_power = daily["last_bi"].power

        if daily_rsq > 0.7:
            trend_conf = "明确"
        elif daily_rsq > 0.5:
            trend_conf = "一般"
        else:
            trend_conf = "模糊"

        lines.append(f"**日线级别：**当前为 **{daily_dir}** 趋势（R²={daily_rsq:.3f}, 力度={daily_power:.1f}），"
                     f"趋势结构{trend_conf}。日线是主要方向基准，决定中长期持仓方向。")
        lines.append("")

        # ── 2. 多周期共振 ──
        if intraday_labels:
            same_count = 0
            opp_count = 0
            for label in intraday_labels:
                r = valid[label]
                if "上升" in r["direction"]:
                    if daily_dir == "向上":
                        same_count += 1
                    else:
                        opp_count += 1
                else:
                    if daily_dir == "向下":
                        same_count += 1
                    else:
                        opp_count += 1

            if same_count == len(intraday_labels):
                lines.append("**多周期共振：**日线与 60m/30m/15m 方向完全一致，大小级别共振，"
                             "趋势可靠性高。当前走势健康，可顺势操作。")
            elif opp_count == len(intraday_labels):
                lines.append("**⚠️ 多周期背离：**日线与所有小级别方向相反！这可能是趋势反转的前兆，"
                             "也可能只是短期回调。建议降低仓位，等待方向确认后再入场。")
            elif opp_count > same_count:
                lines.append(f"**多周期分歧：**日线{ daily_dir }，但 {opp_count} 个小级别反向（仅 {same_count} 个同向），"
                             "短周期与长周期存在明显分歧。建议观望或轻仓，等待共振信号出现。")
            else:
                lines.append(f"**多周期共振偏强：**日线{ daily_dir }，{same_count} 个小级别同向（{opp_count} 个反向），"
                             "多数周期方向一致，共振效果较好。")
            lines.append("")

    # ── 3. 力度与加速度分析 ──
    lines.append("### 力度与加速度分析")
    lines.append("")
    lines.append("| 周期 | 方向 | R² | 力度 | 加速度 | 评价 |")
    lines.append("|------|------|-----|------|--------|------|")
    for label, _ in FREQS:
        r = valid.get(label)
        if r:
            bi = r["last_bi"]
            _dir = "↑" if "上升" in r["direction"] else "↓"
            _rsq = bi.rsq
            _power = bi.power
            _accel = bi.acceleration

            if _rsq > 0.8:
                _eval = "趋势明确"
            elif _rsq > 0.6:
                _eval = "趋势尚可"
            else:
                _eval = "趋势散乱"

            if _accel > 10:
                _eval += "，加速中"
            elif _accel < -10:
                _eval += "，反向加速"
            elif _accel < 0:
                _eval += "，减速中"
            else:
                _eval += "，匀速"

            lines.append(f"| {label} | {_dir} | {_rsq:.3f} | {_power:.1f} | {_accel:.1f} | {_eval} |")
        else:
            lines.append(f"| {label} | - | - | - | - | 数据缺失 |")
    lines.append("")

    # ── 4. 关键观察与风险提示 ──
    lines.append("### 关键观察与风险提示")
    lines.append("")

    warnings = []

    # 检查力度衰减
    powers = {}
    for label, r in valid.items():
        powers[label] = r["last_bi"].power

    if "1d" in powers and "15m" in powers:
        if powers["15m"] < powers["1d"] * 0.3:
            warnings.append("- **动能衰竭信号：**15分钟级别力度仅为日线级别的 {:.0%}，小级别动能严重不足，"
                            "可能无法推动大级别趋势延续".format(powers["15m"] / max(powers["1d"], 0.01)))

    # 检查 R² 普遍偏低
    low_rsq = [l for l, r in valid.items() if r["last_bi"].rsq < 0.5]
    if len(low_rsq) >= 2:
        warnings.append(f"- **趋势散乱：**{', '.join(low_rsq)} 周期 R² < 0.5，笔的几何结构不够规整，"
                        f"当前处于震荡或方向不明确阶段，不宜追涨杀跌。")

    # 检查未完成笔
    ubi_labels = [l for l, r in valid.items() if r.get("ubi_info")]
    if ubi_labels:
        warnings.append(f"- **未完成笔：**{', '.join(ubi_labels)} 存在未完成笔，趋势可能正在转折中，"
                        f"需密切关注该级别的最新K线演变。")

    # 检查加速背离
    if daily and daily["last_bi"].acceleration < -10:
        warnings.append("- **⚠️ 日线反向加速：**日线级别出现反向加速，大趋势可能即将反转，"
                        "建议减仓或设置止损。")

    if not warnings:
        lines.append("- 各周期趋势结构健康，未发现显著风险信号。")
    else:
        for w in warnings:
            lines.append(w)

    lines.append("")
    return "\n".join(lines)


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

    # ── 综合解读 ──
    interp = _comprehensive_interpretation(results)
    if interp:
        lines.append(interp)

    return "\n".join(lines)


def _merged_filename(symbols):
    """多个股票时合并为一个文件名"""
    parts = [s.replace(".", "_") for s in symbols]
    return f"output/czsc_{'_'.join(parts)}.md"


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

    all_results = {}
    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] {symbol} ...")
        all_results[symbol] = predict_stock(symbol, sdt, edt)

    if len(symbols) == 1:
        symbol = symbols[0]
        md = format_md(symbol, all_results[symbol], sdt, edt)
        filename = f"output/czsc_{symbol.replace('.', '_')}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  → {filename}")
    else:
        lines = []
        lines.append(f"# 缠论趋势预测报告（{len(symbols)}只股票）")
        lines.append("")
        lines.append(f"> 数据范围: {sdt} ~ {edt} | 复权: 前复权")
        lines.append(f"> 股票: {', '.join(symbols)}")
        lines.append("")

        # 汇总表
        lines.append("## 综合概览")
        lines.append("| 股票 | 综合信号 |")
        lines.append("|------|----------|")
        for symbol in symbols:
            signal = _overall_signal(all_results[symbol])
            lines.append(f"| {symbol} | {signal} |")
        lines.append("")

        for symbol in symbols:
            lines.append("---")
            lines.append("")
            md = format_md(symbol, all_results[symbol], sdt, edt)
            lines.append(md)
            lines.append("")

        merged = "\n".join(lines)
        filename = _merged_filename(symbols)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(merged)
        print(f"\n  → {filename}")

    print(f"\n完成，共生成 1 份报告 → output/")


if __name__ == "__main__":
    main()
