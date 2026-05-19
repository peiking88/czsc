"""
author: peiking88
create_dt: 2026/5/3
describe: 通达信数据源 (tdxdata)，基于本地 TDX 数据文件，支持复权、增量导入、端点续跑
"""

import json
import os
from datetime import datetime, time
from typing import Optional

import pandas as pd
from loguru import logger

import czsc
from czsc import Freq, RawBar

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_TDXDIR = os.path.expanduser("~/.local/share/tdxcfv/drive_c/tc/")
SYNC_STATE_PATH = os.path.expanduser("~/.tdxdata/tdx_connector_state.json")

# czsc 周期 → tdxdata 本地读取周期（本地仅支持 1d/1m/5m，其余需重采样）
FREQ_MAP = {
    "1分钟": "1m",
    "5分钟": "5m",
    "15分钟": "5m",
    "30分钟": "5m",
    "60分钟": "5m",
    "日线": "1d",
    "周线": "1d",
    "月线": "1d",
}

# 需要通过 resample_kline 重采样的周期
RESAMPLE_FREQS = {"15分钟", "30分钟", "60分钟", "周线", "月线"}

# czsc 周期 → tdxdata resample_kline 目标周期字符串
RESAMPLE_TARGET = {
    "15分钟": "15min",
    "30分钟": "30min",
    "60分钟": "1h",
    "周线": "W",
    "月线": "ME",
}

# 复权类型：czsc 中文 → tdxdata 英文
FQ_MAP = {"前复权": "front", "后复权": "back", "不复权": "none"}

# 盘中实时获取参数：czsc 周期 → (fetch_kline period, count)
RT_KLINE_PARAMS = {
    "1分钟": ("1m", 240),
    "5分钟": ("5m", 48),
    "15分钟": ("15m", 16),
    "30分钟": ("30m", 8),
    "60分钟": ("1h", 4),
    "日线": ("1d", 2),
    "周线": ("1w", 2),
    "月线": ("1mon", 2),
}

# 本地读取方法映射
PERIOD_METHOD = {"1d": "daily", "1m": "minute_1", "5m": "minute_5"}

# 列名映射：mootdx Reader 输出 → czsc
COL_MAP = {"vol": "volume"}

# czsc 标准 8 列
CZSC_COLUMNS = ["symbol", "dt", "open", "close", "high", "low", "vol", "amount"]

# 常用标的代码分组
SYMBOL_GROUPS = {
    "check": ["000001", "600519", "000858"],
    "index": [
        "000001", "399001", "399006", "000300", "000905", "000852",
        "399673", "399681", "399106", "399005",
    ],
}


# ---------------------------------------------------------------------------
# 增量状态管理
# ---------------------------------------------------------------------------


class _SyncState:
    """轻量级同步状态，持久化到 JSON 文件"""

    def __init__(self, path: str = SYNC_STATE_PATH):
        self._path = path
        self._state: dict = {}

    def _load(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2, ensure_ascii=False, default=str)

    def get_last_sync(self, key: str) -> Optional[str]:
        if not self._state:
            self._state = self._load()
        return self._state.get(key, {}).get("last_sync")

    def update_sync(self, key: str) -> None:
        if not self._state:
            self._state = self._load()
        self._state[key] = {
            "last_sync": datetime.now().strftime("%Y-%m-%d"),
            "updated_at": datetime.now().isoformat(),
        }
        self._save()


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _normalize_symbol(symbol: str) -> str:
    """将 czsc 风格的标的代码转换为纯数字格式

    支持格式：600519.SH / 600519.sh / SH600519 / sh.600519 / 600519 等
    """
    symbol = symbol.lower()
    # 前缀：sh.600519 / sh600519 / sz.000858 / bj.899050
    for market in ("sh", "sz", "bj"):
        if symbol.startswith(market + "."):
            return symbol[len(market) + 1 :]
        if symbol.startswith(market):
            return symbol[len(market) :]
    # 后缀：600519.sh / 600519.sz / 600519.xshg
    for suffix in (".sh", ".sz", ".bj", ".xshg", ".xshe"):
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _get_market(symbol: str) -> str:
    """从 czsc 风格标的代码中提取 TDX 市场代码"""
    symbol_lower = symbol.lower()
    # 后缀：600519.sh / 000858.sz
    for suffix, market in ((".sh", "sh"), (".sz", "sz"), (".bj", "bj"),
                           (".xshg", "sh"), (".xshe", "sz")):
        if symbol_lower.endswith(suffix):
            return market
    # 前缀：sh600519 / sz000858 / bj.899050（无点）
    for prefix, market in (("sh", "sh"), ("sz", "sz"), ("bj", "bj")):
        if symbol_lower.startswith(prefix) and not symbol_lower.startswith(prefix + "."):
            return market
    return "sh"


