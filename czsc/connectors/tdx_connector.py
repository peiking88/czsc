"""
author: peiking88
create_dt: 2026/5/3
describe: 通达信数据源 (tdxdata)，基于本地 TDX 数据文件，支持复权、增量导入、端点续跑
"""

import json
import os
from datetime import datetime
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
    """将 czsc 风格的标的代码转换为纯数字格式"""
    for suffix in (".SH", ".SZ", ".BJ", ".XSHG", ".XSHE"):
        symbol = symbol.replace(suffix, "")
    return symbol


def _get_market(symbol: str) -> str:
    """从 czsc 风格标的代码中提取 TDX 市场代码"""
    for suffix, market in ((".SH", "sh"), (".SZ", "sz"), (".BJ", "bj"),
                           (".XSHG", "sh"), (".XSHE", "sz")):
        if symbol.endswith(suffix):
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
    from tdxpy.reader import TdxLCMinBarReader, TdxMinBarReader

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
        filepath = os.path.join(tdxdir, "vipdoc", market, "fzline", f"{market}{code}.lc5")
        reader = TdxLCMinBarReader()
    elif method == "minute_1":
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


# ---------------------------------------------------------------------------
# 复权因子缓存
# ---------------------------------------------------------------------------

# 因子缓存新鲜度阈值（天）：超过此天数后重新拉取，检查是否有新的除权因子
_ADJUST_FACTOR_MAX_AGE_DAYS = 1


def _get_factor_cache_path(code: str, adjust_type: str) -> str:
    """复权因子缓存路径"""
    return os.path.join(CACHE_DIR, code, f"adjust_factor_{adjust_type}.parquet")


