#!/usr/bin/env python
"""一键预测脚本：为每只股票生成缠论趋势质量评估报告（1d/60m/30m/15m）

用法:
  uv run python scripts/predict.py                  # 从TDX自选股读取
  uv run python scripts/predict.py 600519.SH 000001.SZ  # 手动指定
  uv run python scripts/predict.py -n 4 600519.SH 000001.SZ  # 指定并发数

输出:
  自选股模式 → output/czsc_zxg_yyyymmdd.md
  手动模式   → output/czsc_<symbol>.md
"""

import argparse
import logging
import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

from czsc.connectors.tdx_connector import _normalize_symbol, get_raw_bars
from czsc.core import CZSC, Freq

FREQS = [
    ("1d", Freq.D),
    ("60m", Freq.F60),
    ("30m", Freq.F30),
    ("15m", Freq.F15),
]


def _setup_logging(log_file: str) -> logging.Logger:
    """配置日志：文件记录详细信息，屏幕只显示进度"""
    # 抑制 loguru 输出到屏幕
    try:
        from loguru import logger as loguru_logger
        loguru_logger.remove()
        loguru_logger.add(log_file, level="WARNING", encoding="utf-8")
    except ImportError:
        pass

    logger = logging.getLogger("predict")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    return logger


def _batch_stock_names(symbols: list[str], devnull) -> dict[str, str]:
    """批量获取股票名称，返回 {symbol: name} 字典"""
    from tdxdata.api import TdxData

    name_map = {}
    with redirect_stderr(devnull):
        tdx = TdxData()
        try:
            for symbol in symbols:
                code = _normalize_symbol(symbol)
                try:
                    name = tdx.get_stock_name(code)
                    name_map[symbol] = name or code
                except Exception:
                    name_map[symbol] = code
        finally:
            tdx.close()
    return name_map


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


def predict_stock(symbol, sdt, edt, fq="前复权", logger=None):
    """对单只股票生成多周期预测结果"""
    results = {}
    for label, freq in FREQS:
        try:
            bars = get_raw_bars(symbol, freq.value, sdt, edt, fq=fq, realtime=True)
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
            if logger:
                logger.error(f"{symbol} {label} 分析失败: {e}")
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


def format_md(symbol, results, sdt, edt, name=None, in_merged=False):
    """格式化单只股票为 Markdown 报告"""
    display = f"{name}（{symbol}）" if name else symbol
    lines = []

    # 合并报告模式下：添加锚点和回到概览跳转链接
    if in_merged:
        anchor = symbol.replace(".", "-")
        lines.append(f'<a id="stock-{anchor}"></a>')
        lines.append(f"# {display} 缠论趋势预测")
        lines.append("")
        lines.append(f"[↑ 回到概览](#综合概览)")
    else:
        lines.append(f"# {display} 缠论趋势预测")
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


TDX_ZXG_PATH = Path("/home/li/.local/share/tdxcfv/drive_c/tc/T0002/blocknew/zxg.blk")


def _parse_tdx_zxg(path):
    """解析通达信自选股文件，返回 symbol 列表（如 600519.SH）

    文件格式：每行一个7位编码，首字符为市场码（1=上海, 0=深圳），后6位为股票代码。
    """
    market_map = {"1": "SH", "0": "SZ"}
    symbols = []
    with open(path, encoding="gbk", errors="ignore") as f:
        for line in f:
            code = line.strip()
            if len(code) == 7 and code[0] in market_map:
                symbols.append(f"{code[1:]}.{market_map[code[0]]}")
    return symbols


def _merged_filename(symbols):
    """多个股票时合并为一个文件名"""
    parts = [s.replace(".", "_") for s in symbols]
    return f"output/czsc_{'_'.join(parts)}.md"


def _sort_key(symbol, all_results, name_map):
    """排序键：上证指数 > 创业板指 > 偏多 > 多空均衡 > 偏空 > 其他"""
    name = (name_map or {}).get(symbol, "")
    signal = _overall_signal(all_results[symbol])
    if "上证指数" in name:
        return 0
    if "创业板指" in name:
        return 1
    if "偏多" in signal:
        return 2
    if "多空均衡" in signal:
        return 3
    if "偏空" in signal:
        return 4
    return 5


