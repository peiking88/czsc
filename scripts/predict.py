#!/usr/bin/env python
"""一键预测脚本：为每只股票生成缠论趋势质量评估报告（1d/30m/5m）

用法:
  uv run python scripts/predict.py                  # 从TDX自选股读取
  uv run python scripts/predict.py 600519.SH 999999.SH  # 手动指定
  uv run python scripts/predict.py -n 4 600519.SH 999999.SH  # 指定并发数

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

from czsc.connectors.tdx_connector import _normalize_symbol, get_raw_bars, prefetch_factors
from czsc.core import CZSC, Freq

FREQS = [
    ("1d", Freq.D),
    ("30m", Freq.F30),
    ("5m", Freq.F5),
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

    # 抑制第三方库日志噪声（XDXR 解析失败等不影响结果的错误）
    for noisy in ("PYTDX2", "opentdx", "mootdx"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

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
    ubi_bar_count = 0
    if czsc_obj.bars_ubi:
        ubi_bars = czsc_obj.bars_ubi
        ubi_bar_count = len(ubi_bars)
        ubi_dir = "↓ 向下" if last_bi.direction.value == "向上" else "↑ 向上"
        first_close = ubi_bars[0].close
        last_close = ubi_bars[-1].close
        price_pct = (last_close - first_close) / first_close * 100 if first_close > 0 else 0
        ubi_info = (
            f"🔄 未完成笔 ({ubi_bar_count} 根K线)："
            f"方向 {ubi_dir} · "
            f"起始 {str(ubi_bars[0].dt)[:10]} · "
            f"力度={last_close - first_close:+.2f}"
        )

    ubi_momentum = ""
    if czsc_obj.bars_ubi and ubi_bar_count >= 3:
        closes = [b.close for b in ubi_bars]
        path_sum = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        net_move = abs(last_close - first_close)
        efficiency = net_move / path_sum if path_sum > 0 else 0

        seg = max(2, ubi_bar_count // 3)
        vol_head = sum(b.vol for b in ubi_bars[:seg]) / seg
        vol_tail = sum(b.vol for b in ubi_bars[-seg:]) / seg
        vol_ratio = vol_tail / vol_head if vol_head > 0 else 1.0

        abs_pct = abs(price_pct)
        hint_map = {
            "🟢 单边放量": "趋势强劲",
            "🟢 单边推进": "方向明确",
            "🟡 单边缩量": "警惕转折",
            "🟡 台阶放量": "量能积聚",
            "🟡 台阶推进": "推进有阻",
            "🟡 台阶缩量": "动能衰减",
            "🔴 反复拉锯": "方向不明",
            "🔴 窄幅横盘": "无趋势",
        }
        if abs_pct < 1:
            grade = "🔴 窄幅横盘"
        elif efficiency > 0.7:
            if vol_ratio >= 1.2:
                grade = "🟢 单边放量"
            elif vol_ratio >= 0.8:
                grade = "🟢 单边推进"
            else:
                grade = "🟡 单边缩量"
        elif efficiency > 0.4:
            if vol_ratio >= 1.2:
                grade = "🟡 台阶放量"
            elif vol_ratio >= 0.8:
                grade = "🟡 台阶推进"
            else:
                grade = "🟡 台阶缩量"
        else:
            grade = "🔴 反复拉锯"
        hint = hint_map.get(grade, "")
        ubi_momentum = f"{grade}，{hint}<br>，幅{price_pct:+.1f}% 效{efficiency:.2f} 量{vol_ratio:.1f}x"

    return {
        "rsq_msg": rsq_msg,
        "accel_msg": accel_msg,
        "power_msg": power_msg,
        "direction": direction,
        "ubi_info": ubi_info,
        "ubi_momentum": ubi_momentum,
        "ubi_bar_count": ubi_bar_count,
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


def _ubi_star(results):
    """未完成笔方向星级标注：全向上 → ★★，日线向下+分钟向上 → ★"""
    daily = results.get("1d")
    m30 = results.get("30m")
    m5 = results.get("5m")
    if not all([daily, m30, m5]):
        return ""
    if any("error" in r for r in [daily, m30, m5]):
        return ""
    # ubi方向 = 完成笔反方向。完成笔"上升" → ubi向下；完成笔"下降" → ubi向上
    daily_ubi_up = "下降" in daily["direction"]
    m30_ubi_up = "下降" in m30["direction"]
    m5_ubi_up = "下降" in m5["direction"]
    if daily_ubi_up and m30_ubi_up and m5_ubi_up:
        return '<span style="color:red">★★</span> '
    if not daily_ubi_up and m30_ubi_up and m5_ubi_up:
        return '<span style="color:red">★</span> '
    return ""


def format_md(symbol, results, name=None):
    """格式化单只股票为 Markdown 报告"""
    star = _ubi_star(results)
    label = f"{name}（{symbol}）" if name else symbol
    lines = []

    if star:
        lines.append(f"<h1>{star} {label} 缠论趋势预测</h1>")
    else:
        lines.append(f"# {label} 缠论趋势预测")
    lines.append("")
    lines.append("")
    labels = [l for l, _ in FREQS]
    rows = {"之前趋势": [], "趋势规整度": [], "加速度": [], "力度评估": [], "未完成笔": [], "未完成笔力度": []}
    for label, _ in FREQS:
        r = results.get(label)
        if r and "error" not in r:
            ubi = r["ubi_info"] if r["ubi_info"] else "无"
            ubi_mtm = r.get("ubi_momentum") or "-"
            dir_with_power = f"{r['direction']}<br>力度={r['last_bi'].power:.1f}"
            rows["未完成笔"].append(ubi)
            rows["未完成笔力度"].append(ubi_mtm)
            rows["之前趋势"].append(dir_with_power)
            rows["趋势规整度"].append(r["rsq_msg"])
            rows["加速度"].append(r["accel_msg"])
            rows["力度评估"].append(r["power_msg"])
        else:
            err = r.get("error", "数据获取失败") if r else "未知错误"
            rows["未完成笔"].append(f"⚠️ {err}")
            rows["未完成笔力度"].append("-")
            rows["之前趋势"].append("-")
            rows["趋势规整度"].append("-")
            rows["加速度"].append("-")
            rows["力度评估"].append("-")

    def _pad6(s):
        """将字符串填充到6个汉字等效宽度"""
        n = len(s)
        return s + "&nbsp;" * (6 - n) if n < 6 else s

    lines.append("<table>")
    lines.append("<tr><th>" + _pad6("指标") + "</th>" + "".join(f"<th>{l}</th>" for l in labels) + "</tr>")
    for indicator, values in rows.items():
        lines.append("<tr><td>" + _pad6(indicator) + "</td>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>")
    lines.append("</table>")
    lines.append("")

    return "\n".join(lines)


TDX_ZXG_PATH = Path("/home/li/.local/share/tdxcfv/drive_c/tc/T0002/blocknew/zxg.blk")


def _parse_tdx_zxg(path):
    """解析通达信自选股文件，返回 symbol 列表（如 600519.SH）

    文件格式：每行一个7位编码，首字符为市场码（1=上海, 0=深圳），后6位为股票代码。
    """
    market_map = {"1": "SH", "0": "SZ"}
    # TDX 内部编码 → 外部统一编码（自选股文件中上证指数用 000001，对外应为 999999）
    _tdx_code_remap = {"000001.SH": "999999.SH"}
    symbols = []
    with open(path, encoding="gbk", errors="ignore") as f:
        for line in f:
            code = line.strip()
            if len(code) == 7 and code[0] in market_map:
                s = f"{code[1:]}.{market_map[code[0]]}"
                symbols.append(_tdx_code_remap.get(s, s))
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


def _write_merged_report(symbols, all_results, filename, name_map=None):
    """生成多股票合并报告"""
    symbols = sorted(symbols, key=lambda s: _sort_key(s, all_results, name_map))
    lines = []
    lines.append(f"# 缠论趋势预测报告（{len(symbols)}只股票）")
    lines.append("")

    for symbol in symbols:
        lines.append("---")
        lines.append("")
        n = (name_map or {}).get(symbol)
        md = format_md(symbol, all_results[symbol], name=n)
        lines.append(md)
        lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="缠论趋势预测")
    parser.add_argument("symbols", nargs="*", help="股票代码，如 600519.SH 999999.SH")
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
        print("  示例: uv run python scripts/predict.py 600519.SH 999999.SH")
        print("  并发: uv run python scripts/predict.py -n 4 600519.SH 999999.SH")
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

    # 预取复权因子（缓存 1 天，后续 get_raw_bars 命中缓存跳过网络请求）
    print(f"预取复权因子 ({len(symbols)}只)...")
    prefetch_factors(symbols, dividend_type="前复权", max_workers=min(args.workers, len(symbols)))

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
        md = format_md(symbols[0], all_results[symbols[0]], name=name_map.get(symbols[0]))
        with open(filename, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        _write_merged_report(symbols, all_results, filename, name_map=name_map)

    print(f"\n  → {filename}")
    print(f"  → {log_file}")
    print(f"完成，共生成 1 份报告 → output/")


if __name__ == "__main__":
    main()