def _read_local(symbol: str, period: str, tdxdir: str) -> pd.DataFrame:
    """读取指定市场的 TDX 本地数据文件

    绕过 mootdx find_path 的市场解析（get_stock_market 无法区分同代码不同市场），
    直接构造文件路径并调用底层解析器。

    :param symbol: 原始标的代码（含后缀），如 "600519.SH" / "000001.SH"
    :param period: "1d" / "1m" / "5m"
    :param tdxdir: TDX 本地数据目录
    :return: DataFrame，列包含 stock_code, date, open, high, low, close, volume, amount
    """
    from mootdx.contrib.compat import MooTdxDailyBarReader

    if period not in PERIOD_METHOD:
        raise ValueError(f"本地不支持周期 '{period}'，支持: {sorted(PERIOD_METHOD)}")

    code = _normalize_symbol(symbol)
    market = _get_market(symbol)
    method = PERIOD_METHOD[period]

    # 构造文件路径：vipdoc/{sh|sz}/{lday|fzline|minline}/{market}{code}.{ext}
    if method == "daily":
        filepath = os.path.join(tdxdir, "vipdoc", market, "lday", f"{market}{code}.day")
        reader = MooTdxDailyBarReader()
    elif method == "minute_5":
        from opentdx.reader import TdxLCMinBarReader

        filepath = os.path.join(tdxdir, "vipdoc", market, "fzline", f"{market}{code}.lc5")
        reader = TdxLCMinBarReader()
    elif method == "minute_1":
        from opentdx.reader import TdxMinBarReader

        filepath = os.path.join(tdxdir, "vipdoc", market, "minline", f"{market}{code}.lc1")
        reader = TdxMinBarReader()
    else:
        return pd.DataFrame()

    if not os.path.exists(filepath):
        return pd.DataFrame()

    df = reader.get_df(filepath)
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # 重置日期索引
    if df.index.name in ("date", "datetime"):
        df = df.reset_index()

    # 统一日期列
    if "datetime" in df.columns and "date" not in df.columns:
        df = df.rename(columns={"datetime": "date"})

    if "date" in df.columns:
        df.loc[:, "date"] = pd.to_datetime(df["date"], errors="coerce")

    # 统一成交量列名
    if "vol" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"vol": "volume"})

    # 成交量规范化为整数（TDX .day 文件以 float 存储，含浮点误差）
    if "volume" in df.columns:
        df.loc[:, "volume"] = df["volume"].round(0).astype("int64")

    df.loc[:, "stock_code"] = code

    keep = ["stock_code", "date", "open", "high", "low", "close", "volume", "amount"]
    keep = [c for c in keep if c in df.columns]
    return df[keep]


def _apply_adjust(df: pd.DataFrame, code: str, dividend_type: str, force_refresh: bool = False) -> pd.DataFrame:
    """应用复权因子，委托给 tdxdata 内置的 apply_adjust"""
    if dividend_type == "none":
        return df

    try:
        from mootdx.quotes import Quotes
        from tdxdata.sources.adjust import ADJUST_MAP, apply_adjust

        adjust = ADJUST_MAP.get(dividend_type)
        if not adjust:
            return df

        quotes_client = Quotes.factory(market="std")
        return apply_adjust(df, code, adjust, quotes_client=quotes_client)

    except Exception as e:
        logger.warning(f"复权因子获取失败，使用未复权数据: {e}")

    return df