def _load_factor_cache(code: str, adjust_type: str) -> pd.DataFrame:
    """加载本地缓存的复权因子"""
    path = _get_factor_cache_path(code, adjust_type)
    if os.path.exists(path):
        df = pd.read_parquet(path)
        if not df.empty:
            df = df.copy()
            df.loc[:, "date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
            df = df.sort_index()
            return df
    return pd.DataFrame()


def _save_factor_cache(factor_df: pd.DataFrame, code: str, adjust_type: str) -> None:
    """持久化复权因子到缓存"""
    d = os.path.join(CACHE_DIR, code)
    os.makedirs(d, exist_ok=True)
    path = _get_factor_cache_path(code, adjust_type)
    save_df = factor_df.reset_index()
    save_df.to_parquet(path, index=False)


def _factor_cache_is_fresh(factor_df: pd.DataFrame) -> bool:
    """因子缓存是否仍在新鲜期内"""
    if factor_df.empty:
        return False
    last_date = factor_df.index.max()
    if last_date is None or pd.isna(last_date):
        return False
    age = (pd.Timestamp.now() - pd.to_datetime(last_date)).days
    return age <= _ADJUST_FACTOR_MAX_AGE_DAYS


def _get_adjust_factors(code: str, adjust_type: str, force_refresh: bool = False) -> pd.DataFrame:
    """获取复权因子，优先使用本地缓存，增量拉取新因子

    首次导入：从新浪财经一次性拉取全量历史因子，存入本地缓存。
    后续导入：缓存新鲜期内直接使用缓存；过期后增量拉取，合并新旧因子。

    :param code: 纯数字标的代码
    :param adjust_type: "qfq"/"hfq" 或 "front"/"back"，自动归一化为 tdxdata 格式
    :param force_refresh: 强制重新拉取
    :return: 因子 DataFrame，index 为日期，columns 为 factor
    """
    # 归一化：czsc 前端传入 front/back，tdxdata 内部使用 qfq/hfq
    _ALIAS = {"front": "qfq", "back": "hfq"}
    adjust_type = _ALIAS.get(adjust_type, adjust_type)

    if not force_refresh:
        cached = _load_factor_cache(code, adjust_type)
        if not cached.empty and _factor_cache_is_fresh(cached):
            return cached

    # 拉取最新因子
    try:
        from mootdx.quotes import Quotes
        from tdxdata.sources.adjust import fetch_factor

        quotes_client = Quotes.factory(market="std")
        new_factors = fetch_factor(code, adjust_type, quotes_client)
    except Exception as e:
        logger.warning(f"拉取 {code} {adjust_type} 复权因子失败: {e}")
        cached = _load_factor_cache(code, adjust_type)
        return cached if not cached.empty else pd.DataFrame()

    if new_factors is None or new_factors.empty:
        cached = _load_factor_cache(code, adjust_type)
        return cached if not cached.empty else pd.DataFrame()

    # 合并缓存与新因子（新因子覆盖旧日期的值）
    cached = _load_factor_cache(code, adjust_type)
    if not cached.empty:
        merged = pd.concat([cached, new_factors])
        merged = merged[~merged.index.duplicated(keep="last")]
        merged = merged.sort_index()
    else:
        merged = new_factors

    _save_factor_cache(merged, code, adjust_type)
    return merged


def _apply_adjust(df: pd.DataFrame, code: str, dividend_type: str, force_refresh: bool = False) -> pd.DataFrame:
    """应用复权因子，支持日线和分钟数据

    分钟数据的复权匹配以日期（不含时间）为基准，确保 hfq/qfq 方向均正确。
    对齐 tdxdata.sources.adjust.apply_adjust 的复权逻辑。
    """
    if dividend_type == "none":
        return df

    try:
        from tdxdata.sources.adjust import ADJUST_MAP

        adjust = ADJUST_MAP.get(dividend_type)
        if not adjust:
            return df

        factor_df = _get_adjust_factors(code, adjust, force_refresh=force_refresh)
        if factor_df is None or factor_df.empty:
            return df

        date_col = "date" if "date" in df.columns else "datetime"
        if date_col not in df.columns:
            return df

        df = df.copy()
        df.loc[:, date_col] = pd.to_datetime(df[date_col])
        factor_df = factor_df.copy()
        factor_df.index = pd.to_datetime(factor_df.index)
        factor_df = factor_df.sort_index()

        # pandas 3.x 下不同来源的 datetime64 精度可能不一致（[s] vs [us]），
        # 统一转为 datetime64[us] 避免 merge_asof 报 incompatible merge keys
        common_dtype = "datetime64[us]"
        df[date_col] = df[date_col].astype(common_dtype)
        factor_df.index = factor_df.index.astype(common_dtype)

        # 分钟数据用 date-only（不含时间）做 merge_asof，避免时间分量导致
        # hfq 方向匹配错位：例如 "2025-01-15 09:35:00" > "2025-01-15"，
        # forward 会跳过当日因子匹配到下一个
        df = df.sort_values(date_col).reset_index(drop=True)
        df.loc[:, "_adj_date"] = df[date_col].dt.floor("D")
        direction = "backward" if adjust == "qfq" else "forward"

        merged = pd.merge_asof(
            df,
            factor_df[["factor"]],
            left_on="_adj_date",
            right_index=True,
            direction=direction,
        )

        if "factor" not in merged.columns or merged.empty:
            return df.drop(columns=["_adj_date"], errors="ignore")

        merged.loc[:, "factor"] = merged["factor"].ffill().bfill().fillna(1.0)

        # qfq 归一化：以最新因子为基准缩放，使当前价格反映真实价值
        if adjust == "qfq":
            latest_factor = factor_df["factor"].iloc[-1]
            if latest_factor > 0:
                merged["factor"] = merged["factor"] / latest_factor

        for col in ["open", "high", "low", "close"]:
            if col in merged.columns:
                merged.loc[:, col] = merged[col] * merged["factor"]

        merged = merged.drop(columns=["factor", "_adj_date"], errors="ignore")
        return merged

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


def get_raw_bars(
    symbol: str,
    freq,
    sdt,
    edt,
    fq: str = "后复权",
    raw_bar: bool = True,
    **kwargs,
) -> list[RawBar] | pd.DataFrame:
    """获取 K 线数据，支持增量导入和端点续跑

    从本地 TDX 数据文件读取，自动应用复权，同步状态持久化支持中断续跑。

    :param symbol: 标的代码，如 "600519" 或 "600519.SH"
    :param freq: 周期，Freq 对象或字符串，支持 1分钟/5分钟/15分钟/30分钟/60分钟/日线/周线/月线
    :param sdt: 开始日期，如 "2020-01-01"
    :param edt: 结束日期，如 "2024-12-31"
    :param fq: 复权类型，"前复权"/"后复权"/"不复权"，默认 "后复权"
    :param raw_bar: True 返回 list[RawBar]，False 返回 DataFrame
    :param kwargs:
        - tdxdir: str, 通达信本地数据目录
        - use_cache: bool, 是否优先使用本地缓存，默认 True
    :return: RawBar 对象列表或 DataFrame
    """
    freq = Freq(freq)
    freq_val = freq.value
    use_cache = kwargs.get("use_cache", True)
    sdt_ts = pd.to_datetime(sdt)
    edt_ts = pd.to_datetime(edt)

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
