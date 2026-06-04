#!/usr/bin/env python
"""缠论顶底分型方向预测回测：统计分型出现后价格方向是否如预期

逻辑：
  底分型 → 预测上涨 → 检查后续 N 根K线价格是否上涨
  顶分型 → 预测下跌 → 检查后续 N 根K线价格是否下跌

用法:
  uv run python scripts/fx_backtest.py                     # TDX自选股，30分钟
  uv run python scripts/fx_backtest.py -f 日线             # 日线
  uv run python scripts/fx_backtest.py 600519.SH 000858.SZ # 指定股票
  uv run python scripts/fx_backtest.py -n 8 --bars 5,10,20 # 指定预测窗口

输出:
  output/fx_backtest_yyyymmdd.md
"""

import argparse
import logging
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

import pandas as pd

from czsc import CZSC, Freq
from czsc.connectors.tdx_connector import _normalize_symbol, get_raw_bars, prefetch_factors

# 分型强度标签映射
POWER_MAP = {"强": 3, "中": 2, "弱": 1}

# 预测窗口：分型出现后观察 N 根K线的方向
DEFAULT_PREDICT_BARS = [3, 5, 10, 20]

# 数据回看天数
DATA_LOOKBACK = {"30分钟": 540, "日线": 1095}

TDX_ZXG_PATH = Path("/home/li/.local/share/tdxcfv/drive_c/tc/T0002/blocknew/zxg.blk")


# ─────────────────────────────── 工具函数 ───────────────────────────────