def _resample_czsc(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """对 czsc 标准格式 DataFrame 进行 pandas 重采样

    :param df: czsc 格式 DataFrame，含 dt, symbol, open, high, low, close, vol, amount
    :param target: pandas 频率字符串，如 "15min" / "1h" / "W" / "ME"
    :return: 重采样后的 DataFrame
    """
    if df.empty:
        return df
    df = df.set_index("dt")
    agg_rules = {"open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum", "amount": "sum"}
    agg_rules = {k: v for k, v in agg_rules.items() if k in df.columns}
    result = df.resample(target).agg(agg_rules)
    result = result.dropna(subset=["open"]).reset_index()
    result.loc[:, "symbol"] = result.get("symbol", df["symbol"].iloc[0] if "symbol" in df.columns else "")
    return result


def _to_czsc_columns(df: pd.DataFrame, original_symbol: str) -> pd.DataFrame:
    """将 tdxdata 风格 DataFrame 转换为 czsc 标准格式"""
    col_map = {"stock_code": "symbol", "date": "dt", "volume": "vol"}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    if "dt" in df.columns:
        df.loc[:, "dt"] = pd.to_datetime(df["dt"])
    if "vol" in df.columns:
        df.loc[:, "vol"] = df["vol"].round(0).astype("int64")
    df.loc[:, "symbol"] = original_symbol
    df = df.sort_values("dt").reset_index(drop=True)
    df = df.drop_duplicates(subset=["dt"], keep="last")
    cols = [c for c in CZSC_COLUMNS if c in df.columns]
    return df[cols]


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def _is_trading_time() -> bool:
    """判断当前是否在 A 股交易时段内"""
    now = datetime.now()
    try:
        from tdxdata.calendar import is_trading_day
    except ImportError:
        pass
    else:
        if not is_trading_day(now.strftime("%Y-%m-%d")):
            return False
    t = now.time()
    return (time(9, 30) <= t <= time(11, 30)) or (time(13, 0) <= t <= time(15, 0))


def _fetch_realtime_kline(code: str, period: str, count: int, dividend_type: str) -> pd.DataFrame:
    """获取盘中实时K线（仅当日数据），返回 tdxdata 标准格式（已复权）

    通过 tdxdata.fetch_kline() 获取完整 OHLCV K线，然后应用复权因子。

    :param code: 纯数字股票代码，如 "600519"
    :param period: fetch_kline 周期，如 "1m"/"5m"/"15m"/"1d"
    :param count: 获取K线数量
    :param dividend_type: 复权类型，"front"/"back"/"none"
    """
    from tdxdata.api import TdxData

    tdx = TdxData()
    df = tdx.fetch_kline(stock_code=code, period=period, count=count)
    tdx.close()

    if df is None or df.empty:
        return pd.DataFrame()

    # 只保留当日数据
    today = pd.Timestamp.now().normalize()
    if "date" in df.columns:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] >= today]

    if df.empty:
        return pd.DataFrame()

    # 应用复权
    if dividend_type != "none":
        df = _apply_adjust(df, code, dividend_type)

    return df


