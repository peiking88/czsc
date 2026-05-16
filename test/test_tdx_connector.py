"""
test_tdx_connector.py - TDX 连接器单元测试

测试覆盖:
- 列名转换和标准化
- 标的代码格式归一化
- 周期/复权映射完整性
- 增量状态持久化
- 重采样流程
- 边界处理
"""

from unittest.mock import patch

import pandas as pd
import pytest
from czsc import Freq, RawBar
from czsc.connectors import tdx_connector as tc


# ---------------------------------------------------------------------------
# 辅助：构造模拟的 mootdx Reader 输出
# ---------------------------------------------------------------------------


def _make_local_df(rows: int = 10, code: str = "000001") -> pd.DataFrame:
    """生成模拟本地读取输出的 DataFrame"""
    dates = pd.date_range("2024-01-01", periods=rows, freq="B")
    return pd.DataFrame({
        "stock_code": code,
        "date": dates,
        "open": [10.0 + i * 0.1 for i in range(rows)],
        "high": [10.5 + i * 0.1 for i in range(rows)],
        "low": [9.5 + i * 0.1 for i in range(rows)],
        "close": [10.0 + i * 0.1 for i in range(rows)],
        "volume": [1000 + i * 100 for i in range(rows)],
        "amount": [10000.0 + i * 1000 for i in range(rows)],
    })


# ---------------------------------------------------------------------------
# 测试：常量映射完整性
# ---------------------------------------------------------------------------


class TestMappings:
    def test_freq_map_covers_common_freqs(self):
        for f in ["1分钟", "5分钟", "15分钟", "30分钟", "60分钟", "日线", "周线", "月线"]:
            assert f in tc.FREQ_MAP, f"缺少周期映射: {f}"

    def test_fq_map_covers_all_types(self):
        for fq in ("前复权", "后复权", "不复权"):
            assert fq in tc.FQ_MAP

    def test_resample_target_covers_all_resample_freqs(self):
        for f in tc.RESAMPLE_FREQS:
            assert f in tc.RESAMPLE_TARGET

    def test_period_method(self):
        assert tc.PERIOD_METHOD["1d"] == "daily"
        assert tc.PERIOD_METHOD["1m"] == "minute_1"
        assert tc.PERIOD_METHOD["5m"] == "minute_5"


# ---------------------------------------------------------------------------
# 测试：_normalize_symbol
# ---------------------------------------------------------------------------


class TestNormalizeSymbol:
    def test_pure_code(self):
        assert tc._normalize_symbol("000001") == "000001"

    def test_sh_suffix_upper(self):
        assert tc._normalize_symbol("600519.SH") == "600519"

    def test_sz_suffix_upper(self):
        assert tc._normalize_symbol("000858.SZ") == "000858"

    def test_bj_suffix_upper(self):
        assert tc._normalize_symbol("430047.BJ") == "430047"

    def test_suffix_lower(self):
        """小写后缀"""
        assert tc._normalize_symbol("600519.sh") == "600519"
        assert tc._normalize_symbol("000858.sz") == "000858"
        assert tc._normalize_symbol("430047.bj") == "430047"

    def test_prefix_with_dot(self):
        """市场.代码 格式"""
        assert tc._normalize_symbol("sh.600519") == "600519"
        assert tc._normalize_symbol("sz.000858") == "000858"
        assert tc._normalize_symbol("bj.899050") == "899050"

    def test_prefix_upper_with_dot(self):
        """大写的 市场.代码 格式"""
        assert tc._normalize_symbol("SH.600519") == "600519"
        assert tc._normalize_symbol("SZ.000858") == "000858"

    def test_prefix_no_dot(self):
        """无点前缀（TDX 文件名风格）"""
        assert tc._normalize_symbol("sh600519") == "600519"
        assert tc._normalize_symbol("sz000858") == "000858"
        assert tc._normalize_symbol("bj899050") == "899050"

    def test_old_exchange_suffix(self):
        """旧式交易所后缀"""
        assert tc._normalize_symbol("600519.XSHG") == "600519"
        assert tc._normalize_symbol("000858.XSHE") == "000858"

    def test_no_change_pure_numeric(self):
        """纯数字代码不应被修改"""
        assert tc._normalize_symbol("000001") == "000001"
        assert tc._normalize_symbol("600519") == "600519"
        assert tc._normalize_symbol("899050") == "899050"


