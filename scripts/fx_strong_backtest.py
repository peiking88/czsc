#!/usr/bin/env python
"""缠论多周期强分型+买卖点扫描：各周期强分型和买卖点共振的股票重点标注

逻辑：
  1. 多周期扫描（如30分钟+日线+周线），每个周期独立分析
  2. 强分型检测：筛选 power_str == "强" 的分型
  3. 买卖点检测：基于笔结构分析的轻量检测（不跑完整信号管线）
     - 结构检测：移植 Rust check_first_buy/check_first_sell 逻辑
     - ZS中枢检测：当前价格相对于中枢位置的买卖点分类
  4. 共振评分：多周期信号对齐程度，0-100分
  5. 重点标注：共振分≥50的股票标记⭐

用法:
  # 单周期（向后兼容）
  uv run python scripts/fx_strong_backtest.py -f 30分钟
  uv run python scripts/fx_strong_backtest.py 600519.SH 000858.SZ -f 日线

  # 多周期扫描（新）
  uv run python scripts/fx_strong_backtest.py --freqs "30分钟,日线,周线"
  uv run python scripts/fx_strong_backtest.py --freqs "30分钟,日线" --min-resonance 2
  uv run python scripts/fx_strong_backtest.py 600519.SH --freqs "30分钟,日线,周线"

输出:
  output/fx_strong_backtest_yyyymmdd.md
"""

import argparse
import logging
import math
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

from czsc import CZSC, ZS
from czsc._native import Direction
from czsc import get_raw_bars

DATA_LOOKBACK = {
    "30分钟": 540, "60分钟": 540, "5分钟": 365, "15分钟": 540,
    "日线": 1095, "周线": 2555, "月线": 5475,
}
TDX_ZXG_PATH = Path("/home/li/.local/share/tdxcfv/drive_c/tc/T0002/blocknew/zxg.blk")

# ─────────────────────────────── 工具函数 ───────────────────────────────


def _setup_logging(log_file):
    try:
        from loguru import logger as loguru_logger
        loguru_logger.remove()
        loguru_logger.add(log_file, level="WARNING", encoding="utf-8")
    except ImportError:
        pass
    logger = logging.getLogger("fx_strong_backtest")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    return logger


def _parse_tdx_zxg(path):
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


def _batch_stock_names(symbols, devnull=None):
    """批量获取股票名称（TDengine stock_name 表）"""
    from czsc import batch_stock_names
    return batch_stock_names(symbols)


def _is_index(symbol):
    return symbol.startswith("999999") or symbol.startswith("399")


# ─────────────────────────────── 买卖点检测（轻量结构分析） ───────────────────────────────


def _check_first_buy_pattern(bis, n):
    """Python 移植 Rust check_first_buy 逻辑（crates/czsc-signals/src/utils/cxt.rs）

    一买条件：
      - 奇数笔，最后笔方向=向下，首尾同方向
      - 首笔最高点=全局最高，末笔最低点=全局最低
      - 背驰：末笔 price_power < max(前一笔, 关键笔均值)
              AND (volume_power 背驰 OR length 背驰)

    Returns: dict or None
    """
    if n % 2 != 1 or len(bis) < n:
        return None
    bis = bis[-n:]
    directions = [b.direction.value for b in bis]
    if directions[-1] != "向下" or directions[0] != directions[-1]:
        return None
    highs = [b.high for b in bis]
    lows = [b.low for b in bis]
    max_high = max(highs)
    min_low = min(lows)
    if max_high != highs[0] or min_low != lows[-1]:
        return None
    # 收集关键笔（每次新低）
    key_bis = []
    for i in range(0, n - 2, 2):
        if i == 0:
            key_bis.append(bis[i])
        elif bis[i].low < bis[i - 2].low:
            key_bis.append(bis[i])
    if not key_bis:
        return None
    # 背驰判断
    last = bis[-1]
    prev = bis[-3]
    kpp = [k.power_price for k in key_bis]
    kpv = [k.power_volume for k in key_bis]
    kln = [float(k.length) for k in key_bis]
    mean_kpp = sum(kpp) / len(kpp)
    mean_kpv = sum(kpv) / len(kpv)
    mean_kln = sum(kln) / len(kln)
    bc_price = last.power_price < max(prev.power_price, mean_kpp)
    bc_volume = last.power_volume < max(prev.power_volume, mean_kpv)
    bc_length = last.length < max(prev.length, mean_kln)
    if bc_price and (bc_volume or bc_length):
        return {
            "type": "一买",
            "category": "buy",
            "score": 80,
            "price_zone": round(last.low, 2),
            "details": f"一买:{n}笔背驰 price_bc={bc_price} vol_bc={bc_volume} len_bc={bc_length}",
        }
    return None


