"""
author: peiking88
create_dt: 2026/7/14
describe: TDengine 直读工具函数，供 predict.py 等脚本使用

移除 tdxdata/mootdx/opentdx 后，A 股历史 K 线与复权数据统一从 TDengine 读取。
period 用中文字符串（"1分钟"/"5分钟"/"15分钟"/"30分钟"/"60分钟"/"日线"/"周线"/"月线"），
与 czsc Freq.value 及既有脚本参数保持一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 注意：不要在模块顶层 `from czsc import ...`，否则 czsc/__init__ 加载 utils.data 时
# 会触发循环导入（czsc 尚未初始化完成）。所有符号在函数内按需导入。


# 中文周期 → TDengine 子表后缀（已有表）
_PERIOD_TABLE = {"1分钟": "1m", "5分钟": "5m", "日线": "1d"}

# 需要从更细粒度采样的中文周期 → (源周期, pandas resample 规则)
_PERIOD_RESAMPLE: dict[str, tuple[str, str]] = {
    "15分钟": ("5分钟", "15min"),
    "30分钟": ("5分钟", "30min"),
    "60分钟": ("5分钟", "1h"),
    "周线":   ("日线", "W-FRI"),
    "月线":   ("日线", "ME"),
}

# pandas resample 聚合规则
_OHLCV_AGG = {
    "open": "first", "high": "max", "low": "min", "close": "last",
    "volume": "sum", "amount": "sum",
}

# 中文周期 → Freq 枚举字符串名（延迟到函数内解析，避免模块顶层导入 czsc.Freq 触发循环）
_FREQ_NAME: dict[str, str] = {
    "1分钟": "F1", "5分钟": "F5", "15分钟": "F15",
    "30分钟": "F30", "60分钟": "F60", "日线": "D",
    "周线": "W", "月线": "M",
}


def _strip_suffix(symbol: str) -> str:
    """600519.SH → 600519（TDengine stock_name.code 为纯数字）"""
    return symbol.split(".")[0]


def _market_of(symbol: str) -> str:
    """600519.SH → sh（TDengine 表名前缀用小写市场码；无后缀返回空串）"""
    return symbol.partition(".")[2].lower()


def _td_code(symbol: str) -> str:
    """600519.SH → sh600519（TDengine K线/复权表名前缀：小写市场码 + 6 位代码）"""
    num, _, market = symbol.partition(".")
    return f"{market.lower()}{num}"


def batch_stock_names(symbols: list[str]) -> dict[str, str]:
    """从 TDengine stock_name 表批量获取股票名称，返回 {symbol: name}。

    按 (code, market) 精确匹配（tdx-cpp v0.13.7 起 stock_name 含 market 列），
    避免同 code 异市名字互相覆盖；无市场后缀回退到任意一行。
    连接失败时返回空字典，由调用方决定展示逻辑。
    """
    from taosws import connect

    name_map: dict[str, str] = {}
    raw_codes = list({_strip_suffix(s) for s in symbols})

    try:
        conn = connect()
    except Exception:
        print("[WARN] TDengine 连接失败，跳过股票名称查询")
        return name_map

    try:
        placeholders = ",".join(f"'{c}'" for c in raw_codes)
        r = conn.query(
            f"select code, name, market from tdx.stock_name "
            f"where code in ({placeholders})"
        )
        precise: dict[tuple[str, str], str] = {}
        by_code: dict[str, str] = {}
        for row in r:
            code, name, market = row[0], row[1], row[2]
            precise[(code, market)] = name
            by_code.setdefault(code, name)
    finally:
        conn.close()

    for symbol in symbols:
        raw = _strip_suffix(symbol)
        name = precise.get((raw, _market_of(symbol))) or by_code.get(raw)
        if name:
            name_map[symbol] = name
    return name_map


def _query_kline(conn, code: str, period: str, sdt: str, edt: str) -> pd.DataFrame | None:
    """从 TDengine 查询单周期 K 线，返回 DataFrame（index=ts）；失败/无数据返回 None"""
    try:
        r = conn.query(
            f"select ts, open, high, low, close, volume, amount "
            f"from tdx.k_{code}_{period} "
            f"where ts >= '{sdt}' and ts <= '{edt} 23:59:59' order by ts"
        )
        rows = list(r)
    except Exception:
        return None

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "amount"])
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    df = df.set_index("ts").sort_index()
    return df.astype({c: np.float64 for c in ["open", "high", "low", "close", "volume", "amount"]})


def _fetch_adjust_events(conn, code: str) -> list[dict]:
    """从 TDengine 读取复权事件列表；失败返回空列表"""
    events: list[dict] = []
    try:
        r = conn.query(f"select ts, fenhong, peigujia, songzhuangu, peigu from tdx.a_{code} order by ts")
        for row in r:
            ts, fh, pj, sz, pg = row
            fh, pj, sz, pg = float(fh), float(pj), float(sz), float(pg)
            if fh > 0 or sz > 0 or pg > 0:
                events.append({
                    "date": pd.Timestamp(ts).tz_localize(None),
                    "fenhong": fh, "peigujia": pj, "songzhuangu": sz, "peigu": pg,
                })
    except Exception:
        pass
    return events


def _compute_adjust_factor(df: pd.DataFrame, events: list[dict]) -> np.ndarray:
    """计算后复权因子（从最新日向历史日累积）。

    乘数公式（TDX 标准）:
      D = fenhong/10, S = songzhuangu/10, P = peigu/10, Pp = peigujia
      multiplier = C_before * (1+S+P) / (C_before - D + P*Pp)
    """
    n = len(df)
    factor = np.ones(n, dtype=np.float64)
    if not events:
        return factor

    events_sorted = sorted(events, key=lambda e: e["date"])
    df_dates = df.index.values
    raw_close = df["close"].values

    for evt in events_sorted:
        evt_date = np.datetime64(evt["date"])
        event_idx = int(np.searchsorted(df_dates, evt_date))
        if event_idx >= n:
            continue
        prev_idx = event_idx - 1
        if prev_idx < 0:
            continue
        C_before = raw_close[prev_idx]
        if C_before <= 0:
            continue

        D = evt["fenhong"] / 10.0
        S = evt["songzhuangu"] / 10.0
        P = evt["peigu"] / 10.0
        Pp = evt["peigujia"]

        denominator = C_before - D + P * Pp
        if denominator <= 0:
            continue
        numerator = C_before * (1.0 + S + P)
        multiplier = numerator / denominator

        if abs(multiplier - 1.0) < 1e-12:
            continue
        factor[:event_idx] *= multiplier

    return factor


def _apply_adjust(df: pd.DataFrame, events: list[dict], mode: str) -> pd.DataFrame:
    """应用复权因子到 OHLC。

    :param mode: "后复权" 当前价格即市场价，历史按分红放大；
                 "前复权" 最早一期价格不变，后续按分红缩小（与旧 tdx_connector FQ_MAP 语义一致）
    """
    if mode not in ("前复权", "后复权") or not events:
        return df
    df = df.copy()
    factor = _compute_adjust_factor(df, events)  # shape [n], 后复权乘子
    if mode == "后复权":
        adj = factor
    else:
        # 前复权：用最新一个因子做归一化基准，使最早一期 = 1.0
        latest = factor[-1] if factor[-1] > 0 else 1.0
        adj = latest / factor
        adj = np.where(factor > 0, adj, 1.0)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col] * adj
    return df


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame | None:
    """pandas OHLCV resample 到更高周期；列不齐全或空返回 None"""
    ohlc_cols = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in ohlc_cols):
        return None
    resampled = df.resample(rule).agg(_OHLCV_AGG)
    resampled = resampled.dropna(subset=ohlc_cols, how="all")
    return resampled if not resampled.empty else None


def get_raw_bars(
    symbol: str,
    period: str,
    sdt: str,
    edt: str,
    fq: str = "后复权",
) -> list:
    """从 TDengine 读取 K 线，返回 czsc RawBar 列表。

    :param symbol: 标的代码，如 "600519.SH"
    :param period: 中文周期字符串（"1分钟".."月线"）
    :param sdt: 开始日期 "YYYY-MM-DD"
    :param edt: 结束日期 "YYYY-MM-DD"
    :param fq: 复权类型，"前复权"/"后复权"/"不复权"（默认后复权，与原 tdx_connector.get_raw_bars 一致）
    """
    from taosws import connect

    code = _td_code(symbol)

    conn = connect()
    try:
        if period in _PERIOD_TABLE:
            df = _query_kline(conn, code, _PERIOD_TABLE[period], sdt, edt)
        elif period in _PERIOD_RESAMPLE:
            src_period, rule = _PERIOD_RESAMPLE[period]
            df = _query_kline(conn, code, _PERIOD_TABLE[src_period], sdt, edt)
            if df is not None:
                df = _resample_ohlcv(df, rule)
        else:
            return []

        if df is None or df.empty:
            return []

        if fq in ("前复权", "后复权"):
            events = _fetch_adjust_events(conn, code)
            df = _apply_adjust(df, events, fq)
    finally:
        conn.close()

    from czsc import Freq, format_standard_kline

    freq_enum = getattr(Freq, _FREQ_NAME.get(period, "D"))
    df = df.reset_index().rename(columns={"ts": "dt", "volume": "vol"})
    df["symbol"] = symbol
    return format_standard_kline(
        df[["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]],
        freq=freq_enum,
    )


def get_symbols(step: str = "check") -> list[str]:
    """获取常用标的代码列表（替代 tdx_connector.get_symbols）。

    :param step: "check" - 少量验证用代码；"index" - 主要指数
    """
    groups = {
        "check": ["000001", "600519", "000858"],
        "index": [
            "000001", "399001", "399006", "000300", "000905",
            "000852", "399673", "399681", "399106", "399005",
        ],
    }
    return groups.get(step, groups["check"])