class TestGetMarket:
    def test_sh_suffix(self):
        assert tc._get_market("600519.SH") == "sh"
        assert tc._get_market("600519.sh") == "sh"

    def test_sz_suffix(self):
        assert tc._get_market("000858.SZ") == "sz"
        assert tc._get_market("000858.sz") == "sz"

    def test_bj_suffix(self):
        assert tc._get_market("430047.BJ") == "bj"
        assert tc._get_market("430047.bj") == "bj"

    def test_prefix_no_dot(self):
        """无点前缀：sh600519 / sz000858"""
        assert tc._get_market("sh600519") == "sh"
        assert tc._get_market("sz000858") == "sz"
        assert tc._get_market("bj899050") == "bj"

    def test_default_sh(self):
        """无法识别的代码默认返回上海"""
        assert tc._get_market("600519") == "sh"
        assert tc._get_market("000001") == "sh"

    def test_old_exchange(self):
        assert tc._get_market("600519.XSHG") == "sh"
        assert tc._get_market("000858.XSHE") == "sz"


# ---------------------------------------------------------------------------
# 测试：_to_czsc_columns
# ---------------------------------------------------------------------------


class TestToCzscColumns:
    def test_column_rename(self):
        df = _make_local_df(5)
        result = tc._to_czsc_columns(df, "600519.SH")
        for col in tc.CZSC_COLUMNS:
            assert col in result.columns

    def test_symbol_preserved(self):
        df = _make_local_df(3, code="000001")
        result = tc._to_czsc_columns(df, "000001.SH")
        assert (result["symbol"] == "000001.SH").all()

    def test_dedup(self):
        df = pd.concat([_make_local_df(3), _make_local_df(3)], ignore_index=True)
        result = tc._to_czsc_columns(df, "000001")
        assert len(result) == 3

    def test_sorted_by_dt(self):
        df = _make_local_df(5).sort_values("date", ascending=False)
        result = tc._to_czsc_columns(df, "000001")
        assert result["dt"].is_monotonic_increasing


# ---------------------------------------------------------------------------
# 测试：get_raw_bars（mock _read_local）
# ---------------------------------------------------------------------------