def _check_first_sell_pattern(bis, n):
    """一卖的镜像逻辑"""
    if n % 2 != 1 or len(bis) < n:
        return None
    bis = bis[-n:]
    directions = [b.direction.value for b in bis]
    if directions[-1] != "向上" or directions[0] != directions[-1]:
        return None
    highs = [b.high for b in bis]
    lows = [b.low for b in bis]
    max_high = max(highs)
    min_low = min(lows)
    if min_low != lows[0] or max_high != highs[-1]:
        return None
    # 收集关键笔（每次新高）
    key_bis = []
    for i in range(0, n - 2, 2):
        if i == 0:
            key_bis.append(bis[i])
        elif bis[i].high > bis[i - 2].high:
            key_bis.append(bis[i])
    if not key_bis:
        return None
    last = bis[-1]
    prev = bis[-3]
    kpp = [k.power_price for k in key_bis]
    kpv = [k.power_volume for k in key_bis]
    kln = [float(k.length) for k in key_bis]
    mean_kpp = sum(kpp) / len(kpp)
    mean_kpv = sum(kpv) / len(kpv)
    mean_kln = sum(kln) / len(kln)
    bc_price = last.power_price < max(prev.power_price, mean_kpp)
    bc_volume = last.power_volume < max(prev.power_volume, mean_kpv)
    bc_length = last.length < max(prev.length, mean_kln)
    if bc_price and (bc_volume or bc_length):
        return {
            "type": "一卖",
            "category": "sell",
            "score": 80,
            "price_zone": round(last.high, 2),
            "details": f"一卖:{n}笔背驰 price_bc={bc_price} vol_bc={bc_volume} len_bc={bc_length}",
        }
    return None


def _detect_bs_points_structural(bi_list, max_lookback=12):
    """基于笔结构的一买/一卖检测（移植 Rust 逻辑）"""
    points = []
    if len(bi_list) < 3:
        return points
    lookback = min(max_lookback, len(bi_list))
    # 对不同的窗口大小检测
    for n in [3, 5, 7]:
        if lookback < n:
            break
        result = _check_first_buy_pattern(bi_list[-lookback:], n)
        if result:
            points.append(result)
            break
    for n in [3, 5, 7]:
        if lookback < n:
            break
        result = _check_first_sell_pattern(bi_list[-lookback:], n)
        if result:
            points.append(result)
            break
    return points


def _detect_bs_points_via_zs(bi_list, raw_bars):
    """基于 ZS 中枢的买卖点分类

    中枢由最近3个已完成笔构造，根据当前价格在 zg/zd/zz 的相对位置判断买卖点类型。
    """
    points = []
    if len(bi_list) < 3 or not raw_bars:
        return points
    try:
        zs = ZS(bi_list[-3:])
    except Exception:
        return points
    if not zs.is_valid():
        return points
    current_price = raw_bars[-1].close
    last_direction = bi_list[-1].direction.value  # "向上" or "向下"
    # last_direction == "向下" → ubi 向上 → 找买点
    if last_direction == "向下":
        if current_price < zs.zd:
            points.append({
                "type": "一买(ZS)", "category": "buy", "score": 70,
                "price_zone": round(zs.zd, 2),
                "details": f"价格{current_price:.2f}低于中枢下沿{zs.zd:.2f}",
            })
        elif current_price <= zs.zz:
            points.append({
                "type": "二买(ZS)", "category": "buy", "score": 60,
                "price_zone": round(zs.zz, 2),
                "details": f"价格{current_price:.2f}在中枢区间{zs.zd:.2f}-{zs.zg:.2f}内",
            })
        elif current_price > zs.zg:
            points.append({
                "type": "三买(ZS)", "category": "buy", "score": 50,
                "price_zone": round(zs.zg, 2),
                "details": f"价格{current_price:.2f}高于中枢上沿{zs.zg:.2f}",
            })
    else:  # ubi 向下 → 找卖点
        if current_price > zs.zg:
            points.append({
                "type": "一卖(ZS)", "category": "sell", "score": 70,
                "price_zone": round(zs.zg, 2),
                "details": f"价格{current_price:.2f}高于中枢上沿{zs.zg:.2f}",
            })
        elif current_price >= zs.zz:
            points.append({
                "type": "二卖(ZS)", "category": "sell", "score": 60,
                "price_zone": round(zs.zz, 2),
                "details": f"价格{current_price:.2f}在中枢区间{zs.zd:.2f}-{zs.zg:.2f}内",
            })
        elif current_price < zs.zd:
            points.append({
                "type": "三卖(ZS)", "category": "sell", "score": 50,
                "price_zone": round(zs.zd, 2),
                "details": f"价格{current_price:.2f}低于中枢下沿{zs.zd:.2f}",
            })
    return points