def get_raw_bars(
    symbol: str,
    freq,
    sdt,
    edt,
    fq: str = "后复权",
    raw_bar: bool = True,
    **kwargs,
) -> list[RawBar] | pd.DataFrame:
    """获取 K 线数据

    从本地 TDX 数据文件读取历史数据，支持盘中实时数据补充。

    :param symbol: 标的代码，如 "600519" 或 "600519.SH"
    :param freq: 周期，支持 1分钟/5分钟/15分钟/30分钟/60分钟/日线/周线/月线
    :param sdt: 开始日期
    :param edt: 结束日期
    :param fq: 复权类型，"前复权"/"后复权"/"不复权"
    :param raw_bar: True 返回 list[RawBar]，False 返回 DataFrame
    :param kwargs:
        - tdxdir: str, 通达信本地数据目录
        - use_cache: bool, 是否优先使用本地缓存，默认 True
        - realtime: bool, 是否盘中模式（补充当日实时数据），默认 False
    :return: RawBar 对象列表或 DataFrame
    """
    freq = Freq(freq)
    freq_val = freq.value
    use_cache = kwargs.get("use_cache", True)
    sdt_ts = pd.to_datetime(sdt)
    edt_ts = pd.to_datetime(edt)
    # 日期字符串（如 "2026-05-15"）解析为 00:00:00，补齐到当日末以包含全天数据
    if isinstance(edt, str) and len(edt) <= 10:
        edt_ts = edt_ts + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)

    # 优先从缓存加载
    if use_cache:
        cached = _load_cache(symbol, freq_val)
        if not cached.empty:
            cached.loc[:, "dt"] = pd.to_datetime(cached["dt"])
            cached_in_range = cached[(cached["dt"] >= sdt_ts) & (cached["dt"] <= edt_ts)]
            if not cached_in_range.empty:
                result = cached_in_range.reset_index(drop=True)
                if raw_bar:
                    return czsc.format_standard_kline(result, freq_val)
                return result

    tdx_period = FREQ_MAP.get(freq_val)
    if tdx_period is None:
        raise ValueError(f"不支持的周期: {freq_val}，支持: {list(FREQ_MAP)}")

    dividend_type = FQ_MAP.get(fq)
    if dividend_type is None:
        raise ValueError(f"不支持的复权类型: {fq}，支持: {list(FQ_MAP)}")

    need_resample = freq_val in RESAMPLE_FREQS

    code = _normalize_symbol(symbol)
    tdxdir = kwargs.get("tdxdir", DEFAULT_TDXDIR)

    # 读取本地数据（传原始 symbol 以区分市场）
    df = _read_local(symbol, tdx_period, tdxdir)
    if df.empty:
        return [] if raw_bar else pd.DataFrame(columns=CZSC_COLUMNS)

    # 应用复权
    if dividend_type != "none":
        df = _apply_adjust(df, code, dividend_type)

    # 重采样（15分钟/30分钟/60分钟/周线/月线），在列转换前执行
    if need_resample:
        from tdxdata.sources.base import resample_kline

        target = RESAMPLE_TARGET[freq_val]
        df = resample_kline(df, target)
        if df.empty:
            return [] if raw_bar else pd.DataFrame(columns=CZSC_COLUMNS)

    # 转换为 czsc 标准格式
    df = _to_czsc_columns(df, symbol)

    # ── 盘中实时数据补充（已复权）─────────────────────────────────────
    if kwargs.get("realtime") and _is_trading_time():
        try:
            rt_params = RT_KLINE_PARAMS.get(freq_val)
            if rt_params:
                rt_period, rt_count = rt_params
                raw_rt = _fetch_realtime_kline(code, rt_period, rt_count, dividend_type)
                if not raw_rt.empty:
                    rt_df = _to_czsc_columns(raw_rt, symbol)
                    if not rt_df.empty:
                        df = pd.concat([df, rt_df], ignore_index=True)
                        df = df.drop_duplicates(subset=["dt"], keep="last")
                        df = df.sort_values("dt").reset_index(drop=True)
        except Exception as e:
            logger.warning(f"盘中实时数据获取失败: {e}")

    # 过滤到用户请求的日期范围
    df = df[(df["dt"] >= sdt_ts) & (df["dt"] <= edt_ts)].reset_index(drop=True)

    if df.empty:
        return [] if raw_bar else df

    if raw_bar:
        return czsc.format_standard_kline(df, freq_val)
    return df


def get_symbols(step: str = "check") -> list[str]:
    """获取标的代码列表

    :param step: 分组名称
        'check' - 少量验证用代码
        'index'  - 主要指数
    :return: 标的代码列表
    """
    return SYMBOL_GROUPS.get(step, SYMBOL_GROUPS["check"])


# ---------------------------------------------------------------------------
# 批量同步导入
# ---------------------------------------------------------------------------

# 缓存存储目录
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".czsc", "tdxdata")

# 市场前缀映射
_MARKET_PREFIX = {"sh": ".SH", "sz": ".SZ", "bj": ".BJ"}

# 所有需要产出的周期（按基础周期分组，方便增量更新）
_NATIVE_FREQS = ["5分钟", "日线"]
_RESAMPLE_FREQS_FROM_5M = ["15分钟", "30分钟", "60分钟"]
_RESAMPLE_FREQS_FROM_1D = ["周线", "月线"]
_ALL_OUTPUT_FREQS = ["5分钟", "15分钟", "30分钟", "60分钟", "日线", "周线", "月线"]