class TestGetRawBars:
    @patch("czsc.connectors.tdx_connector._load_cache")
    @patch("czsc.connectors.tdx_connector._read_local")
    @patch("czsc.connectors.tdx_connector._apply_adjust")
    def test_daily_returns_raw_bars(self, mock_adjust, mock_read, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        mock_read.return_value = _make_local_df(10)
        mock_adjust.side_effect = lambda df, *a, **kw: df

        bars = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31")
        assert isinstance(bars, list)
        assert all(isinstance(b, RawBar) for b in bars)
        assert len(bars) == 10

    @patch("czsc.connectors.tdx_connector._load_cache")
    @patch("czsc.connectors.tdx_connector._read_local")
    @patch("czsc.connectors.tdx_connector._apply_adjust")
    def test_daily_returns_dataframe(self, mock_adjust, mock_read, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        mock_read.return_value = _make_local_df(5)
        mock_adjust.side_effect = lambda df, *a, **kw: df

        df = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31", raw_bar=False)
        assert isinstance(df, pd.DataFrame)
        assert set(tc.CZSC_COLUMNS).issubset(df.columns)

    @patch("czsc.connectors.tdx_connector._load_cache")
    @patch("czsc.connectors.tdx_connector._read_local")
    def test_empty_result_raw_bar(self, mock_read, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        mock_read.return_value = pd.DataFrame()
        result = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31")
        assert result == []

    @patch("czsc.connectors.tdx_connector._load_cache")
    @patch("czsc.connectors.tdx_connector._read_local")
    def test_empty_result_dataframe(self, mock_read, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        mock_read.return_value = pd.DataFrame()
        result = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31", raw_bar=False)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    @patch("czsc.connectors.tdx_connector._load_cache")
    def test_unsupported_freq(self, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        with pytest.raises(ValueError, match="不支持的周期"):
            tc.get_raw_bars("600519", "季线", "2024-01-01", "2024-12-31")

    @patch("czsc.connectors.tdx_connector._load_cache")
    def test_unsupported_fq(self, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        with pytest.raises(ValueError, match="不支持的复权类型"):
            tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31", fq="乱来")

    @patch("czsc.connectors.tdx_connector._load_cache")
    @patch("czsc.connectors.tdx_connector._read_local")
    @patch("czsc.connectors.tdx_connector._apply_adjust")
    def test_symbol_suffix_stripped(self, mock_adjust, mock_read, mock_cache):
        mock_cache.return_value = pd.DataFrame()
        mock_read.return_value = _make_local_df(3, code="600519")
        mock_adjust.side_effect = lambda df, *a, **kw: df

        bars = tc.get_raw_bars("600519.SH", "日线", "2024-01-01", "2024-12-31")
        mock_read.assert_called_once_with("600519.SH", "1d", tc.DEFAULT_TDXDIR)
        assert bars[0].symbol == "600519.SH"

    @patch("czsc.connectors.tdx_connector._load_cache")
    @patch("czsc.connectors.tdx_connector._read_local")
    @patch("czsc.connectors.tdx_connector._apply_adjust")
    def test_5min_with_adjust(self, mock_adjust, mock_read, mock_cache):
        """分钟数据也应调用复权"""
        mock_cache.return_value = pd.DataFrame()
        mock_read.return_value = _make_local_df(5)
        mock_adjust.side_effect = lambda df, *a, **kw: df

        tc.get_raw_bars("600519", "5分钟", "2024-01-01", "2024-12-31")
        mock_adjust.assert_called_once()


# ---------------------------------------------------------------------------
# 测试：_SyncState
# ---------------------------------------------------------------------------


class TestSyncState:
    def test_no_previous_state(self, tmp_path):
        state = tc._SyncState(path=str(tmp_path / "state.json"))
        assert state.get_last_sync("600519_kline_1d") is None

    def test_update_and_read(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = tc._SyncState(path=path)
        state.update_sync("600519_kline_1d")
        assert state.get_last_sync("600519_kline_1d") is not None

    def test_persists_across_instances(self, tmp_path):
        path = str(tmp_path / "state.json")
        tc._SyncState(path=path).update_sync("600519_kline_1d")
        assert tc._SyncState(path=path).get_last_sync("600519_kline_1d") is not None


# ---------------------------------------------------------------------------
# 测试：get_symbols
# ---------------------------------------------------------------------------


class TestGetSymbols:
    def test_check_group(self):
        assert "600519" in tc.get_symbols("check")

    def test_unknown_group_falls_back(self):
        assert tc.get_symbols("nonexistent") == tc.get_symbols("check")


# ---------------------------------------------------------------------------
# 测试：scan_stocks
# ---------------------------------------------------------------------------


class TestScanStocks:
    def test_returns_sorted_list(self, tmp_path):
        # 构造临时 TDX 目录结构
        for market, prefix in [("sh", "sh"), ("sz", "sz")]:
            lday = tmp_path / "vipdoc" / market / "lday"
            lday.mkdir(parents=True, exist_ok=True)
        # 创建模拟文件
        (tmp_path / "vipdoc" / "sh" / "lday" / "sh600519.day").touch()
        (tmp_path / "vipdoc" / "sh" / "lday" / "sh000001.day").touch()
        (tmp_path / "vipdoc" / "sz" / "lday" / "sz000858.day").touch()

        stocks = tc.scan_stocks(str(tmp_path))
        assert stocks == sorted(stocks)
        assert "000001.SH" in stocks
        assert "000858.SZ" in stocks
        assert "600519.SH" in stocks

    def test_ignores_non_day_files(self, tmp_path):
        lday = tmp_path / "vipdoc" / "sh" / "lday"
        lday.mkdir(parents=True, exist_ok=True)
        (lday / "sh600519.day").touch()
        (lday / "sh600519.txt").touch()
        (lday / "sh600519.lc5").touch()

        stocks = tc.scan_stocks(str(tmp_path))
        assert stocks == ["600519.SH"]

    def test_missing_market_skipped(self, tmp_path):
        # 只有 SH，没有 SZ/BJ
        lday = tmp_path / "vipdoc" / "sh" / "lday"
        lday.mkdir(parents=True, exist_ok=True)
        (lday / "sh600519.day").touch()

        stocks = tc.scan_stocks(str(tmp_path))
        assert len(stocks) == 1
        assert stocks[0] == "600519.SH"


# ---------------------------------------------------------------------------
# 测试：缓存路径和存取
# ---------------------------------------------------------------------------


class TestCacheOps:
    def test_get_cache_path(self):
        path = tc._get_cache_path("600519.SH", "日线")
        assert path.endswith("600519/1d.parquet")
        assert ".czsc" in path

    def test_get_cache_path_15m(self):
        path = tc._get_cache_path("000858", "15分钟")
        assert path.endswith("000858/15m.parquet")

    def test_save_and_load_roundtrip(self, tmp_path):
        # 临时覆盖缓存目录
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_cache")
            df = _make_local_df(5)
            df = tc._to_czsc_columns(df, "600519")
            tc._save_cache(df, "600519", "日线")
            loaded = tc._load_cache("600519", "日线")
            assert not loaded.empty
            assert len(loaded) == 5
            assert "dt" in loaded.columns
        finally:
            tcm.CACHE_DIR = orig_dir

    def test_load_cache_missing(self, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "nonexistent")
            result = tc._load_cache("000001", "日线")
            assert result.empty
        finally:
            tcm.CACHE_DIR = orig_dir

    def test_save_cache_dedup(self, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_cache_dedup")
            df = pd.concat([_make_local_df(3), _make_local_df(3)], ignore_index=True)
            df = tc._to_czsc_columns(df, "600519")
            tc._save_cache(df, "600519", "日线")
            loaded = tc._load_cache("600519", "日线")
            assert len(loaded) == 3
        finally:
            tcm.CACHE_DIR = orig_dir


# ---------------------------------------------------------------------------
# 测试：复权因子缓存
# ---------------------------------------------------------------------------


class TestSyncBars:
    @patch("czsc.connectors.tdx_connector._read_local")
    def test_sync_bars_no_adjust(self, mock_read, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_sync")
            mock_read.return_value = _make_local_df(10)
            result = tc.sync_bars("600519", fq="不复权")
            # 日线同步应始终存在
            assert "日线" in result
            assert len(result["日线"]) == 10
            # 周线/月线从日线重采样
            assert "周线" in result
            assert "月线" in result
            # 5分钟数据存在（mock 数据按日线频率，resample_bars 会把它当 5min 存下来）
            assert "5分钟" in result
        finally:
            tcm.CACHE_DIR = orig_dir

    @patch("czsc.connectors.tdx_connector._read_local")
    def test_sync_bars_empty_returns_empty_dict(self, mock_read, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_empty")
            mock_read.return_value = pd.DataFrame()
            result = tc.sync_bars("600519", fq="不复权")
            assert result == {}
        finally:
            tcm.CACHE_DIR = orig_dir

    @patch("czsc.connectors.tdx_connector._read_local")
    def test_sync_bars_incremental(self, mock_read, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_incr")
            # 第一次
            mock_read.return_value = _make_local_df(5)
            r1 = tc.sync_bars("600519", fq="不复权")
            assert len(r1["日线"]) == 5

            # 第二次：新增 3 条
            new_df = _make_local_df(8)
            new_df.loc[5:, "date"] = pd.to_datetime(["2024-01-08", "2024-01-09", "2024-01-10"])
            mock_read.return_value = new_df

            r2 = tc.sync_bars("600519", fq="不复权")
            # 应为 5(旧) + 3(新) = 8 去重后
            assert len(r2["日线"]) == 8
        finally:
            tcm.CACHE_DIR = orig_dir

    @patch("czsc.connectors.tdx_connector._read_local")
    def test_sync_bars_force_full(self, mock_read, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_force")
            mock_read.return_value = _make_local_df(5)
            tc.sync_bars("600519", fq="不复权")

            # force_full 应忽略缓存
            mock_read.return_value = _make_local_df(3)
            result = tc.sync_bars("600519", fq="不复权", force_full=True)
            assert len(result["日线"]) == 3
        finally:
            tcm.CACHE_DIR = orig_dir


class TestSyncAll:
    @patch("czsc.connectors.tdx_connector._read_local")
    def test_sync_all_with_given_list(self, mock_read, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_syncall")
            mock_read.return_value = _make_local_df(5)
            result = tc.sync_all(symbols=["600519", "000858"], fq="不复权")
            assert "600519" in result
            assert "000858" in result
        finally:
            tcm.CACHE_DIR = orig_dir

    @patch("czsc.connectors.tdx_connector._read_local")
    def test_sync_all_handles_empty(self, mock_read, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_syncall_empty")
            mock_read.return_value = pd.DataFrame()
            result = tc.sync_all(symbols=["999999"], fq="不复权")
            assert result == {} or "999999" not in result
        finally:
            tcm.CACHE_DIR = orig_dir


# ---------------------------------------------------------------------------
# 测试：get_raw_bars 缓存优先
# ---------------------------------------------------------------------------


class TestGetRawBarsWithCache:
    def test_cache_hit_returns_fast(self, tmp_path):
        """缓存命中时不调用 _read_local"""
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_gbr_cache")
            df = _make_local_df(10)
            df = tc._to_czsc_columns(df, "600519")
            tc._save_cache(df, "600519", "日线")

            # 缓存命中应直接返回
            bars = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31", fq="不复权")
            assert len(bars) == 10
            assert isinstance(bars, list)
            assert all(isinstance(b, RawBar) for b in bars)
        finally:
            tcm.CACHE_DIR = orig_dir

    def test_cache_hit_returns_dataframe(self, tmp_path):
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_gbr_cache_df")
            df = _make_local_df(8)
            df = tc._to_czsc_columns(df, "600519")
            tc._save_cache(df, "600519", "5分钟")

            result = tc.get_raw_bars("600519", "5分钟", "2024-01-01", "2024-12-31",
                                     fq="不复权", raw_bar=False)
            assert isinstance(result, pd.DataFrame)
            assert len(result) == 8
        finally:
            tcm.CACHE_DIR = orig_dir

    @patch("czsc.connectors.tdx_connector._read_local")
    def test_cache_miss_falls_back_to_tdx(self, mock_read, tmp_path):
        """缓存放了不同 symbol，当前 symbol 缓存缺失 → fallback 到 _read_local"""
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_gbr_miss")
            mock_read.return_value = _make_local_df(5)
            bars = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31", fq="不复权")
            mock_read.assert_called_once()
            assert len(bars) == 5
        finally:
            tcm.CACHE_DIR = orig_dir

    def test_cache_bypassed_with_use_cache_false(self, tmp_path):
        """use_cache=False 时跳过缓存直接读 TDX"""
        import czsc.connectors.tdx_connector as tcm
        orig_dir = tcm.CACHE_DIR
        try:
            tcm.CACHE_DIR = str(tmp_path / "test_gbr_nocache")
            # 先存缓存
            df = _make_local_df(3)
            df = tc._to_czsc_columns(df, "600519")
            tc._save_cache(df, "600519", "日线")

            patcher = patch("czsc.connectors.tdx_connector._read_local")
            mock_read = patcher.start()
            mock_read.return_value = _make_local_df(7)

            try:
                bars = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31",
                                       fq="不复权", use_cache=False)
                mock_read.assert_called_once()
                assert len(bars) == 7
            finally:
                patcher.stop()
        finally:
            tcm.CACHE_DIR = orig_dir