def detect_all_bs_points(bi_list, raw_bars, max_lookback=12):
    """合并结构检测和 ZS 检测的买卖点，按类型去重（结构检测分数高，优先保留）"""
    structural = _detect_bs_points_structural(bi_list, max_lookback)
    zs_based = _detect_bs_points_via_zs(bi_list, raw_bars)
    merged = {}
    for pt in structural + zs_based:
        key = pt["type"].split("(")[0]  # e.g. "一买" from "一买(ZS)"
        if key not in merged or pt["score"] > merged[key]["score"]:
            merged[key] = pt
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


# ─────────────────────────────── 单周期分析 ───────────────────────────────


def _compute_direction_accuracy(strong_fxs, raw, predict_n=5):
    """计算强分型的方向准确率"""
    dir_stats = {"total_fxs": 0, "correct": 0,
                 "ding_total": 0, "ding_correct": 0,
                 "di_total": 0, "di_correct": 0}
    for sfx in strong_fxs:
        future_idx = sfx["idx"] + predict_n
        if future_idx >= len(raw):
            continue
        future_price = raw[future_idx].close
        predicted_up = not sfx["is_ding"]
        actual_up = future_price > sfx["price"]
        correct = (predicted_up == actual_up)
        dir_stats["total_fxs"] += 1
        dir_stats["correct"] += 1 if correct else 0
        if sfx["is_ding"]:
            dir_stats["ding_total"] += 1
            dir_stats["ding_correct"] += 1 if correct else 0
        else:
            dir_stats["di_total"] += 1
            dir_stats["di_correct"] += 1 if correct else 0
    if dir_stats["total_fxs"] > 0:
        dir_stats["accuracy"] = dir_stats["correct"] / dir_stats["total_fxs"] * 100
        dir_stats["ding_accuracy"] = (dir_stats["ding_correct"] / dir_stats["ding_total"] * 100
                                      if dir_stats["ding_total"] > 0 else 0)
        dir_stats["di_accuracy"] = (dir_stats["di_correct"] / dir_stats["di_total"] * 100
                                     if dir_stats["di_total"] > 0 else 0)
    return dir_stats


def _match_trades(strong_fxs):
    """从强分型序列匹配交易：底分型(强)买 → 顶分型(强)卖"""
    trades = []
    holding = None
    for sfx in strong_fxs:
        if not holding and not sfx["is_ding"]:
            holding = {"buy_dt": sfx["dt"], "buy_idx": sfx["idx"],
                        "buy_price": sfx["price"], "buy_type": sfx["type"]}
        elif holding and sfx["is_ding"]:
            ret_pct = (sfx["price"] - holding["buy_price"]) / holding["buy_price"] * 100
            hold_bars = sfx["idx"] - holding["buy_idx"]
            direction_correct = sfx["price"] > holding["buy_price"]
            trades.append({
                "buy_dt": holding["buy_dt"],
                "sell_dt": sfx["dt"],
                "buy_price": holding["buy_price"],
                "sell_price": sfx["price"],
                "return_pct": ret_pct,
                "hold_bars": hold_bars,
                "direction_correct": direction_correct,
                "buy_type": holding["buy_type"],
                "sell_type": sfx["type"],
            })
            holding = None
    return trades


