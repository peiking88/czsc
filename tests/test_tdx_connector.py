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

import os
import time
from unittest.mock import patch

import pandas as pd
import pytest
from czsc import Freq, RawBar
from czsc.connectors import tdx_connector as tc


# ---------------------------------------------------------------------------
# 辅助：构造模拟的复权因子 DataFrame（与 _save_factor_cache / _apply_factor 约定一致）
# ---------------------------------------------------------------------------


def _make_factor_df(code: str = "600519") -> pd.DataFrame:
    """生成模拟复权因子，date 为索引、含 factor 列

    构造早期 factor=2.0、近期 factor=1.0 的前复权因子，便于在测试中
    通过价格是否被放大来区分「已复权」与「未复权」。
    """
    dates = pd.to_datetime(["2024-01-01", "2024-06-03"])
    return pd.DataFrame({"date": dates, "factor": [2.0, 1.0]}).set_index("date")


# ---------------------------------------------------------------------------
# 辅助：构造模拟的 mootdx Reader 输出
# ---------------------------------------------------------------------------


def _make_local_df(rows: int = 10, code: str = "000001") -> pd.DataFrame:
    """生成模拟本地读取输出的 DataFrame"""
    dates = pd.date_range("2024-01-01", periods=rows, freq="B")
    return pd.DataFrame(
        {
            "stock_code": code,
            "date": dates,
            "open": [10.0 + i * 0.1 for i in range(rows)],
            "high": [10.5 + i * 0.1 for i in range(rows)],
            "low": [9.5 + i * 0.1 for i in range(rows)],
            "close": [10.0 + i * 0.1 for i in range(rows)],
            "volume": [1000 + i * 100 for i in range(rows)],
            "amount": [10000.0 + i * 1000 for i in range(rows)],
        }
    )


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

            result = tc.get_raw_bars("600519", "5分钟", "2024-01-01", "2024-12-31", fq="不复权", raw_bar=False)
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
                bars = tc.get_raw_bars("600519", "日线", "2024-01-01", "2024-12-31", fq="不复权", use_cache=False)
                mock_read.assert_called_once()
                assert len(bars) == 7
            finally:
                patcher.stop()
        finally:
            tcm.CACHE_DIR = orig_dir


# ---------------------------------------------------------------------------
# 测试：复权因子缓存期 + 过期缓存兜底
# ---------------------------------------------------------------------------


class TestFactorCacheTTL:
    def test_ttl_is_one_month(self):
        """复权因子缓存有效期默认为 30 天（1 个月）"""
        assert tc._FACTOR_CACHE_TTL_HOURS == 24 * 30

    def test_load_factor_cache_hit_within_ttl(self, tmp_path):
        """TTL 内的缓存可正常读取"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            tc._save_factor_cache("600519", "qfq", _make_factor_df())
            loaded = tc._load_factor_cache("600519", "qfq")
            assert loaded is not None
            assert not loaded.empty
            assert "factor" in loaded.columns
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir

    def test_load_factor_cache_expired_returns_none(self, tmp_path):
        """缓存文件 mtime 超过 TTL 时默认返回 None"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            tc._save_factor_cache("600519", "qfq", _make_factor_df())
            cache_path = os.path.join(tcm.FACTOR_CACHE_DIR, "600519_qfq.parquet")
            # 把 mtime 改到 31 天前，使其按 30 天 TTL 判定为过期
            old_ts = time.time() - 31 * 24 * 3600
            os.utime(cache_path, (old_ts, old_ts))
            assert tc._load_factor_cache("600519", "qfq", max_age_hours=24 * 30) is None
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir

    def test_load_factor_cache_allow_expired(self, tmp_path):
        """allow_expired=True 时即使过期也返回缓存（供兜底使用）"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            tc._save_factor_cache("600519", "qfq", _make_factor_df())
            cache_path = os.path.join(tcm.FACTOR_CACHE_DIR, "600519_qfq.parquet")
            old_ts = time.time() - 31 * 24 * 3600
            os.utime(cache_path, (old_ts, old_ts))
            # 已过期（默认 max_age 会判 None），但允许过期 → 仍返回缓存
            assert tc._load_factor_cache("600519", "qfq") is None
            loaded = tc._load_factor_cache("600519", "qfq", allow_expired=True)
            assert loaded is not None
            assert not loaded.empty
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir

    def test_load_factor_cache_missing(self, tmp_path):
        """无缓存文件返回 None"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            assert tc._load_factor_cache("600519", "qfq") is None
            assert tc._load_factor_cache("600519", "qfq", allow_expired=True) is None
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir


class TestApplyAdjustFallback:
    """网络获取失败时用过期缓存兜底，避免退化为未复权"""

    @patch("mootdx.quotes.Quotes")
    def test_expired_cache_plus_network_failure_uses_fallback(self, mock_quotes_cls, tmp_path):
        """缓存过期 + 网络失败 → 用过期缓存兜底，结果为已复权（非原始价）"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            # 写入一份"过期"缓存：max_age=0 即过期
            tc._save_factor_cache("600519", "qfq", _make_factor_df())

            raw = _make_local_df(5, code="600519")
            raw_close = float(raw["close"].iloc[0])

            # 网络层：Quotes.factory 返回 mock；fetch_factor 抛异常模拟获取失败
            mock_quotes_cls.factory.return_value = mock_quotes_cls.return_value
            with patch("tdxdata.sources.adjust.fetch_factor", side_effect=RuntimeError("network down")):
                # force_refresh=True 才会跳过 TTL 缓存、强制走网络→失败→兜底
                result = tc._apply_adjust(raw, "600519", "front", force_refresh=True)

            # 兜底命中过期缓存：早期日期 close 应被 factor=2.0 放大（qfq 以最新 factor 归一，
            # 此处 _make_factor_df 最新=1.0、早期=2.0），不等于原始价
            assert result["close"].iloc[0] != pytest.approx(raw_close)
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir

    @patch("mootdx.quotes.Quotes")
    def test_no_cache_plus_network_failure_falls_back_unadjusted(self, mock_quotes_cls, tmp_path):
        """无任何缓存 + 网络失败 → 退化为未复权（原值返回）"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            raw = _make_local_df(5, code="600519")

            mock_quotes_cls.factory.return_value = mock_quotes_cls.return_value
            with patch("tdxdata.sources.adjust.fetch_factor", side_effect=RuntimeError("network down")):
                result = tc._apply_adjust(raw, "600519", "front", force_refresh=True)

            # 无缓存可兜底，返回未复权原值
            pd.testing.assert_frame_equal(result.reset_index(drop=True), raw.reset_index(drop=True))
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir

    @patch("mootdx.quotes.Quotes")
    def test_expired_cache_plus_empty_network_result_uses_fallback(self, mock_quotes_cls, tmp_path):
        """缓存过期 + 网络返回空 DataFrame → 用过期缓存兜底"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            tc._save_factor_cache("600519", "qfq", _make_factor_df())
            raw = _make_local_df(5, code="600519")
            raw_close = float(raw["close"].iloc[0])

            mock_quotes_cls.factory.return_value = mock_quotes_cls.return_value
            empty = pd.DataFrame(columns=["factor"])
            with patch("tdxdata.sources.adjust.fetch_factor", return_value=empty):
                result = tc._apply_adjust(raw, "600519", "front", force_refresh=True)

            assert result["close"].iloc[0] != pytest.approx(raw_close)
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir


class TestPrefetchFactorsFallback:
    def test_network_failure_with_expired_cache_succeeds(self, tmp_path):
        """预取时网络失败，但有过期缓存兜底 → 返回成功"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            # 写入缓存，并让其强制过期以触发网络路径
            tc._save_factor_cache("600519", "qfq", _make_factor_df())
            cache_path = os.path.join(tcm.FACTOR_CACHE_DIR, "600519_qfq.parquet")
            # 把 mtime 改到 31 天前，使其按 30 天 TTL 判定为过期
            old_ts = time.time() - 31 * 24 * 3600
            os.utime(cache_path, (old_ts, old_ts))

            with (
                patch("mootdx.quotes.Quotes") as mock_quotes_cls,
                patch("tdxdata.sources.adjust.fetch_factor", side_effect=RuntimeError("network down")),
            ):
                mock_quotes_cls.factory.return_value = mock_quotes_cls.return_value
                results = tc.prefetch_factors(["600519.SH"], dividend_type="前复权", max_workers=1)

            assert results.get("600519.SH") is True
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir

    def test_network_failure_without_cache_fails(self, tmp_path):
        """预取时网络失败且无任何缓存 → 返回失败"""
        import czsc.connectors.tdx_connector as tcm

        orig_dir = tcm.FACTOR_CACHE_DIR
        try:
            tcm.FACTOR_CACHE_DIR = str(tmp_path / "factors")
            with (
                patch("mootdx.quotes.Quotes") as mock_quotes_cls,
                patch("tdxdata.sources.adjust.fetch_factor", side_effect=RuntimeError("network down")),
            ):
                mock_quotes_cls.factory.return_value = mock_quotes_cls.return_value
                results = tc.prefetch_factors(["600519.SH"], dividend_type="前复权", max_workers=1)

            assert results.get("600519.SH") is False
        finally:
            tcm.FACTOR_CACHE_DIR = orig_dir