def scan_stocks(tdxdir: str = DEFAULT_TDXDIR) -> list[str]:
    """扫描 TDX 本地数据目录，获取所有可用的标的代码

    :param tdxdir: 通达信本地数据目录
    :return: 标的代码列表，格式如 "600519.SH"、"000001.SZ"、"899050.BJ"
    """
    stocks = []
    for market in ("sh", "sz", "bj"):
        lday_dir = os.path.join(tdxdir, "vipdoc", market, "lday")
        if not os.path.isdir(lday_dir):
            continue
        suffix = _MARKET_PREFIX[market]
        for fname in os.listdir(lday_dir):
            if fname.endswith(".day"):
                code = fname[2:8]  # "sh600519.day" → "600519"
                stocks.append(f"{code}{suffix}")
    return sorted(stocks)


# 频次值 → 安全文件名词缀
_FREQ_FILENAME: dict[str, str] = {
    "1分钟": "1m",
    "5分钟": "5m",
    "15分钟": "15m",
    "30分钟": "30m",
    "60分钟": "60m",
    "日线": "1d",
    "周线": "1w",
    "月线": "1M",
}


def _get_cache_path(symbol: str, freq_val: str) -> str:
    """获取缓存文件路径"""
    code = _normalize_symbol(symbol)
    freq_file = _FREQ_FILENAME.get(freq_val, freq_val)
    return os.path.join(CACHE_DIR, code, f"{freq_file}.parquet")


def _load_cache(symbol: str, freq_val: str) -> pd.DataFrame:
    """从缓存加载已同步的数据"""
    path = _get_cache_path(symbol, freq_val)
    if os.path.exists(path):
        return pd.read_parquet(path)
    return pd.DataFrame()


def _save_cache(df: pd.DataFrame, symbol: str, freq_val: str) -> None:
    """将数据存入缓存，自动去重"""
    code = _normalize_symbol(symbol)
    d = os.path.join(CACHE_DIR, code)
    os.makedirs(d, exist_ok=True)
    path = _get_cache_path(symbol, freq_val)
    df = df.drop_duplicates(subset=["dt"], keep="last")
    df = df.sort_values("dt").reset_index(drop=True)
    df.to_parquet(path, index=False)


def _sync_single_freq(
    symbol: str,
    freq_val: str,
    tdxdir: str,
    fq: str = "后复权",
    force_full: bool = False,
) -> pd.DataFrame:
    """同步单个频次的数据，支持增量更新

    :return: 合并去重后的完整 DataFrame
    """
    code = _normalize_symbol(symbol)
    tdx_period = FREQ_MAP[freq_val]
    dividend_type = FQ_MAP[fq]

    # 加载已有缓存
    cached = _load_cache(symbol, freq_val)
    if not cached.empty:
        cached.loc[:, "dt"] = pd.to_datetime(cached["dt"])
        last_dt = cached["dt"].max()
    else:
        last_dt = None

    # 读取本地数据（传原始 symbol 以区分市场）
    df = _read_local(symbol, tdx_period, tdxdir)
    if df.empty:
        return cached if not cached.empty else pd.DataFrame(columns=CZSC_COLUMNS)

    # 应用复权
    if dividend_type != "none":
        df = _apply_adjust(df, code, dividend_type, force_refresh=force_full)

    # 重采样（如果需要），在列转换前执行
    need_resample = freq_val in RESAMPLE_FREQS
    if need_resample:
        from tdxdata.sources.base import resample_kline

        target = RESAMPLE_TARGET[freq_val]
        df = resample_kline(df, target)
        if df.empty:
            return cached if not cached.empty else pd.DataFrame(columns=CZSC_COLUMNS)

    # 转换为 czsc 标准格式
    df = _to_czsc_columns(df, symbol)

    # 增量合并
    if force_full or cached.empty:
        result = df
    else:
        new_data = df[df["dt"] > last_dt] if last_dt is not None else df
        if new_data.empty:
            return cached
        result = pd.concat([cached, new_data], ignore_index=True)

    result = result.drop_duplicates(subset=["dt"], keep="last")
    result = result.sort_values("dt").reset_index(drop=True)
    return result