def _write_merged_report(symbols, all_results, sdt, edt, filename, name_map=None):
    """生成多股票合并报告"""
    symbols = sorted(symbols, key=lambda s: _sort_key(s, all_results, name_map))
    lines = []
    lines.append(f"# 缠论趋势预测报告（{len(symbols)}只股票）")
    lines.append("")
    lines.append(f"> 数据范围: {sdt} ~ {edt} | 复权: 前复权")

    stock_labels = []
    for s in symbols:
        n = (name_map or {}).get(s)
        stock_labels.append(f"{n}（{s}）" if n else s)
    lines.append(f"> 股票: {', '.join(stock_labels)}")
    lines.append("")

    # 汇总表
    lines.append('<a id="综合概览"></a>')
    lines.append("## 综合概览")
    lines.append("| 股票 | 综合信号 |")
    lines.append("|------|----------|")
    for symbol in symbols:
        signal = _overall_signal(all_results[symbol])
        n = (name_map or {}).get(symbol)
        display = f"{n}（{symbol}）" if n else symbol
        anchor = symbol.replace(".", "-")
        link = f"[{display}](#stock-{anchor})"
        lines.append(f"| {link} | {signal} |")
    lines.append("")

    for symbol in symbols:
        lines.append("---")
        lines.append("")
        n = (name_map or {}).get(symbol)
        md = format_md(symbol, all_results[symbol], sdt, edt, name=n, in_merged=True)
        lines.append(md)
        lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="缠论趋势预测")
    parser.add_argument("symbols", nargs="*", help="股票代码，如 600519.SH 000001.SZ")
    parser.add_argument("-n", "--workers", type=int, default=4, help="并行线程数（默认 4）")
    args = parser.parse_args()

    # 解析股票列表：命令行参数优先，否则读取TDX自选股
    if args.symbols:
        symbols = args.symbols
        from_zxg = False
    elif TDX_ZXG_PATH.exists():
        symbols = _parse_tdx_zxg(TDX_ZXG_PATH)
        if not symbols:
            print(f"TDX自选股文件为空: {TDX_ZXG_PATH}")
            sys.exit(1)
        from_zxg = True
    else:
        print("用法: uv run python scripts/predict.py [股票代码1] [股票代码2] ...")
        print("  无参数时自动读取TDX自选股")
        print("  示例: uv run python scripts/predict.py 600519.SH 000001.SZ")
        print("  并发: uv run python scripts/predict.py -n 4 600519.SH 000001.SZ")
        sys.exit(1)

    edt = date.today().strftime("%Y-%m-%d")
    sdt = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    os.makedirs("output", exist_ok=True)
    log_file = f"output/predict_{edt.replace('-', '')}.log"
    logger = _setup_logging(log_file)

    print(f"数据范围: {sdt} ~ {edt}")
    print(f"待预测({len(symbols)}): {', '.join(symbols)}")
    print(f"并发数: {args.workers}")
    print("=" * 60)

    # 批量获取股票名称
    print("获取股票名称...")
    devnull = open(os.devnull, "w")
    name_map = _batch_stock_names(symbols, devnull)
    for s in symbols:
        logger.info(f"{s} → {name_map.get(s, s)}")

    # 并行分析
    all_results = {}
    total = len(symbols)

    if total == 1:
        symbol = symbols[0]
        print(f"\n[1/1] {name_map.get(symbol, symbol)} ...")
        all_results[symbol] = predict_stock(symbol, sdt, edt, logger=logger)
    else:
        workers = min(args.workers, total)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(predict_stock, symbol, sdt, edt, "前复权", logger): symbol
                for symbol in symbols
            }
            done_count = 0
            for future in as_completed(futures):
                symbol = futures[future]
                done_count += 1
                try:
                    all_results[symbol] = future.result()
                except Exception as e:
                    logger.error(f"{symbol} 分析异常: {e}")
                    all_results[symbol] = {label: {"error": str(e)} for label, _ in FREQS}
                display = name_map.get(symbol, symbol)
                print(f"  [{done_count}/{total}] {display} 完成")

    # 确定输出文件名
    if from_zxg:
        filename = f"output/czsc_zxg_{edt.replace('-', '')}.md"
    elif len(symbols) == 1:
        filename = f"output/czsc_{symbols[0].replace('.', '_')}.md"
    else:
        filename = _merged_filename(symbols)

    # 生成报告
    if len(symbols) == 1 and not from_zxg:
        md = format_md(symbols[0], all_results[symbols[0]], sdt, edt, name=name_map.get(symbols[0]))
        with open(filename, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        _write_merged_report(symbols, all_results, sdt, edt, filename, name_map=name_map)

    print(f"\n  → {filename}")
    print(f"  → {log_file}")
    print(f"完成，共生成 1 份报告 → output/")


if __name__ == "__main__":
    main()
