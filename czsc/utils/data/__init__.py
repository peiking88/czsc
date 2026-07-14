"""
数据处理工具模块

包括缓存、数据客户端、验证器和转换器
"""

# 从 cache 导入缓存相关功能
from .cache import (
    DiskCache,
    clear_cache,
    clear_expired_cache,
    disk_cache,
    empty_cache_path,
    get_dir_size,
    home_path,
)

# 从 client 导入数据客户端
from .client import (
    DataClient,
    get_url_token,
    set_url_token,
)

# 从 converters 导入转换函数
from .converters import (
    convert_dict_to_dataframe,
    ensure_datetime_column,
    flatten_multiindex_columns,
    normalize_symbol,
    pivot_weight_data,
    resample_to_period,
    to_standard_kline_format,
)

# 从 validators 导入验证函数
from .validators import (
    validate_dataframe_columns,
    validate_date_range,
    validate_datetime_index,
    validate_no_duplicates,
    validate_numeric_column,
    validate_weight_data,
)

# 从 tdengine 导入 TDengine 直读工具（替代已移除的 tdx_connector）
from .tdengine import batch_stock_names, get_raw_bars, get_symbols

__all__ = [
    # Cache
    "home_path",
    "get_dir_size",
    "empty_cache_path",
    "DiskCache",
    "disk_cache",
    "clear_cache",
    "clear_expired_cache",
    # Client
    "DataClient",
    "set_url_token",
    "get_url_token",
    # Validators
    "validate_dataframe_columns",
    "validate_datetime_index",
    "validate_numeric_column",
    "validate_date_range",
    "validate_no_duplicates",
    "validate_weight_data",
    # Converters
    "to_standard_kline_format",
    "pivot_weight_data",
    "resample_to_period",
    "normalize_symbol",
    "convert_dict_to_dataframe",
    "ensure_datetime_column",
    "flatten_multiindex_columns",
    # TDengine
    "get_raw_bars",
    "get_symbols",
    "batch_stock_names",
]
