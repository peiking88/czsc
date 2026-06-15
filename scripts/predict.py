#!/usr/bin/env python
"""一键预测脚本：为每只股票生成缠论趋势质量评估报告（1d/30m/5m）

用法:
  uv run python scripts/predict.py                  # 从TDX自选股读取
  uv run python scripts/predict.py 600519.SH 999999.SH  # 手动指定
  uv run python scripts/predict.py -n 4 600519.SH 999999.SH  # 指定并发数

输出:
  自选股模式 → output/czsc-zxg-yyyymmdd.md
  手动模式   → output/czsc-<symbol>.md
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
from czsc import CZSC, Freq, ZS
import math
import time

import numpy as np
import talib

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


def _find_divergence(series, closes, lookback=80):
    """检测 series 与价格的顶/底背离，返回 "顶背离" / "底背离" / "" """
    n = len(series)
    start = max(0, n - lookback)
    peaks, troughs = [], []
    for i in range(start + 1, n - 1):
        if math.isnan(series[i]):
            continue
        if series[i] > series[i - 1] and series[i] > series[i + 1]:
            peaks.append((i, series[i], closes[i]))
        if series[i] < series[i - 1] and series[i] < series[i + 1]:
            troughs.append((i, series[i], closes[i]))
    if len(peaks) >= 2 and peaks[-1][2] > peaks[-2][2] and peaks[-1][1] < peaks[-2][1]:
        return "顶背离"
    if len(troughs) >= 2 and troughs[-1][2] < troughs[-2][2] and troughs[-1][1] > troughs[-2][1]:
        return "底背离"
    return ""


def _fx_enhanced_power(fx):
    """增强分型强度判定：根据影线 + 成交量组合决定强度

    规则：
      底分型/买点 + 长下影线 + 明显放量 → 强（趋势转强）
      顶分型/卖点 + 长上影线 + 明显放量 → 弱（趋势转弱）
      影线满足但放量不明显 → 中
      其他 → 保持 Rust 核心的原始强度

    返回格式："强|长下影|放量" 或 "中" 或 "弱" 等
    """
    power = fx.power_str  # 原始强度：强/中/弱
    is_ding = fx.mark.value == "顶分型"
    tags = []

    if len(fx.elements) < 3:
        return power

    mid = fx.elements[1]  # 分型极值K线
    prev = fx.elements[0]
    nxt = fx.elements[2]

    total_range = mid.high - mid.low
    if total_range <= 0:
        return power

    # ── 影线因子 ──
    upper_shadow = mid.high - max(mid.close, mid.open)
    lower_shadow = min(mid.close, mid.open) - mid.low
    upper_pct = upper_shadow / total_range * 100
    lower_pct = lower_shadow / total_range * 100

    has_long_shadow = False
    if is_ding and upper_pct >= 40:
        tags.append("长上影")
        has_long_shadow = True
    elif not is_ding and lower_pct >= 40:
        tags.append("长下影")
        has_long_shadow = True

    # ── 成交量因子 ──
    avg_vol = (prev.vol + nxt.vol) / 2 if (prev.vol + nxt.vol) > 0 else 1
    vol_ratio = mid.vol / avg_vol if avg_vol > 0 else 1
    is_obvious_vol = vol_ratio >= 1.5

    if is_obvious_vol:
        tags.append("放量")
    elif vol_ratio <= 0.6:
        tags.append("缩量")

    # ── 强度判定：影线 + 放量组合 ──
    if has_long_shadow:
        if is_obvious_vol:
            # 底分型+长下影+放量 → 强
            # 顶分型+长上影+放量 → 强
            power = "强"
        else:
            # 影线满足但放量不明显 → 中
            power = "中"

    tags.insert(0, power)
    return "|".join(tags) if len(tags) > 1 else power


def _macd_cci_status(bars_raw):
    """计算 MACD 和 CCI 当前状态（含背离检测），返回 (macd_text, cci_text)"""
    closes = np.array([b.close for b in bars_raw], dtype=float)
    highs = np.array([b.high for b in bars_raw], dtype=float)
    lows = np.array([b.low for b in bars_raw], dtype=float)

    # ── MACD ──
    macd_text = "-"
    if len(closes) >= 35:
        macd_line, signal, hist = talib.MACD(closes)
        if not np.isnan(hist[-1]) and not np.isnan(hist[-2]):
            m, s = macd_line[-1], signal[-1]
            h_cur, h_prev = hist[-1], hist[-2]

            # 柱状图方向决定动能强弱
            # 红柱放大 → 上涨动能增强，红柱缩小 → 上涨动能衰竭
            # 绿柱放大 → 下跌动能增强，绿柱缩小 → 下跌动能衰竭
            if h_cur > h_prev:
                strength = "上涨动能增强" if h_cur > 0 else "下跌动能衰竭"
            else:
                strength = "上涨动能衰竭" if h_cur > 0 else "下跌动能增强"

            # 柱状状态
            if m > s and macd_line[-2] <= signal[-2]:
                bar = "金叉"
            elif m < s and macd_line[-2] >= signal[-2]:
                bar = "死叉"
            elif h_cur > 0 and h_cur > h_prev:
                bar = "红柱放大"
            elif h_cur > 0:
                bar = "红柱缩小"
            elif h_cur < 0 and h_cur < h_prev:
                bar = "绿柱放大"
            else:
                bar = "绿柱缩小"

            macd_text = f"{bar} {strength}"

            # MACD 背离检测（用 MACD 柱状图）
            h_list = [float(x) for x in hist]
            close_list = [float(x) for x in closes]
            div = _find_divergence(h_list, close_list)
            if div:
                macd_text += f" {div}"

    # ── CCI(14) ──
    cci_text = "-"
    if len(closes) >= 16:
        cci = talib.CCI(highs, lows, closes, timeperiod=14)
        cci_val = float(cci[-1])
        cci_prev = float(cci[-2]) if len(cci) >= 2 and not np.isnan(cci[-2]) else cci_val
        if not math.isnan(cci_val):
            # CCI 强弱势：结合区间 + 趋势方向
            cci_rising = cci_val > cci_prev
            if cci_val > 100:
                strength = "上涨动能增强" if cci_rising else "上涨动能衰竭"
            elif cci_val < -100:
                strength = "下跌动能增强" if not cci_rising else "下跌动能衰竭"
            elif cci_val > 0:
                strength = "偏多"
            else:
                strength = "偏空"

            close_list = [float(x) for x in closes]
            cci_list = [float(x) for x in cci]
            div = _find_divergence(cci_list, close_list)

            cci_text = f"{cci_val:.0f} {strength}"
            if div:
                cci_text += f" {div}"

    return macd_text, cci_text


# 加速度阈值：不同周期价格波动幅度不同，用标准化阈值
# threshold = 加速度 / 笔均价比 × 1000（千分比），避免绝对值受价格水平影响
ACCEL_THRESHOLDS = {
    "1d":  (0.3, -0.3),   # 日线：千分之0.3
    "30m": (1.0, -1.0),   # 30分钟：千分之1.0
    "5m":  (3.0, -3.0),   # 5分钟：千分之3.0（噪声大，阈值宽松）
}


def assess_trend(czsc_obj, freq_label="1d"):
    """从 CZSC 对象提取趋势质量评估，返回 dict 或 None"""
    if not czsc_obj.bi_list:
        return None

    bi_list = czsc_obj.bi_list
    last_bi = bi_list[-1]

    cur_rsq = last_bi.rsq
    if cur_rsq > 0.8:
        rsq_msg = f"🟢 趋势规整 (R²={cur_rsq:.3f})<br>方向明确"
    elif cur_rsq > 0.6:
        rsq_msg = f"🟡 趋势一般 (R²={cur_rsq:.3f})<br>关注方向变化"
    else:
        rsq_msg = f"🔴 趋势散乱 (R²={cur_rsq:.3f})<br>方向不确定"

    # 加速度标准化：accel / 笔均价 × 1000（千分比）
    accel = last_bi.acceleration
    bi_avg_price = (last_bi.high + last_bi.low) / 2 if last_bi.high > 0 else 1
    accel_norm = accel / bi_avg_price * 1000
    up_th, dn_th = ACCEL_THRESHOLDS.get(freq_label, (0.3, -0.3))
    if accel_norm > up_th:
        accel_msg = f"🟢 加速中 ({accel_norm:.2f}‰)<br>趋势强劲"
    elif accel_norm > dn_th:
        accel_msg = f"🟡 匀速/减速 ({accel_norm:.2f}‰)<br>关注转折"
    else:
        accel_msg = f"🔴 反向加速 ({accel_norm:.2f}‰)<br>趋势可能反转"

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
        ubi_up = last_bi.direction.value == "向下"
        ubi_dir = "↑ 向上" if ubi_up else "↓ 向下"
        first_close = ubi_bars[0].close
        last_close = ubi_bars[-1].close
        cur_price = last_close
        power_val = last_close - first_close
        price_pct = power_val / first_close * 100 if first_close > 0 else 0

        # ── 分型（增强：影线+成交量） ──
        ubi_fxs = czsc_obj.ubi_fxs
        fx_text = "-"
        last_fx_power = "-"
        if ubi_fxs:
            last_fx = ubi_fxs[-1]
            fx_mark = "顶分型" if last_fx.mark.value == "顶分型" else "底分型"
            last_fx_power = _fx_enhanced_power(last_fx)
            fx_text = f"{fx_mark}({last_fx_power})"

        # ── 中枢 ──
        zs_text = "-"
        zs = None
        if len(bi_list) >= 3:
            try:
                zs = ZS(bi_list[-3:])
                if zs.is_valid() and zs.zg >= zs.zd:
                    if zs.zd <= cur_price <= zs.zg:
                        zs_text = f"{zs.zd:.2f}-{zs.zg:.2f}"
                    elif cur_price > zs.zg:
                        zs_text = f"上方 {zs.zg:.2f}"
                    else:
                        zs_text = f"下方 {zs.zd:.2f}"
                else:
                    zs = None
            except Exception:
                pass

        # ── 买卖点 ──
        # ubi向上 → 买点分析；ubi向下 → 卖点分析
        bs_text = "-"
        if zs is not None:
            if ubi_up:
                if cur_price < zs.zd:
                    bs_text = f"一买（{last_fx_power}）"
                elif cur_price <= zs.zg:
                    bs_text = f"二买（{last_fx_power}）"
                else:
                    bs_text = f"三买（{last_fx_power}）"
            else:
                if cur_price > zs.zg:
                    bs_text = f"一卖（{last_fx_power}）"
                elif cur_price >= zs.zd:
                    bs_text = f"二卖（{last_fx_power}）"
                else:
                    bs_text = f"三卖（{last_fx_power}）"

        # ── MACD / CCI ──
        macd_text, cci_text = _macd_cci_status(czsc_obj.bars_raw)

        ubi_info = (
            f"未完成笔 ({ubi_bar_count} 根K线)：{ubi_dir}<br>"
            f"起始：{str(ubi_bars[0].dt)[:10]}<br>"
            f"力度：{power_val:+.2f} ({price_pct:+.1f}%)<br>"
            f"分型：{fx_text}<br>"
            f"中枢：{zs_text}<br>"
            f"买卖点：{bs_text}<br>"
            f"MACD：{macd_text}<br>"
            f"CCI：{cci_text}"
        )

    return {
        "rsq_msg": rsq_msg,
        "accel_msg": accel_msg,
        "power_msg": power_msg,
        "direction": direction,
        "ubi_info": ubi_info,
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
            trend = assess_trend(czsc_obj, freq_label=label)
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


def _extract_bs_from_ubi(ubi_info):
    """从未完成笔信息中提取买卖点文本"""
    if not ubi_info:
        return ""
    for prefix in ("买卖点：", "买卖点:"):
        if prefix in ubi_info:
            return ubi_info.split(prefix)[-1].split("<br>")[0]
    return ""


def _position_alert(results):
    """跨周期仓位提醒：三周期同时出现买点/卖点时提醒

    加仓：1d + 30m + 5m 同时出现任意买点（一买/二买/三买）
    减仓：1d + 30m + 5m 同时出现任意卖点（一卖/二卖/三卖）
    """
    for label in ("1d", "30m", "5m"):
        r = results.get(label)
        if not r or "error" in r:
            return "", ""

    d_bs = _extract_bs_from_ubi(results["1d"]["ubi_info"])
    m30_bs = _extract_bs_from_ubi(results["30m"]["ubi_info"])
    m5_bs = _extract_bs_from_ubi(results["5m"]["ubi_info"])

    all_buy = all("买" in bs for bs in [d_bs, m30_bs, m5_bs])
    all_sell = all("卖" in bs for bs in [d_bs, m30_bs, m5_bs])

    if all_buy:
        pts = "、".join(bs.split("（")[0] for bs in [d_bs, m30_bs, m5_bs])
        return f"🔺 加仓：三周期共振买点（{pts}）", "add"
    if all_sell:
        pts = "、".join(bs.split("（")[0] for bs in [d_bs, m30_bs, m5_bs])
        return f"🔻 减仓：三周期共振卖点（{pts}）", "reduce"

    return "", ""


def _strong_fx_bs_star(results):
    """三周期同时出现强分型+买卖点时标注⭐⭐

    条件：1d/30m/5m 每个周期的未完成笔分型为"强"且存在买卖点（非"-"）
    """
    for label in ("1d", "30m", "5m"):
        r = results.get(label)
        if not r or "error" in r:
            return ""
    required = 0
    for label in ("1d", "30m", "5m"):
        ubi = results[label].get("ubi_info", "")
        fx_ok = "强)" in ubi  # 分型强度为"强"，如"底分型(强)"
        bs_ok = "买" in ubi or "卖" in ubi  # 存在买卖点
        if fx_ok and bs_ok:
            required += 1
    if required >= 3:
        return '<span style="color:orange">⭐⭐</span> '
    if required >= 2:
        return '<span style="color:orange">⭐</span> '
    return ""


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
    fx_star = _strong_fx_bs_star(results)
    label = f"{name}（{symbol}）" if name else symbol
    lines = []

    markers = f"{star}{fx_star}".strip()
    if markers:
        lines.append(f"<h1>{markers} {label} 缠论趋势预测</h1>")
    else:
        lines.append(f"# {label} 缠论趋势预测")
    lines.append("")
    # ── 趋势质量评估 ──
    lines.append("## 趋势质量评估")
    lines.append("")
    labels = [l for l, _ in FREQS]
    rows = {"之前趋势": [], "趋势规整度": [], "加速度": [], "力度评估": [], "未完成笔": []}
    for label, _ in FREQS:
        r = results.get(label)
        if r and "error" not in r:
            ubi = r["ubi_info"] if r["ubi_info"] else "无"
            dir_with_power = f"{r['direction']}<br>力度={r['last_bi'].power:.1f}"
            rows["未完成笔"].append(ubi)
            rows["之前趋势"].append(dir_with_power)
            rows["趋势规整度"].append(r["rsq_msg"])
            rows["加速度"].append(r["accel_msg"])
            rows["力度评估"].append(r["power_msg"])
        else:
            err = r.get("error", "数据获取失败") if r else "未知错误"
            rows["未完成笔"].append(f"⚠️ {err}")
            rows["之前趋势"].append("-")
            rows["趋势规整度"].append("-")
            rows["加速度"].append("-")
            rows["力度评估"].append("-")

    # ── 跨周期仓位提醒 ──
    alert_text, _ = _position_alert(results)
    if alert_text:
        # 追加到每个周期的未完成笔末尾（换行显示）
        alert_line = f"<br>{alert_text}"
        rows["未完成笔"] = [v + alert_line for v in rows["未完成笔"]]

    # ── Markdown 表格 ──
    lines.append('<table style="border-collapse:collapse">')
    lines.append('<colgroup><col style="width:8em;white-space:nowrap;text-align:center">')
    lines.append("".join('<col style="text-align:left">' for _ in labels))
    lines.append("</colgroup>")
    lines.append('<tr><th style="text-align:center">指标</th>' + "".join(f"<th>{l}</th>" for l in labels) + "</tr>")
    for indicator, values in rows.items():
        lines.append('<tr><td style="text-align:center">' + indicator + "</td>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>")
    lines.append("</table>")
    lines.append("")

    return "\n".join(lines)


TDX_ZXG_PATH = Path("/home/li/.local/share/tdxcfv/drive_c/tc/T0002/blocknew/zxg.blk")


def _parse_tdx_blk(path):
    """解析通达信 blk 板块文件，返回 symbol 列表（如 600519.SH）

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
    return f"output/czsc-{'_'.join(parts)}.md"