def _calc_stats(trades):
    """从交易列表计算统计指标"""
    if not trades:
        return {"total_trades": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    n = len(trades)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0001
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
    return {
        "total_trades": n,
        "win_trades": len(wins),
        "win_rate": len(wins) / n * 100,
        "avg_return": sum(rets) / n,
        "total_return": sum(rets),
        "max_win": max(rets),
        "max_loss": min(rets),
        "avg_hold_bars": sum(t["hold_bars"] for t in trades) / n,
        "profit_factor": profit_factor,
        "direction_correct": sum(1 for t in trades if t["direction_correct"]),
        "direction_accuracy": sum(1 for t in trades if t["direction_correct"]) / n * 100,
        "long_count": n,
        "long_win_rate": len(wins) / n * 100,
        "long_avg_return": sum(rets) / n,
    }


def analyze_period(symbol, freq, sdt, edt, logger=None):
    """单周期分析：强分型 + 买卖点 + 方向准确率

    Returns dict:
      {
        "data_available": bool,
        "bar_count": int,
        "bi_count": int,
        "fx_count": int,
        "strong_fx_count": int,
        "strong_fxs": list,
        "direction_accuracy": dict,
        "bs_points": list,
        "latest_strong": dict or None,
        "trades": list,
        "stats": dict,
      }
    """
    bars = get_raw_bars(symbol, freq, sdt, edt, fq="前复权")
    if not bars or len(bars) < 30:
        return {"data_available": False}

    c = CZSC(bars)
    raw = c.bars_raw

    # dt → index 映射
    dt_to_idx = {str(b.dt): i for i, b in enumerate(raw)}

    # 筛选强分型
    strong_fxs = []
    for fx in c.fx_list:
        if fx.power_str != "强":
            continue
        mid_dt = str(fx.elements[1].dt) if len(fx.elements) >= 2 else str(fx.dt)
        idx = dt_to_idx.get(mid_dt)
        if idx is None:
            continue
        is_ding = fx.mark.value == "顶分型"
        strong_fxs.append({
            "dt": str(fx.dt),
            "idx": idx,
            "price": fx.fx,
            "is_ding": is_ding,
            "type": "顶分型(强)" if is_ding else "底分型(强)",
        })

    # 方向准确率
    dir_stats = _compute_direction_accuracy(strong_fxs, raw)

    # 买卖点检测
    bs_points = []
    if c.bi_list and len(c.bi_list) >= 3:
        bs_points = detect_all_bs_points(c.bi_list, raw)

    # 最近强分型
    latest_strong = strong_fxs[-1] if strong_fxs else None

    # 交易匹配
    trades = _match_trades(strong_fxs)
    stats = _calc_stats(trades)

    return {
        "data_available": True,
        "bar_count": len(raw),
        "bi_count": len(c.bi_list),
        "fx_count": len(c.fx_list),
        "strong_fx_count": len(strong_fxs),
        "strong_fxs": strong_fxs,
        "direction_accuracy": dir_stats,
        "bs_points": bs_points,
        "latest_strong": latest_strong,
        "trades": trades,
        "stats": stats,
    }


# ─────────────────────────────── 多周期聚合 ───────────────────────────────


def _compute_resonance(period_results, freqs):
    """计算多周期共振评分

    评分维度：
      1. 强分型共振（权重 40）：≥2周期有强底分型 +20，≥3周期 +40
      2. 买卖点共振（权重 40）：≥2周期有同类买点 +30，≥3周期 +40
      3. 分型+买卖点重合（权重 20）：同周期同时有强底分型+买点 +10/周期

    Returns:
      {
        "score": int, "star_rating": int, "highlight": bool,
        "strong_fx_buy_periods": [...], "strong_fx_sell_periods": [...],
        "bs_buy_periods": [...], "bs_sell_periods": [...],
        "coincident_periods": [...],
        "tags": [...],
      }
    """
    score = 0
    strong_buy = []
    strong_sell = []
    bs_buy = []
    bs_sell = []
    coincident = []
    tags = []

    for freq in freqs:
        r = period_results.get(freq)
        if not r or not r.get("data_available"):
            continue

        # 强分型
        if r.get("latest_strong"):
            ls = r["latest_strong"]
            if ls["is_ding"]:
                strong_sell.append(freq)
            else:
                strong_buy.append(freq)

        # 买卖点
        has_buy = any(p["category"] == "buy" for p in r.get("bs_points", []))
        has_sell = any(p["category"] == "sell" for p in r.get("bs_points", []))
        if has_buy:
            bs_buy.append(freq)
        if has_sell:
            bs_sell.append(freq)

        # 分型+买卖点方向一致
        ls = r.get("latest_strong")
        if ls and has_buy and not ls["is_ding"]:
            coincident.append(freq)  # 强底分型 + 买点
        if ls and has_sell and ls["is_ding"]:
            coincident.append(freq)  # 强顶分型 + 卖点

    # 1. 强分型共振（买入侧）
    n_strong_buy = len(strong_buy)
    if n_strong_buy >= 3:
        score += 40
    elif n_strong_buy >= 2:
        score += 20
    elif n_strong_buy >= 1:
        score += 10

    # 强分型共振（卖出侧，权重减半）
    n_strong_sell = len(strong_sell)
    if n_strong_sell >= 3:
        score += 20
    elif n_strong_sell >= 2:
        score += 10
    elif n_strong_sell >= 1:
        score += 5

    # 2. 买卖点共振（买入侧）
    n_bs_buy = len(bs_buy)
    if n_bs_buy >= 3:
        score += 40
    elif n_bs_buy >= 2:
        score += 30
    elif n_bs_buy >= 1:
        score += 10

    # 买卖点共振（卖出侧，权重减半）
    n_bs_sell = len(bs_sell)
    if n_bs_sell >= 3:
        score += 20
    elif n_bs_sell >= 2:
        score += 15
    elif n_bs_sell >= 1:
        score += 5

    # 检查是否有同类型买卖点跨周期
    bs_types_by_period = {}
    for freq in freqs:
        r = period_results.get(freq)
        if not r or not r.get("data_available"):
            continue
        for pt in r.get("bs_points", []):
            t = pt["type"].split("(")[0]  # "一买", "二买", etc
            bs_types_by_period.setdefault(t, []).append(freq)
    same_type_multi = sum(1 for freqs_list in bs_types_by_period.values() if len(freqs_list) >= 2)
    if same_type_multi >= 2:
        score += 15
    elif same_type_multi >= 1:
        score += 5

    # 3. 分型+买卖点方向一致（强底+买点 or 强顶+卖点）
    n_coincident = len(coincident)
    score += min(n_coincident * 10, 20)

    # 星级评定
    if score >= 80:
        stars, highlight = 3, True
    elif score >= 50:
        stars, highlight = 2, True
    elif score >= 30:
        stars, highlight = 1, False
    else:
        stars, highlight = 0, False

    if highlight:
        tags.append(f"{'★' * stars}")

    return {
        "score": score,
        "star_rating": stars,
        "highlight": highlight,
        "strong_fx_buy_periods": strong_buy,
        "strong_fx_sell_periods": strong_sell,
        "bs_buy_periods": bs_buy,
        "bs_sell_periods": bs_sell,
        "coincident_periods": coincident,
        "tags": tags,
    }


def analyze_stock_multi_period(symbol, freqs, sdt, edt, logger=None):
    """对单只股票执行多周期分析

    Returns:
      {"periods": {freq: result}, "resonance": {...}, "summary": str}
    """
    period_results = {}
    for freq in freqs:
        period_results[freq] = analyze_period(symbol, freq, sdt, edt, logger)

    resonance = _compute_resonance(period_results, freqs)
    summary = _make_stock_summary(symbol, period_results, resonance, freqs)
    return {"periods": period_results, "resonance": resonance, "summary": summary}


def _make_stock_summary(symbol, period_results, resonance, freqs):
    """生成股票单行摘要"""
    parts = []
    for freq in freqs:
        r = period_results.get(freq)
        codes = []
        if r and r.get("data_available"):
            ls = r.get("latest_strong")
            if ls:
                codes.append("底(强)" if not ls["is_ding"] else "顶(强)")
            for pt in r.get("bs_points", []):
                codes.append(pt["type"].split("(")[0])
        parts.append("+".join(codes) if codes else "-")
    parts.append(f"共振={resonance['score']}")
    if resonance["highlight"]:
        parts.append("⭐" * resonance["star_rating"])
    return " | ".join(parts)


# ─────────────────────────────── 报告生成 ───────────────────────────────


def _signal_short_code(period_results, freq):
    """生成单周期信号简码"""
    r = period_results.get(freq)
    if not r or not r.get("data_available"):
        return "-"
    codes = []
    ls = r.get("latest_strong")
    if ls:
        codes.append("底(强)" if not ls["is_ding"] else "顶(强)")
    for pt in r.get("bs_points", []):
        t = pt["type"].split("(")[0]
        codes.append(t)
    return "+".join(codes) if codes else "○"


def generate_report(all_results, name_map, freqs, sdt, edt, min_resonance, filename):
    """生成多周期 Markdown 扫描报告"""
    lines = []
    lines.append("# 缠论多周期强分型+买卖点扫描报告")
    lines.append("")
    lines.append(f"- **扫描周期**: {', '.join(freqs)}")
    lines.append(f"- **数据范围**: {sdt} ~ {edt}")
    lines.append(f"- **股票数**: {len(all_results)}")
    lines.append(f"- **共振阈值**: ≥{min_resonance}周期")
    lines.append("")
    lines.append("> 信号简码：底(强)=强底分型，顶(强)=强顶分型，"
                 "一买/二买/三买=笔结构买点，一卖/二卖/三卖=笔结构卖点")
    lines.append("")

    # 过滤有效数据
    valid = {}
    for sym, r in all_results.items():
        if r and any(p.get("data_available") for p in r["periods"].values()):
            valid[sym] = r

    if not valid:
        lines.append("⚠️ 无有效数据")
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return

    # ── 按共振分排序 ──
    ranked = sorted(valid.items(), key=lambda x: x[1]["resonance"]["score"], reverse=True)
    resonance_stocks = [(sym, r) for sym, r in ranked if r["resonance"]["highlight"]]

    # ════════════════════════════════════════════════════════
    # 一、强共振股票（重点关注）
    # ════════════════════════════════════════════════════════
    lines.append("## ⭐ 一、强共振股票（重点关注）")
    lines.append("")
    if not resonance_stocks:
        lines.append("> 暂无达到共振阈值的股票")
        lines.append("")
    else:
        lines.append(f"> 以下 {len(resonance_stocks)} 只股票在多周期同时出现强分型+买卖点信号，建议重点关注")
        lines.append("")
        lines.append("| 排名 | 股票 | 共振分 | " + " | ".join(freqs) + " | 共振特征 |")
        lines.append("|------|------|--------|" + "|".join(["------"] * len(freqs)) + "|----------|")
        for rank, (sym, r) in enumerate(resonance_stocks, 1):
            name = name_map.get(sym, sym)
            res = r["resonance"]
            stars = "⭐" * res["star_rating"]
            cells = [_signal_short_code(r["periods"], f) for f in freqs]
            # 共振特征描述
            features = []
            if res["strong_fx_buy_periods"]:
                features.append(f"强底:{','.join(res['strong_fx_buy_periods'])}")
            if res["bs_buy_periods"]:
                features.append(f"买点:{','.join(res['bs_buy_periods'])}")
            if res["coincident_periods"]:
                features.append(f"共振:{','.join(res['coincident_periods'])}")
            feat_str = "; ".join(features) if features else "-"
            lines.append(
                f"| {rank} | {stars} **{name}** | {res['score']} | "
                + " | ".join(cells) + f" | {feat_str} |"
            )
        lines.append("")

    # ════════════════════════════════════════════════════════
    # 二、各周期买卖点统计
    # ════════════════════════════════════════════════════════
    lines.append("## 二、各周期买卖点统计")
    lines.append("")
    bs_types = ["一买", "二买", "三买", "一卖", "二卖", "三卖"]
    header = "| 周期 | " + " | ".join(bs_types) + " | 强底分型 | 强顶分型 |"
    sep = "|------|" + "|".join(["------"] * (len(bs_types) + 2)) + "|"
    lines.append(header)
    lines.append(sep)

    for freq in freqs:
        bs_counts = {t: 0 for t in bs_types}
        strong_di = 0
        strong_ding = 0
        for sym, r in valid.items():
            p = r["periods"].get(freq)
            if not p or not p.get("data_available"):
                continue
            for sfx in p.get("strong_fxs", []):
                if sfx["is_ding"]:
                    strong_ding += 1
                else:
                    strong_di += 1
            for pt in p.get("bs_points", []):
                t = pt["type"].split("(")[0]
                if t in bs_counts:
                    bs_counts[t] += 1
        cells = [str(bs_counts[t]) for t in bs_types] + [str(strong_di), str(strong_ding)]
        lines.append(f"| {freq} | " + " | ".join(cells) + " |")
    lines.append("")

    # ════════════════════════════════════════════════════════
    # 三、各周期方向准确率汇总
    # ════════════════════════════════════════════════════════
    lines.append("## 三、各周期强分型方向准确率汇总")
    lines.append("")
    lines.append("| 周期 | 总强分型 | 方向准确率 | 顶分型准确率 | 底分型准确率 | 交易次数 | 交易胜率 | 平均收益 |")
    lines.append("|------|----------|------------|-------------|-------------|----------|----------|----------|")

    for freq in freqs:
        total_fx = 0
        total_correct = 0
        total_ding = 0
        ding_correct = 0
        total_di = 0
        di_correct = 0
        total_trades = 0
        total_wins = 0
        all_returns = []

        for sym, r in valid.items():
            p = r["periods"].get(freq)
            if not p or not p.get("data_available"):
                continue
            da = p.get("direction_accuracy", {})
            total_fx += da.get("total_fxs", 0)
            total_correct += da.get("correct", 0)
            total_ding += da.get("ding_total", 0)
            ding_correct += da.get("ding_correct", 0)
            total_di += da.get("di_total", 0)
            di_correct += da.get("di_correct", 0)
            st = p.get("stats", {})
            total_trades += st.get("total_trades", 0)
            total_wins += st.get("win_trades", 0)
            if st.get("total_trades", 0) > 0:
                all_returns.append(st["avg_return"])

        acc = f"{total_correct / total_fx * 100:.1f}%" if total_fx > 0 else "-"
        d_acc = f"{ding_correct / total_ding * 100:.1f}%" if total_ding > 0 else "-"
        di_acc = f"{di_correct / total_di * 100:.1f}%" if total_di > 0 else "-"
        wr = f"{total_wins / total_trades * 100:.1f}%" if total_trades > 0 else "-"
        ar = f"{sum(all_returns) / len(all_returns):+.2f}%" if all_returns else "-"

        lines.append(
            f"| {freq} | {total_fx} | {acc} | {d_acc} | {di_acc} "
            f"| {total_trades} | {wr} | {ar} |"
        )
    lines.append("")

    # ════════════════════════════════════════════════════════
    # 四、全量股票排名（按共振分）
    # ════════════════════════════════════════════════════════
    lines.append("## 四、全量股票扫描排名")
    lines.append("")
    lines.append("| 排名 | 股票 | 共振分 | " + " | ".join(freqs) + " |")
    lines.append("|------|------|--------|" + "|".join(["------"] * len(freqs)) + "|")

    for rank, (sym, r) in enumerate(ranked, 1):
        name = name_map.get(sym, sym)
        res = r["resonance"]
        stars = "⭐" * res["star_rating"] if res["highlight"] else ""
        prefix = f"{stars} " if stars else ""
        cells = [_signal_short_code(r["periods"], f) for f in freqs]
        lines.append(
            f"| {rank} | {prefix}**{name}** | {res['score']} | " + " | ".join(cells) + " |"
        )
    lines.append("")

    # ════════════════════════════════════════════════════════
    # 五、共振股票详情
    # ════════════════════════════════════════════════════════
    if resonance_stocks:
        lines.append("## 五、共振股票详情")
        lines.append("")

        for sym, r in resonance_stocks:
            name = name_map.get(sym, sym)
            res = r["resonance"]
            stars = "⭐" * res["star_rating"]
            lines.append(f"### {stars} {name}（{sym}）")
            lines.append(f"- **共振评分**: {res['score']}分")
            if res["coincident_periods"]:
                lines.append(f"- **分型+买卖点重合周期**: {', '.join(res['coincident_periods'])}")
            lines.append("")

            lines.append("| 周期 | K线数 | 笔数 | 分型数 | 强分型 | 最新信号 | 买卖点 | 方向准确率 |")
            lines.append("|------|-------|------|--------|--------|----------|--------|------------|")
            for freq in freqs:
                p = r["periods"].get(freq)
                if not p or not p.get("data_available"):
                    lines.append(f"| {freq} | - | - | - | - | - | - | - |")
                    continue
                ls = p.get("latest_strong")
                latest_str = (f"{ls['type']} {ls['price']:.2f}元" if ls else "○")
                bs_str = ", ".join(pt["type"].split("(")[0] for pt in p.get("bs_points", [])) or "○"
                da = p.get("direction_accuracy", {})
                acc_str = f"{da.get('accuracy', 0):.1f}%" if da.get("total_fxs", 0) > 0 else "-"
                lines.append(
                    f"| {freq} | {p['bar_count']} | {p['bi_count']} | {p['fx_count']} "
                    f"| {p['strong_fx_count']} | {latest_str} | {bs_str} | {acc_str} |"
                )
            lines.append("")

            # 买卖点详情（仅共振股票展开）
            bs_details = []
            for freq in freqs:
                p = r["periods"].get(freq)
                if not p or not p.get("data_available"):
                    continue
                for pt in p.get("bs_points", []):
                    bs_details.append(f"  - [{freq}] {pt['type']} | {pt['details']}")
            if bs_details:
                lines.append("**买卖点详情**:")
                lines.extend(bs_details)
                lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─────────────────────────────── 主流程 ───────────────────────────────


def _resolve_freqs(args):
    """解析周期列表"""
    if args.freqs:
        return [f.strip() for f in args.freqs.split(",")]
    return [args.freq]


def main():
    parser = argparse.ArgumentParser(description="缠论多周期强分型+买卖点扫描")
    parser.add_argument("symbols", nargs="*", help="股票代码")
    parser.add_argument("-n", "--workers", type=int, default=16, help="并行线程数（默认 16）")
    parser.add_argument("-f", "--freq", default="30分钟", help="单周期模式（默认 30分钟）")
    parser.add_argument("--freqs", default=None,
                        help="多周期模式：逗号分隔，如 '30分钟,日线,周线'")
    parser.add_argument("--min-resonance", type=int, default=1,
                        help="最少共振周期数（默认 1，多周期模式下建议 2）")
    args = parser.parse_args()

    freqs = _resolve_freqs(args)
    multi_period = len(freqs) > 1

    if args.symbols:
        symbols = args.symbols
    elif TDX_ZXG_PATH.exists():
        symbols = _parse_tdx_zxg(TDX_ZXG_PATH)
        if not symbols:
            sys.exit(1)
    else:
        print("用法: uv run python scripts/fx_strong_backtest.py [股票代码] [-f 日线] [--freqs '30分钟,日线']")
        sys.exit(1)

    symbols = [s for s in symbols if not _is_index(s)]
    if not symbols:
        sys.exit(1)

    # 按最长数据需求取回看天数
    max_lookback = max(DATA_LOOKBACK.get(f, 540) for f in freqs)
    edt = date.today().strftime("%Y-%m-%d")
    sdt = (date.today() - timedelta(days=max_lookback)).strftime("%Y-%m-%d")

    os.makedirs("output", exist_ok=True)
    log_file = f"output/fx_strong_backtest_{edt.replace('-', '')}.log"
    logger = _setup_logging(log_file)

    t_start = time.time()
    print(f"扫描参数: {', '.join(freqs)} | {sdt} ~ {edt} | "
          f"策略: 强分型 + 买卖点结构分析 | 共振阈值: ≥{args.min_resonance}周期")
    print(f"待扫描({len(symbols)}): {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
    print(f"并发数: {args.workers}")
    print("=" * 60)

    print("获取股票名称...")
    name_map = _batch_stock_names(symbols)

    all_results = {}
    total = len(symbols)

    if total == 1:
        sym = symbols[0]
        print(f"\n[1/1] {name_map.get(sym, sym)} ...")
        all_results[sym] = analyze_stock_multi_period(sym, freqs, sdt, edt, logger)
        r = all_results[sym]
        res = r["resonance"]
        stars = "⭐" * res["star_rating"] if res["highlight"] else ""
        print(f"  → 共振={res['score']} {stars}")
    else:
        workers = min(args.workers, total)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(analyze_stock_multi_period, sym, freqs, sdt, edt, logger): sym
                       for sym in symbols}
            done_count = 0
            for future in as_completed(futures):
                sym = futures[future]
                done_count += 1
                try:
                    all_results[sym] = future.result()
                except Exception as e:
                    logger.error(f"{sym} 分析失败: {e}")
                    all_results[sym] = None
                display = name_map.get(sym, sym)
                r = all_results[sym]
                if r:
                    res = r["resonance"]
                    stars = "⭐" * res["star_rating"] if res["highlight"] else ""
                    print(f"  [{done_count}/{total}] {display} 共振={res['score']} {stars}")
                else:
                    print(f"  [{done_count}/{total}] {display} 失败")

    all_results = {k: v for k, v in all_results.items() if v is not None}

    report_file = f"output/fx_strong_backtest_{edt.replace('-', '')}.md"
    generate_report(all_results, name_map, freqs, sdt, edt, args.min_resonance, report_file)

    elapsed = time.time() - t_start

    # 扫描摘要
    high_res = sum(1 for r in all_results.values() if r["resonance"]["star_rating"] >= 3)
    mid_res = sum(1 for r in all_results.values() if r["resonance"]["star_rating"] >= 2)
    low_res = sum(1 for r in all_results.values() if r["resonance"]["star_rating"] >= 1)

    print(f"\n  → {report_file}")
    print(f"  → {log_file}")
    print(f"扫描完成: {len(all_results)}只 | 耗时 {elapsed:.1f}秒")
    print(f"共振股票: {mid_res}只 (≥50分) | 强共振: {high_res}只 (≥80分) | 一般共振: {low_res}只 (≥30分)")


if __name__ == "__main__":
    main()
