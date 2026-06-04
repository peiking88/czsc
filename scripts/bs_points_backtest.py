#!/usr/bin/env python
"""缠论买卖点回测脚本：评估一买/二买/三买/一卖/二卖/三卖的准确率和收益率

用法:
  uv run python scripts/bs_points_backtest.py                     # TDX自选股，30分钟
  uv run python scripts/bs_points_backtest.py -f 日线             # 日线
  uv run python scripts/bs_points_backtest.py 600519.SH 000858.SZ # 指定股票
  uv run python scripts/bs_points_backtest.py -n 8                # 8线程

输出:
  output/bs_backtest_yyyymmdd.md      — 汇总报告
  output/bs_backtest_{买卖点类型}.html  — wbt HTML 报告
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
from wbt import WeightBacktest, generate_backtest_report

from czsc import CzscStrategyBase, Event, Position
from czsc.connectors.tdx_connector import _normalize_symbol, get_raw_bars, prefetch_factors

# ── 6 种缠论买卖点信号配置 ──
# 格式：{名称: {open: 开仓信号体, exit: 平仓信号体, not: 禁止信号体, dir: long/short}}
BS_CONFIGS = {
    "一买开多": {
        "open": "D1B_BUY1V221126_一买_任意_任意_0",
        "exit": "D1_表里关系V230101_向下_任意_任意_0",
        "not": "D1_涨跌停V230331_涨停_任意_任意_0",
        "dir": "long",
    },
    "二买开多": {
        "open": "D1W9T2_第二买卖点V240524_二买_任意_任意_0",
        "exit": "D1_表里关系V230101_向下_任意_任意_0",
        "not": "D1_涨跌停V230331_涨停_任意_任意_0",
        "dir": "long",
    },
    "三买开多": {
        "open": "D1_三买辅助V230228_三买_任意_任意_0",
        "exit": "D1_表里关系V230101_向下_任意_任意_0",
        "not": "D1_涨跌停V230331_涨停_任意_任意_0",
        "dir": "long",
    },
    "一卖开空": {
        "open": "D1B_SELL1V221126_一卖_任意_任意_0",
        "exit": "D1_表里关系V230101_向上_任意_任意_0",
        "not": "D1_涨跌停V230331_跌停_任意_任意_0",
        "dir": "short",
    },
    "二卖开空": {
        "open": "D1W9T2_第二买卖点V240524_二卖_任意_任意_0",
        "exit": "D1_表里关系V230101_向上_任意_任意_0",
        "not": "D1_涨跌停V230331_跌停_任意_任意_0",
        "dir": "short",
    },
    "三卖开空": {
        "open": "D1#SMA#34_BS3辅助V230318_三卖_任意_任意_0",
        "exit": "D1_表里关系V230101_向上_任意_任意_0",
        "not": "D1_涨跌停V230331_跌停_任意_任意_0",
        "dir": "short",
    },
}

# ── Position 参数 ──
# 不同周期的持仓控制参数
POS_PARAMS = {
    "30分钟": {"interval": 3600 * 4, "timeout": 100, "stop_loss": 500},
    "日线":   {"interval": 3600 * 24 * 3, "timeout": 30, "stop_loss": 500},
}

# ── 数据量参数 ──
DATA_LOOKBACK = {"30分钟": 540, "日线": 1095}  # 天数
WARMUP_MONTHS = 6

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
    logger = logging.getLogger("bs_backtest")
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
    """判断是否为指数（不参与回测）"""
    return symbol.startswith("999999") or symbol.startswith("399")


# ─────────────────────────────── 策略构建 ───────────────────────────────


def build_bs_position(symbol: str, freq: str, bs_name: str, cfg: dict) -> Position:
    """根据买卖点配置构建 Position"""
    is_long = cfg["dir"] == "long"
    open_sig = f"{freq}_{cfg['open']}"
    exit_sig = f"{freq}_{cfg['exit']}"
    not_sig = f"{freq}_{cfg['not']}"
    params = POS_PARAMS.get(freq, POS_PARAMS["30分钟"])

    open_event = Event.load({
        "name": f"{bs_name}_开仓",
        "operate": "开多" if is_long else "开空",
        "signals_all": [open_sig],
        "signals_not": [not_sig],
    })
    exit_event = Event.load({
        "name": f"{bs_name}_平仓",
        "operate": "平多" if is_long else "平空",
        "signals_all": [exit_sig],
    })
    return Position(
        name=bs_name,
        symbol=symbol,
        opens=[open_event],
        exits=[exit_event],
        interval=params["interval"],
        timeout=params["timeout"],
        stop_loss=params["stop_loss"],
    )


class BsStrategy(CzscStrategyBase):
    """通用缠论买卖点策略"""

    def __init__(self, symbol, freq, bs_name, cfg):
        self._freq = freq
        self._bs_name = bs_name
        self._cfg = cfg
        super().__init__(symbol=symbol)

    @property
    def positions(self) -> list[Position]:
        return [build_bs_position(self.symbol, self._freq, self._bs_name, self._cfg)]


# ─────────────────────────────── 回测执行 ───────────────────────────────


def compute_trade_stats(pairs: pd.DataFrame) -> dict:
    """从 pairs_df 计算交易统计

    盈亏比例字段单位为 BP（基点），1BP = 0.01%，报告输出转为百分比。
    """
    if pairs.empty:
        return {"交易次数": 0}
    n = len(pairs)
    # 盈亏比例列（中文字段，来自 Rust TradePairs，单位 BP）
    col = "盈亏比例" if "盈亏比例" in pairs.columns else pairs.columns[-1]
    rets_bp = pairs[col].astype(float)  # BP 单位
    rets_pct = rets_bp / 100           # 转为百分比
    wins = rets_pct[rets_pct > 0]
    losses = rets_pct[rets_pct < 0]
    win_rate = len(wins) / n * 100 if n > 0 else 0
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 0.0001
    profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    stats = {
        "交易次数": n,
        "胜率": win_rate,
        "盈亏比": profit_ratio,
        "平均收益": rets_pct.mean(),
        "中位数收益": rets_pct.median(),
        "最大单笔盈利": rets_pct.max(),
        "最大单笔亏损": rets_pct.min(),
    }
    if "持仓天数" in pairs.columns:
        stats["平均持仓天数"] = pairs["持仓天数"].mean()
    return stats


def holds_to_weight_df(holds: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """holds_df 转为 wbt 权重表格式"""
    if holds.empty:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])
    df = holds[["dt", "pos", "price"]].copy()
    df["symbol"] = symbol
    df = df.rename(columns={"pos": "weight"})
    if df.duplicated(subset=["dt", "symbol"]).any():
        df = df.groupby(["dt", "symbol"], as_index=False).agg(
            weight=("weight", "mean"),
            price=("price", "first"),
        )
    return df[["dt", "symbol", "weight", "price"]]


def backtest_one_symbol(symbol, freq, sdt, edt, bt_sdt, fee_rate, logger=None):
    """对单只股票执行 6 种买卖点回测"""
    bars = get_raw_bars(symbol, freq, sdt, edt, fq="前复权", raw_bar=True)
    if not bars:
        return None

    results = {}
    for bs_name, cfg in BS_CONFIGS.items():
        try:
            strategy = BsStrategy(symbol=symbol, freq=freq, bs_name=bs_name, cfg=cfg)
            res = strategy.backtest(bars, sdt=bt_sdt)
            pairs = res.pairs_df()
            holds = res.holds_df()
            stats = compute_trade_stats(pairs)
            wdf = holds_to_weight_df(holds, symbol)
            results[bs_name] = {"stats": stats, "pairs": pairs, "weight_df": wdf}
            del res  # 释放内存
        except Exception as e:
            if logger:
                logger.error(f"{symbol} {bs_name} 回测失败: {e}")
            results[bs_name] = {"stats": {"交易次数": 0, "error": str(e)}, "pairs": pd.DataFrame(), "weight_df": pd.DataFrame()}
    return results


# ─────────────────────────────── 报告生成 ───────────────────────────────


def generate_markdown_report(all_results, name_map, freq, sdt, edt, fee_rate, filename):
    """生成 Markdown 汇总报告"""
    lines = []
    lines.append(f"# 缠论买卖点回测报告")
    lines.append("")
    lines.append(f"- **回测周期**: {freq}")
    lines.append(f"- **数据范围**: {sdt} ~ {edt}")
    lines.append(f"- **手续费**: {fee_rate * 10000:.0f} BP")
    lines.append(f"- **股票数**: {len(all_results)}")
    lines.append("")

    # ── 第一部分：各买卖点类型汇总 ──
    lines.append("## 一、各买卖点类型汇总")
    lines.append("")
    lines.append("| 买卖点 | 交易次数 | 胜率 | 盈亏比 | 平均收益 | 最大盈利 | 最大亏损 |")
    lines.append("|--------|----------|------|--------|----------|----------|----------|")

    for bs_name in BS_CONFIGS:
        all_stats = []
        for sym, res in all_results.items():
            if res and bs_name in res:
                st = res[bs_name]["stats"]
                if st.get("交易次数", 0) > 0:
                    all_stats.append(st)

        if not all_stats:
            lines.append(f"| {bs_name} | 0 | - | - | - | - | - |")
            continue

        total_trades = sum(s["交易次数"] for s in all_stats)
        # 加权平均胜率
        weighted_wins = sum(s["胜率"] * s["交易次数"] for s in all_stats)
        avg_win_rate = weighted_wins / total_trades if total_trades > 0 else 0
        avg_profit_ratio = sum(s["盈亏比"] for s in all_stats) / len(all_stats)
        avg_return = sum(s["平均收益"] for s in all_stats) / len(all_stats)
        max_win = max(s["最大单笔盈利"] for s in all_stats)
        max_loss = min(s["最大单笔亏损"] for s in all_stats)

        lines.append(
            f"| {bs_name} | {total_trades} | {avg_win_rate:.1f}% | {avg_profit_ratio:.2f} "
            f"| {avg_return:+.2f}% | {max_win:+.2f}% | {max_loss:+.2f}% |"
        )
    lines.append("")

    # ── 第二部分：个股明细 ──
    lines.append("## 二、个股明细")
    lines.append("")

    # 按总交易次数排序
    sorted_symbols = sorted(
        all_results.items(),
        key=lambda x: sum(
            x[1][bs]["stats"].get("交易次数", 0) if x[1] and bs in x[1] else 0
            for bs in BS_CONFIGS
        ),
        reverse=True,
    )

    for symbol, res in sorted_symbols:
        name = name_map.get(symbol, symbol)
        lines.append(f"### {name}（{symbol}）")
        lines.append("")
        lines.append("| 买卖点 | 交易次数 | 胜率 | 盈亏比 | 平均收益 | 最大盈利 | 最大亏损 |")
        lines.append("|--------|----------|------|--------|----------|----------|----------|")

        for bs_name in BS_CONFIGS:
            if not res or bs_name not in res:
                continue
            st = res[bs_name]["stats"]
            n = st.get("交易次数", 0)
            if n == 0:
                lines.append(f"| {bs_name} | 0 | - | - | - | - | - |")
                continue
            lines.append(
                f"| {bs_name} | {n} | {st['胜率']:.1f}% | {st['盈亏比']:.2f} "
                f"| {st['平均收益']:+.2f}% | {st['最大单笔盈利']:+.2f}% "
                f"| {st['最大单笔亏损']:+.2f}% |"
            )
        lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def generate_html_reports(all_results, freq, fee_rate, output_dir):
    """为每种买卖点类型生成 wbt HTML 报告"""
    for bs_name in BS_CONFIGS:
        weight_dfs = []
        for sym, res in all_results.items():
            if res and bs_name in res:
                wdf = res[bs_name].get("weight_df")
                if wdf is not None and not wdf.empty:
                    weight_dfs.append(wdf)

        if not weight_dfs:
            continue

        dfw = pd.concat(weight_dfs, ignore_index=True)
        html_path = output_dir / f"bs_backtest_{bs_name}.html"
        try:
            wb = WeightBacktest(data=dfw, fee_rate=fee_rate, weight_type="ts", yearly_days=252)
            generate_backtest_report(
                df=dfw,
                output_path=str(html_path),
                title=f"缠论{bs_name}回测报告",
                fee_rate=fee_rate,
                weight_type="ts",
                yearly_days=252,
            )
            print(f"  → {html_path} (胜率={wb.stats.get('交易胜率', '-')})")
        except Exception as e:
            print(f"  → {bs_name} HTML 报告生成失败: {e}")


# ─────────────────────────────── 主流程 ───────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="缠论买卖点回测")
    parser.add_argument("symbols", nargs="*", help="股票代码，如 600519.SH")
    parser.add_argument("-n", "--workers", type=int, default=16, help="并行线程数（默认 16）")
    parser.add_argument("-f", "--freq", default="30分钟", help="基础周期（默认 30分钟）")
    parser.add_argument("--fee", type=float, default=0.0002, help="手续费（默认 4BP）")
    parser.add_argument("--stop-loss", type=int, default=500, help="止损 BP（默认 500=5%%）")
    args = parser.parse_args()

    freq = args.freq
    fee_rate = args.fee

    # 解析股票列表
    if args.symbols:
        symbols = args.symbols
    elif TDX_ZXG_PATH.exists():
        symbols = _parse_tdx_zxg(TDX_ZXG_PATH)
        if not symbols:
            print(f"TDX自选股文件为空: {TDX_ZXG_PATH}")
            sys.exit(1)
    else:
        print("用法: uv run python scripts/bs_points_backtest.py [股票代码1] [股票代码2] ...")
        sys.exit(1)

    # 过滤指数
    symbols = [s for s in symbols if not _is_index(s)]
    if not symbols:
        print("过滤指数后无股票可回测")
        sys.exit(1)

    # 数据时间范围
    lookback = DATA_LOOKBACK.get(freq, 540)
    edt = date.today().strftime("%Y-%m-%d")
    sdt = (date.today() - timedelta(days=lookback)).strftime("%Y-%m-%d")
    bt_sdt = (date.today() - timedelta(days=lookback - WARMUP_MONTHS * 30)).strftime("%Y-%m-%d")

    os.makedirs("output", exist_ok=True)
    log_file = f"output/bs_backtest_{edt.replace('-', '')}.log"
    logger = _setup_logging(log_file)

    t_start = time.time()
    print(f"回测参数: {freq} | {sdt} ~ {edt} | 手续费 {fee_rate*10000:.0f}BP")
    print(f"待回测({len(symbols)}): {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
    print(f"并发数: {args.workers}")
    print("=" * 60)

    # 预取复权因子
    print("获取股票名称...")
    devnull = open(os.devnull, "w")
    name_map = _batch_stock_names(symbols, devnull)

    print(f"预取复权因子 ({len(symbols)}只)...")
    prefetch_factors(symbols, dividend_type="前复权", max_workers=min(args.workers, len(symbols)))

    # 并行回测
    all_results = {}
    total = len(symbols)

    if total == 1:
        symbol = symbols[0]
        print(f"\n[1/1] {name_map.get(symbol, symbol)} ...")
        all_results[symbol] = backtest_one_symbol(symbol, freq, sdt, edt, bt_sdt, fee_rate, logger)
    else:
        workers = min(args.workers, total)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(backtest_one_symbol, sym, freq, sdt, edt, bt_sdt, fee_rate, logger): sym
                for sym in symbols
            }
            done_count = 0
            for future in as_completed(futures):
                sym = futures[future]
                done_count += 1
                try:
                    all_results[sym] = future.result()
                except Exception as e:
                    logger.error(f"{sym} 回测异常: {e}")
                    all_results[sym] = None
                display = name_map.get(sym, sym)
                print(f"  [{done_count}/{total}] {display} 完成")

    # 过滤 None 结果
    all_results = {k: v for k, v in all_results.items() if v is not None}

    # 生成 Markdown 报告
    report_file = f"output/bs_backtest_{edt.replace('-', '')}.md"
    generate_markdown_report(all_results, name_map, freq, sdt, edt, fee_rate, report_file)

    # 生成 HTML 报告
    print("\n生成 HTML 报告...")
    generate_html_reports(all_results, freq, fee_rate, Path("output"))

    elapsed = time.time() - t_start
    print(f"\n  → {report_file}")
    print(f"  → {log_file}")
    print(f"完成，共回测 {len(all_results)} 只股票，耗时 {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