def _sort_key(symbol, all_results, name_map):
    """排序键：上证指数 > 创业板指 > 强分型+买卖点共振 > 加仓提醒 > 偏多 > 多空均衡 > 偏空 > 其他"""
    name = (name_map or {}).get(symbol, "")
    signal = _overall_signal(all_results[symbol])
    alert, _ = _position_alert(all_results[symbol])
    fx_star = _strong_fx_bs_star(all_results[symbol])
    if "上证指数" in name:
        return 0
    if "创业板指" in name:
        return 1
    if "⭐⭐" in fx_star:
        return 2
    if alert and "加仓" in alert:
        return 3
    if "⭐" in fx_star:
        return 4
    if "偏多" in signal:
        return 5
    if "多空均衡" in signal:
        return 6
    if "偏空" in signal:
        return 7
    return 8


def _write_merged_report(symbols, all_results, filename, name_map=None):
    """生成多股票合并报告"""
    symbols = sorted(symbols, key=lambda s: _sort_key(s, all_results, name_map))
    lines = []
    lines.append(f"# 缠论趋势预测报告（{len(symbols)}只股票）")
    lines.append("")

    for symbol in symbols:
        n = (name_map or {}).get(symbol)
        md = format_md(symbol, all_results[symbol], name=n)
        lines.append(md)
        lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="缠论趋势预测")
    parser.add_argument("symbols", nargs="*", help="股票代码，如 600519.SH 999999.SH")
    parser.add_argument("-n", "--workers", type=int, default=16, help="并行线程数（默认 16）")
    args = parser.parse_args()

    # 解析股票列表：命令行参数优先，否则读取TDX自选股
    if args.symbols:
        symbols = args.symbols
        from_zxg = False
    elif TDX_ZXG_PATH.exists():
        symbols = _parse_tdx_blk(TDX_ZXG_PATH)
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

    t_start = time.time()
    print(f"数据范围: {sdt} ~ {edt}")
    print(f"待预测({len(symbols)}): {', '.join(symbols)}")
    print(f"并发数: {args.workers}")
    print("=" * 60)

    # 批量获取股票名称
    print("获取股票名称...")
    devnull = open(os.devnull, "w")
    try:
        name_map = _batch_stock_names(symbols, devnull)
        for s in symbols:
            logger.info(f"{s} → {name_map.get(s, s)}")

        # 预取复权因子（缓存 1 个月；网络获取不到最新因子时用过期缓存兜底，
        # 后续 get_raw_bars 命中缓存跳过网络请求）
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

        # ── 盘面分析 ──
        # 确定输出文件名
        if from_zxg:
            filename = f"output/czsc-zxg-{edt.replace('-', '')}.md"
        elif len(symbols) == 1:
            filename = f"output/czsc-{symbols[0].replace('.', '_')}.md"
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
    finally:
        devnull.close()
        elapsed = time.time() - t_start
        print(f"完成，耗时 {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