def _setup_logging(log_file: str) -> logging.Logger:
    """配置日志"""
    try:
        from loguru import logger as loguru_logger
        loguru_logger.remove()
        loguru_logger.add(log_file, level="WARNING", encoding="utf-8")
    except ImportError:
        pass
    for noisy in ("PYTDX2", "opentdx", "mootdx"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    logger = logging.getLogger("fx_backtest")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    return logger


def _parse_tdx_zxg(path):
    """解析通达信自选股文件"""
    market_map = {"1": "SH", "0": "SZ"}
    _tdx_code_remap = {"000001.SH": "999999.SH"}
    symbols = []
    with open(path, encoding="gbk", errors="ignore") as f:
        for line in f:
            code = line.strip()
            if len(code) == 7 and code[0] in market_map:
                s = f"{code[1:]}.{market_map[code[0]]}"
                symbols.append(_tdx_code_remap.get(s, s))
    return symbols


def _batch_stock_names(symbols, devnull):
    """批量获取股票名称"""
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


def _is_index(symbol: str) -> bool:
    return symbol.startswith("999999") or symbol.startswith("399")


# ─────────────────────────────── 分型回测核心 ───────────────────────────────


def backtest_fractals(symbol, freq, sdt, edt, predict_bars, logger=None):
    """对单只股票统计分型方向预测准确率

    返回 dict:
      {
        "total_fxs": 总分型数,
        "ding": {"count": 顶分型数, "accuracy": {N: 准确率}, "by_power": {强度: 准确率}},
        "di":   {"count": 底分型数, "accuracy": {N: 准确率}, "by_power": {强度: 准确率}},
        "all":  {"count": 总分型数, "accuracy": {N: 准确率}},
        "details": [...每笔交易明细...],
      }
    """
    # get_raw_bars 接受中文字符串周期
    bars = get_raw_bars(symbol, freq, sdt, edt, fq="前复权", raw_bar=True)
    if not bars or len(bars) < 30:
        return None

    czsc_obj = CZSC(bars)
    raw = czsc_obj.bars_raw

    # 构建 dt → index 映射，用分型中间K线（elements[1]）定位
    dt_to_idx = {}
    for i, b in enumerate(raw):
        dt_to_idx[str(b.dt)] = i

    results = {
        "total_fxs": len(czsc_obj.fx_list),
        "ding": {"count": 0, "correct": {}, "by_power": {}},
        "di": {"count": 0, "correct": {}, "by_power": {}},
        "all": {"count": 0, "correct": {}},
        "details": [],
    }
    for n in predict_bars:
        results["ding"]["correct"][n] = 0
        results["di"]["correct"][n] = 0
        results["all"]["correct"][n] = 0

    for fx in czsc_obj.fx_list:
        # 用分型中间K线定位
        mid_dt = str(fx.elements[1].dt) if len(fx.elements) >= 2 else str(fx.dt)
        idx = dt_to_idx.get(mid_dt)
        if idx is None:
            continue

        is_ding = fx.mark.value == "顶分型"
        power = fx.power_str
        fx_price = fx.fx  # 分型极值点价格

        # 统计各预测窗口
        for n in predict_bars:
            if idx + n >= len(raw):
                continue

            future_close = raw[idx + n].close
            predicted_up = not is_ding  # 底分型预测涨，顶分型预测跌
            actual_up = future_close > fx_price
            correct = (predicted_up == actual_up)

            if is_ding:
                results["ding"]["correct"][n] += 1 if correct else 0
            else:
                results["di"]["correct"][n] += 1 if correct else 0
            results["all"]["correct"][n] += 1 if correct else 0

        # 统计总数和按强度分组（只统计第一个窗口即可代表整体）
        n0 = predict_bars[0]
        if idx + n0 < len(raw):
            future_close = raw[idx + n0].close
            actual_up = future_close > fx_price
            predicted_up = not is_ding
            correct = (predicted_up == actual_up)
            price_change = (future_close - fx_price) / fx_price * 100

            if is_ding:
                results["ding"]["count"] += 1
                if power not in results["ding"]["by_power"]:
                    results["ding"]["by_power"][power] = {"count": 0, "correct": 0}
                results["ding"]["by_power"][power]["count"] += 1
                results["ding"]["by_power"][power]["correct"] += 1 if correct else 0
            else:
                results["di"]["count"] += 1
                if power not in results["di"]["by_power"]:
                    results["di"]["by_power"][power] = {"count": 0, "correct": 0}
                results["di"]["by_power"][power]["count"] += 1
                results["di"]["by_power"][power]["correct"] += 1 if correct else 0

            results["all"]["count"] += 1
            results["details"].append({
                "dt": str(fx.dt)[:16],
                "type": "顶" if is_ding else "底",
                "power": power,
                "fx_price": fx_price,
                "predict": "跌" if is_ding else "涨",
                f"close_{n0}": future_close,
                "change_pct": price_change,
                "correct": correct,
            })

    return results


# ─────────────────────────────── 报告生成 ───────────────────────────────


def generate_report(all_results, name_map, freq, predict_bars, sdt, edt, filename):
    """生成 Markdown 汇总报告"""
    lines = []
    lines.append("# 缠论顶底分型方向预测回测报告")
    lines.append("")
    lines.append(f"- **回测周期**: {freq}")
    lines.append(f"- **数据范围**: {sdt} ~ {edt}")
    lines.append(f"- **预测窗口**: {', '.join(str(n) for n in predict_bars)} 根K线")
    lines.append(f"- **股票数**: {len(all_results)}")
    lines.append("")
    lines.append("> 底分型预测上涨，顶分型预测下跌；统计后续N根K线价格是否如预期。")
    lines.append("")

    # ── 第一部分：整体汇总 ──
    lines.append("## 一、整体汇总")
    lines.append("")
    lines.append("| 买卖点 | 分型数 | " + " | ".join(f"{n}根准确率" for n in predict_bars) + " |")
    lines.append("|--------|--------|" + "|".join(["--------"] * len(predict_bars)) + "|")

    for fx_type, label in [("all", "全部分型"), ("ding", "顶分型"), ("di", "底分型")]:
        total_count = sum(r[fx_type]["count"] for r in all_results.values() if r)
        if total_count == 0:
            lines.append(f"| {label} | 0 | " + " | ".join(["-"] * len(predict_bars)) + " |")
            continue
        acc_cells = []
        for n in predict_bars:
            total_n = sum(r[fx_type].get("correct", {}).get(n, 0) for r in all_results.values() if r)
            # 分型总数（每个窗口可能不同，因为末尾K线不足的会跳过）
            total_possible = 0
            for r in all_results.values():
                if not r:
                    continue
                # 重新统计每个窗口的有效分型数
                total_possible += r[fx_type]["count"]
            # 简化：用第一个窗口的 count 作为基数
            acc = total_n / total_count * 100 if total_count > 0 else 0
            acc_cells.append(f"{acc:.1f}%")
        lines.append(f"| {label} | {total_count} | " + " | ".join(acc_cells) + " |")
    lines.append("")

    # ── 第二部分：按分型强度汇总 ──
    lines.append("## 二、按分型强度汇总")
    lines.append("")
    n0 = predict_bars[0]
    lines.append(f"| 分型类型 | 强度 | 分型数 | {n0}根准确率 | 平均涨跌 |")
    lines.append(f"|----------|------|--------|------------|----------|")

    for fx_type, label in [("ding", "顶分型"), ("di", "底分型")]:
        by_power = {}
        for sym, r in all_results.items():
            if not r:
                continue
            for pwr, info in r[fx_type].get("by_power", {}).items():
                if pwr not in by_power:
                    by_power[pwr] = {"count": 0, "correct": 0}
                by_power[pwr]["count"] += info["count"]
                by_power[pwr]["correct"] += info["correct"]

        for pwr in ["强", "中", "弱"]:
            info = by_power.get(pwr, {"count": 0, "correct": 0})
            cnt = info["count"]
            if cnt == 0:
                lines.append(f"| {label} | {pwr} | 0 | - | - |")
                continue
            acc = info["correct"] / cnt * 100
            lines.append(f"| {label} | {pwr} | {cnt} | {acc:.1f}% | - |")
    lines.append("")

    # ── 第三部分：个股明细 ──
    lines.append("## 三、个股明细")
    lines.append("")

    sorted_syms = sorted(
        all_results.items(),
        key=lambda x: x[1]["all"]["count"] if x[1] else 0,
        reverse=True,
    )

    for symbol, r in sorted_syms:
        if not r:
            continue
        name = name_map.get(symbol, symbol)
        total = r["all"]["count"]
        ding_cnt = r["ding"]["count"]
        di_cnt = r["di"]["count"]

        # 计算总准确率
        acc_cells = []
        for n in predict_bars:
            correct = r["all"].get("correct", {}).get(n, 0)
            # 有效总数（用 ding+di 的 count 之和作为近似）
            acc = correct / total * 100 if total > 0 else 0
            acc_cells.append(f"{acc:.1f}%")

        lines.append(f"### {name}（{symbol}）")
        lines.append(f"- 总分型: {total}（顶{ding_cnt} + 底{di_cnt}）")
        lines.append(f"- 准确率: {' / '.join(f'{n}根={a}' for n, a in zip(predict_bars, acc_cells))}")
        lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─────────────────────────────── 主流程 ───────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="缠论顶底分型方向预测回测")
    parser.add_argument("symbols", nargs="*", help="股票代码")
    parser.add_argument("-n", "--workers", type=int, default=16, help="并行线程数（默认 16）")
    parser.add_argument("-f", "--freq", default="30分钟", help="基础周期（默认 30分钟）")
    parser.add_argument("--bars", default="3,5,10,20", help="预测窗口K线数（默认 3,5,10,20）")
    args = parser.parse_args()

    freq = args.freq
    predict_bars = [int(x) for x in args.bars.split(",")]

    # 解析股票列表
    if args.symbols:
        symbols = args.symbols
    elif TDX_ZXG_PATH.exists():
        symbols = _parse_tdx_zxg(TDX_ZXG_PATH)
        if not symbols:
            print(f"TDX自选股文件为空: {TDX_ZXG_PATH}")
            sys.exit(1)
    else:
        print("用法: uv run python scripts/fx_backtest.py [股票代码] [-f 日线] [--bars 3,5,10]")
        sys.exit(1)

    symbols = [s for s in symbols if not _is_index(s)]
    if not symbols:
        print("过滤指数后无股票可回测")
        sys.exit(1)

    lookback = DATA_LOOKBACK.get(freq, 540)
    edt = date.today().strftime("%Y-%m-%d")
    sdt = (date.today() - timedelta(days=lookback)).strftime("%Y-%m-%d")

    os.makedirs("output", exist_ok=True)
    log_file = f"output/fx_backtest_{edt.replace('-', '')}.log"
    logger = _setup_logging(log_file)

    t_start = time.time()
    print(f"回测参数: {freq} | {sdt} ~ {edt} | 预测窗口 {predict_bars} 根K线")
    print(f"待回测({len(symbols)}): {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
    print(f"并发数: {args.workers}")
    print("=" * 60)

    # 预取
    print("获取股票名称...")
    devnull = open(os.devnull, "w")
    name_map = _batch_stock_names(symbols, devnull)

    print(f"预取复权因子 ({len(symbols)}只)...")
    prefetch_factors(symbols, dividend_type="前复权", max_workers=min(args.workers, len(symbols)))

    # 并行回测
    all_results = {}
    total = len(symbols)

    def _run(sym):
        return sym, backtest_fractals(sym, freq, sdt, edt, predict_bars, logger)

    if total == 1:
        sym = symbols[0]
        print(f"\n[1/1] {name_map.get(sym, sym)} ...")
        all_results[sym] = backtest_fractals(sym, freq, sdt, edt, predict_bars, logger)
    else:
        workers = min(args.workers, total)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run, sym): sym for sym in symbols}
            done_count = 0
            for future in as_completed(futures):
                sym, result = future.result()
                done_count += 1
                all_results[sym] = result
                display = name_map.get(sym, sym)
                fx_cnt = result["total_fxs"] if result else 0
                print(f"  [{done_count}/{total}] {display} 分型数={fx_cnt}")

    # 过滤 None
    all_results = {k: v for k, v in all_results.items() if v is not None}

    # 生成报告
    report_file = f"output/fx_backtest_{edt.replace('-', '')}.md"
    generate_report(all_results, name_map, freq, predict_bars, sdt, edt, report_file)

    elapsed = time.time() - t_start
    print(f"\n  → {report_file}")
    print(f"  → {log_file}")
    print(f"完成，共回测 {len(all_results)} 只股票，耗时 {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