def sync_bars(
    symbol: str,
    tdxdir: str = DEFAULT_TDXDIR,
    fq: str = "后复权",
    force_full: bool = False,
) -> dict[str, pd.DataFrame]:
    """同步单个标的的全频次数据，按 5分钟/日线 分别增量

    从本地 TDX 文件读取 5分钟和日线数据，应用复权，重采样生成所有周期数据，
    存入本地缓存。支持增量更新和断点续跑。

    :param symbol: 标的代码，如 "600519" 或 "600519.SH"
    :param tdxdir: 通达信本地数据目录
    :param fq: 复权类型，"前复权"/"后复权"/"不复权"
    :param force_full: 是否强制全量同步（忽略已有缓存）
    :return: {freq_val: DataFrame} 字典
    """
    result = {}
    native_freqs = [f for f in _NATIVE_FREQS if f in FREQ_MAP]
    for freq_val in native_freqs:
        tdx_period = FREQ_MAP[freq_val]
        sync_key = f"{_normalize_symbol(symbol)}_{tdx_period}"
        state = _SyncState()
        try:
            logger.info(f"同步 {symbol} {freq_val} ...")
            df = _sync_single_freq(symbol, freq_val, tdxdir, fq, force_full)
            if df.empty:
                logger.warning(f"  {symbol} {freq_val}: 无数据")
                continue
            _save_cache(df, symbol, freq_val)
            result[freq_val] = df
            state.update_sync(sync_key)
            logger.info(f"  {symbol} {freq_val}: {len(df)} 条, "
                        f"{df['dt'].min().strftime('%Y-%m-%d')} ~ {df['dt'].max().strftime('%Y-%m-%d')}")
        except Exception as e:
            logger.error(f"  {symbol} {freq_val} 同步失败: {e}")
            continue

    # 从 5分钟 重采样生成 15/30/60 分钟
    if "5分钟" in result:
        base_df = result["5分钟"]
        for target_freq in _RESAMPLE_FREQS_FROM_5M:
            try:
                target = RESAMPLE_TARGET[target_freq]
                df = _resample_czsc(base_df, target)
                _save_cache(df, symbol, target_freq)
                result[target_freq] = df
                logger.info(f"  {symbol} {target_freq}: 从5分钟重采样, {len(df)} 条")
            except Exception as e:
                logger.error(f"  {symbol} {target_freq} 重采样失败: {e}")

    # 从日线重采样生成周线、月线
    if "日线" in result:
        base_df = result["日线"]
        for target_freq in _RESAMPLE_FREQS_FROM_1D:
            try:
                target = RESAMPLE_TARGET[target_freq]
                df = _resample_czsc(base_df, target)
                _save_cache(df, symbol, target_freq)
                result[target_freq] = df
                logger.info(f"  {symbol} {target_freq}: 从日线重采样, {len(df)} 条")
            except Exception as e:
                logger.error(f"  {symbol} {target_freq} 重采样失败: {e}")

    return result


def sync_all(
    symbols: Optional[list[str]] = None,
    tdxdir: str = DEFAULT_TDXDIR,
    fq: str = "后复权",
    force_full: bool = False,
) -> dict[str, dict[str, pd.DataFrame]]:
    """批量同步所有标的的全频次数据

    端点续跑：已完成的标的会跳过（除非 force_full=True），中断后重新调用
    会从上次断点继续。

    :param symbols: 标的代码列表，None 则自动扫描 TDX 目录下所有标的
    :param tdxdir: 通达信本地数据目录
    :param fq: 复权类型
    :param force_full: 是否强制全量同步
    :return: {symbol: {freq_val: DataFrame}} 嵌套字典
    """
    if symbols is None:
        symbols = scan_stocks(tdxdir)

    all_results: dict[str, dict[str, pd.DataFrame]] = {}
    for i, symbol in enumerate(symbols):
        logger.info(f"[{i+1}/{len(symbols)}] 处理 {symbol} ...")
        try:
            result = sync_bars(symbol, tdxdir, fq, force_full)
            if result:
                all_results[symbol] = result
        except Exception as e:
            logger.error(f"[{i+1}/{len(symbols)}] {symbol} 同步异常: {e}")
            continue

    logger.info(f"批量同步完成: {len(all_results)}/{len(symbols)} 个标的有数据")
    return all_results
