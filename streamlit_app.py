"""
CZSC 数据导入与缠论分析报告 - Streamlit 前端
启动方式: uv run streamlit run streamlit_app.py
"""

from datetime import date
import os

import pandas as pd
import streamlit as st
import warnings

warnings.filterwarnings("ignore")

from czsc.connectors.tdx_connector import (
    get_raw_bars,
    scan_stocks,
    _normalize_symbol,
    DEFAULT_TDXDIR,
)
from czsc.core import CZSC
from czsc.utils.plotting.kline import plot_czsc_chart

st.set_page_config(page_title="CZSC 缠论分析报告", page_icon="📊", layout="wide")

# ---------------------------------------------------------------------------
# 自选股加载
# ---------------------------------------------------------------------------

ZXG_BLK = os.path.join(DEFAULT_TDXDIR, "T0002", "blocknew", "zxg.blk")

# 特殊代码映射
_SPECIAL_NAMES = {"999999": "上证指数", "000988": "深证成指"}


@st.cache_data(ttl=3600)
def _load_stock_names() -> dict[str, str]:
    """从 tdxdata 获取全量股票代码→名称映射，缓存 1 小时"""
    try:
        from tdxdata.api import TdxData

        tdx = TdxData()
        sh = tdx._get_stocks(1)  # MARKET_SH
        sz = tdx._get_stocks(0)  # MARKET_SZ
        tdx.close()
        df = pd.concat([sh, sz], ignore_index=True)
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}


@st.cache_data
def load_zxg_list() -> list[tuple[str, str]]:
    """加载 TDX 自选股列表，返回 [(czsc_symbol, display_name), ...]"""
    stocks: list[tuple[str, str]] = []
    if not os.path.exists(ZXG_BLK):
        return stocks

    with open(ZXG_BLK, "rb") as f:
        raw = f.read()
    text = raw.decode("gbk", errors="ignore")
    codes = [c.strip() for c in text.split() if c.strip()]

    name_map = _load_stock_names()

    for code in codes:
        if len(code) != 7:
            continue
        market_prefix = code[0]
        real_code = code[1:]

        if market_prefix == "1":
            czsc_sym = f"{real_code}.SH"
        elif market_prefix == "0":
            czsc_sym = f"{real_code}.SZ"
        else:
            continue

        name = name_map.get(real_code, _SPECIAL_NAMES.get(real_code, real_code))
        stocks.append((czsc_sym, f"{name} ({real_code})"))

    return stocks


def render_table(df: pd.DataFrame, key: str = "", compact: bool = False) -> None:
    """用 HTML 渲染表格，带分页控制和水平滚动。

    :param df: 要渲染的 DataFrame
    :param key: 分页组件的唯一 key
    :param compact: True 时使用更紧凑的字体和间距，适合列数多的表格
    """
    PAGE_SIZE = 10
    total = len(df)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    if total <= PAGE_SIZE:
        page = 1
    else:
        page = st.number_input("页码", min_value=1, max_value=pages, value=1, step=1, key=f"page_{key}")

    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_df = df.iloc[start:end]

    font_size = "12px" if compact else "14px"
    cell_padding = "4px 8px" if compact else "6px 12px"

    html = (
        f'<div style="overflow-x:auto; width:100%;">'
        f'<table style="width:100%; border-collapse:collapse; font-size:{font_size}; white-space:nowrap;">'
        "<thead><tr>"
        + "".join(
            f'<th style="text-align:left; padding:{cell_padding}; border-bottom:2px solid #ddd;">{c}</th>'
            for c in page_df.columns
        )
        + "</tr></thead>"
        "<tbody>"
        + "".join(
            "<tr>"
            + "".join(
                f'<td style="text-align:left; padding:{cell_padding}; border-bottom:1px solid #eee;">{v}</td>'
                for v in row
            )
            + "</tr>"
            for row in page_df.values
        )
        + "</tbody></table></div>"
    )
    st.markdown(html, unsafe_allow_html=True)
    if total > PAGE_SIZE:
        st.caption(f"第 {page}/{pages} 页，共 {total} 条 | 每页 {PAGE_SIZE} 条")


st.title("📊 CZSC 缠论分析工具")
st.caption("基于缠中说禅理论的技术分析 · 数据源：通达信本地数据")

# ── 顶部 Tab 切换 ─────────────────────────────────────────────────────
tab_import, tab_analysis = st.tabs(["📥 数据导入", "🔍 缠论分析"])

# ── Sidebar ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 参数设置")

    # 自选股下拉框
    zxg_list = load_zxg_list()
    if zxg_list:
        # zxg_list: [(czsc_symbol, display_name), ...]  display_name 格式为 "股票名称 (代码)"
        symbol_map = {name: code for code, name in zxg_list}
        default_idx = next((i for i, (c, _) in enumerate(zxg_list) if c == "600519.SH"), 0)
        # 下拉框高度加倍
        st.markdown(
            "<style>div[data-baseweb='select'] div[role='listbox'] ul "
            "{max-height: 600px !important}</style>",
            unsafe_allow_html=True,
        )
        symbol_name = st.selectbox(
            "自选股",
            options=list(symbol_map.keys()),
            index=default_idx,
            help="数据来源：TDX 自选股列表 zxg.blk",
        )
        symbol = symbol_map[symbol_name]
    else:
        symbol = st.text_input(
            "股票代码",
            value="600353.SH",
            placeholder="如: 600353.SH",
            help="未找到自选股文件，请手动输入",
        )

    # Frequency
    freq = st.selectbox(
        "K线周期",
        ["日线", "30分钟", "60分钟", "15分钟", "5分钟", "周线", "月线"],
        index=0,
    )

    # Date range
    col1, col2 = st.columns(2)
    with col1:
        sdt = st.date_input("开始日期", value=pd.to_datetime("2025-01-01"))
    with col2:
        edt = st.date_input("结束日期", value=date.today())

    # FQ type
    fq = st.selectbox("复权方式", ["前复权", "后复权", "不复权"], index=0)

    st.divider()

    # TDX 数据目录信息
    st.caption(f"TDX 数据目录: `{DEFAULT_TDXDIR}`")
    try:
        stocks = scan_stocks(DEFAULT_TDXDIR)
        sh = sum(1 for s in stocks if s.endswith(".SH"))
        sz = sum(1 for s in stocks if s.endswith(".SZ"))
        bj = sum(1 for s in stocks if s.endswith(".BJ"))
        st.caption(f"本地数据: SH {sh} + SZ {sz} + BJ {bj} = {len(stocks)} 个")
    except Exception:
        st.caption("本地可用标的: 未扫描")
    try:
        from tdxdata.calendar import is_trading_day
        from czsc.connectors.tdx_connector import _is_trading_time

        today = date.today().strftime("%Y-%m-%d")
        if is_trading_day(today):
            if _is_trading_time():
                st.caption(f"📡 {today} 盘中 · 实时数据可用")
            else:
                st.caption(f"📡 {today} 交易日(已收盘)")
        else:
            st.caption(f"📡 {today} 非交易日")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════
# Tab 1: 数据导入
# ══════════════════════════════════════════════════════════════════════
with tab_import:
    import_btn = st.button("🚀 导入数据", type="primary", key="import_btn")

    if import_btn:
        code = _normalize_symbol(symbol)
        fq_map = {"前复权": "front", "后复权": "back", "不复权": "none"}
        adjust_type = fq_map.get(fq)

        # ── 1. 原始数据（不复权） ────────────────────────────────────────
        st.subheader("📋 原始数据（不复权）")
        with st.spinner("读取原始数据..."):
            try:
                raw_df = get_raw_bars(symbol, freq, str(sdt), str(edt), fq="不复权", raw_bar=False)
            except Exception as e:
                st.error(f"读取失败: {e}")
                raw_df = pd.DataFrame()

        if raw_df.empty:
            st.warning("未读取到数据，请检查日期范围和股票代码")
        else:
            st.metric("原始数据条数", len(raw_df))
            display_raw = raw_df.copy()
            for col in ["open", "close", "high", "low"]:
                if col in display_raw.columns:
                    display_raw.loc[:, col] = display_raw[col].round(2)
            if "amount" in display_raw.columns:
                display_raw.loc[:, "amount"] = display_raw["amount"].round(0)
            display_raw = display_raw.sort_values("dt", ascending=False)
            render_table(display_raw, key="raw")

            # ── 2. 复权因子 ─────────────────────────────────────────────────
            if fq != "不复权" and adjust_type:
                st.subheader(f"📐 复权因子（{fq}）")
                with st.spinner("获取复权因子..."):
                    try:
                        from mootdx.quotes import Quotes
                        from tdxdata.sources.adjust import fetch_factor

                        client = Quotes.factory(market="std")
                        factor_df = fetch_factor(code, adjust_type, client)
                    except Exception as e:
                        st.warning(f"复权因子获取失败: {e}")
                        factor_df = pd.DataFrame()

                if factor_df is not None and not factor_df.empty:
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.metric("因子条数", len(factor_df))
                    with c2:
                        st.metric("起始日期", str(factor_df.index.min().date()))
                    with c3:
                        st.metric("截止日期", str(factor_df.index.max().date()))

                    display_factor = factor_df.copy()
                    display_factor.loc[:, "factor"] = display_factor["factor"].round(6)
                    display_factor = display_factor[~display_factor.index.duplicated(keep="last")]
                    display_factor.index = display_factor.index.strftime("%Y-%m-%d")
                    st.dataframe(display_factor.T, use_container_width=True, height=120)
                else:
                    st.info("无复权因子数据（可能该标的无除权记录）")

            # ── 3. 复权后数据 ────────────────────────────────────────────────
            if fq != "不复权":
                st.subheader(f"📊 复权后数据（{fq}）")
                with st.spinner("应用复权..."):
                    try:
                        adj_df = get_raw_bars(symbol, freq, str(sdt), str(edt), fq=fq, raw_bar=False)
                    except Exception as e:
                        st.error(f"复权数据获取失败: {e}")
                        adj_df = pd.DataFrame()

                if not adj_df.empty:
                    display_adj = adj_df.copy()
                    for col in ["open", "close", "high", "low"]:
                        if col in display_adj.columns:
                            display_adj.loc[:, col] = display_adj[col].round(2)
                    if "amount" in display_adj.columns:
                        display_adj.loc[:, "amount"] = display_adj["amount"].round(0)
                    display_adj = display_adj.sort_values("dt", ascending=False)
                    render_table(display_adj, key="adj")

                    # ── 4. 价格对比 ─────────────────────────────────────────
                    st.subheader("📈 复权前后价格对比")
                    if len(raw_df) == len(adj_df):
                        compare = pd.DataFrame({
                            "日期": raw_df["dt"].values,
                            "原始收盘价": raw_df["close"].round(2).values,
                            f"{fq}收盘价": adj_df["close"].round(2).values,
                            "复权比率": (adj_df["close"].values / raw_df["close"].values).round(4),
                        })
                        compare = compare.sort_values("日期", ascending=False)
                        render_table(compare, key="compare")
                    else:
                        st.info("原始数据与复权数据条数不一致，跳过对比")
                else:
                    st.warning("复权后无数据")
            else:
                st.info("已选择\"不复权\"，无需对比")
    else:
        st.info("👈 在左侧设置参数后，点击\"导入数据\"查看原始数据与复权效果")

# ══════════════════════════════════════════════════════════════════════
# Tab 2: 缠论分析
# ══════════════════════════════════════════════════════════════════════
with tab_analysis:
    generate = st.button("🚀 一键生成分析报告", type="primary", key="analysis_btn")

    if not generate:
        st.info("👈 设置参数后，点击\"一键生成分析报告\"开始分析")
    else:
        # Fetch data
        with st.spinner(f"正在获取 {symbol} {freq} 数据 ({fq})..."):
            try:
                bars = get_raw_bars(symbol, freq, str(sdt), str(edt), fq=fq)
            except Exception as e:
                st.error(f"数据获取失败: {e}")
                bars = []

        if not bars:
            st.error(f"未获取到数据，请检查日期范围和股票代码是否正确")
        else:
            # Run CZSC analysis
            with st.spinner("正在执行缠论分析..."):
                try:
                    czsc_obj = CZSC(bars)
                except Exception as e:
                    st.error(f"缠论分析失败: {e}")
                    czsc_obj = None

            if czsc_obj is not None:
                # ── Report Header ────────────────────────────────────────
                st.header(f"🔍 {symbol} 分析报告")

                # Key metrics row
                col1, col2, col3, col4, col5 = st.columns(5)
                bar_count = len(bars)
                first_bar = bars[0]
                last_bar = bars[-1]
                change_pct = (last_bar.close / first_bar.close - 1) * 100

                with col1:
                    st.metric("K线数量", bar_count)
                with col2:
                    st.metric("起始价", f"{first_bar.close:.2f}")
                with col3:
                    st.metric("最新价", f"{last_bar.close:.2f}", delta=f"{change_pct:.2f}%")
                with col4:
                    st.metric("分型数", len(czsc_obj.fx_list))
                with col5:
                    st.metric("笔数", len(czsc_obj.bi_list))

                # ── K-line Chart ─────────────────────────────────────────
                st.subheader("📈 K线缠论分析图")

                with st.spinner("正在生成K线图..."):
                    try:
                        total = len(bars)
                        last_date = str(bars[-1].dt.date())
                        first_date = str(bars[0].dt.date())
                        chart = plot_czsc_chart(czsc_obj, height=650)
                        st.plotly_chart(chart.fig, use_container_width=True)
                        st.caption(
                            f"数据范围: {first_date} ~ {last_date}"
                            f" | 共 {total} 根K线 | 最新: {last_date}"
                        )
                    except Exception as e:
                        st.warning(f"K线图生成失败: {e}，使用备用简单图表")
                        df_bars = pd.DataFrame([{
                            "日期": b.dt,
                            "开": round(b.open, 2),
                            "高": round(b.high, 2),
                            "低": round(b.low, 2),
                            "收": round(b.close, 2),
                            "量": int(b.vol),
                        } for b in bars])
                        render_table(df_bars, key="bars")

                # ── 分型详情 ─────────────────────────────────────────────
                with st.expander("🔺 分型列表", expanded=False):
                    st.caption("顶分型(G)：局部高点；底分型(D)：局部低点")

                    if czsc_obj.fx_list:
                        fx_data = []
                        for fx in czsc_obj.fx_list:
                            fx_data.append({
                                "日期": str(fx.dt)[:10],
                                "分型": str(fx.mark),
                                "分型值": round(fx.fx, 2),
                            })
                        df_fx = pd.DataFrame(fx_data[::-1])
                        render_table(df_fx, key="fx")
                    else:
                        st.info("当前数据范围内未检测到分型")

                # ── 笔详情 ───────────────────────────────────────────────
                with st.expander("📝 笔列表", expanded=False):

                    if czsc_obj.bi_list:
                        bi_data = []
                        for bi in czsc_obj.bi_list:
                            bi_data.append({
                                "方向": "↑ 向上" if bi.direction.value == "向上" else "↓ 向下",
                                "起始日": str(bi.sdt)[:10],
                                "结束日": str(bi.edt)[:10],
                                "最高": round(bi.high, 2),
                                "最低": round(bi.low, 2),
                                "力度": round(bi.power, 2),
                                "R²": round(bi.rsq, 3),
                                "加速度": round(bi.acceleration, 1),
                                "信噪比": round(bi.SNR, 2),
                            })
                        df_bi = pd.DataFrame(bi_data[::-1])

                        up_count = sum(1 for b in czsc_obj.bi_list if b.direction.value == "向上")
                        down_count = len(czsc_obj.bi_list) - up_count

                        c1, c2 = st.columns(2)
                        with c1:
                            st.metric("向上笔", up_count)
                        with c2:
                            st.metric("向下笔", down_count)

                        render_table(df_bi, key="bi", compact=True)
                    else:
                        st.info("当前数据范围内未检测到笔")

                # ── 趋势质量评估 ─────────────────────────────────────
                if czsc_obj.bi_list:
                    st.subheader("📡 趋势质量评估")
                    bi_list = czsc_obj.bi_list
                    last_bi = bi_list[-1]

                    # R² — 当前笔拟合度，反映当前趋势的确定性
                    cur_rsq = last_bi.rsq
                    if cur_rsq > 0.8:
                        rsq_msg = f"🟢 趋势规整 (R²={cur_rsq:.3f})，方向明确"
                    elif cur_rsq > 0.6:
                        rsq_msg = f"🟡 趋势一般 (R²={cur_rsq:.3f})，关注方向变化"
                    else:
                        rsq_msg = f"🔴 趋势散乱 (R²={cur_rsq:.3f})，方向不确定"

                    # 加速度 — 当前笔速度变化
                    accel = last_bi.acceleration
                    if accel > 10:
                        accel_msg = f"🟢 加速中 ({accel:.1f})，趋势强劲"
                    elif accel > -10:
                        accel_msg = f"🟡 匀速/减速 ({accel:.1f})，关注转折"
                    else:
                        accel_msg = f"🔴 反向加速 ({accel:.1f})，趋势可能反转"

                    # 力度衰减 — 对比同方向笔的力度变化
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

                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.caption(rsq_msg)
                    with c2:
                        st.caption(accel_msg)
                    with c3:
                        st.caption(power_msg)

                    # 当前趋势
                    if last_bi.direction.value == "向上":
                        st.success(f"当前趋势：上升笔中 📈 (力度={last_bi.power:.1f})")
                    else:
                        st.warning(f"当前趋势：下降笔中 📉 (力度={last_bi.power:.1f})")

                    if czsc_obj.bars_ubi:
                        ubi_bars = czsc_obj.bars_ubi
                        ubi_high = max(b.high for b in ubi_bars)
                        ubi_low = min(b.low for b in ubi_bars)
                        last_dir = last_bi.direction.value
                        ubi_dir = "↓ 向下" if last_dir == "向上" else "↑ 向上"
                        st.info(
                            f"🔄 未完成笔 ({len(ubi_bars)} 根K线)："
                            f"方向 {ubi_dir} | "
                            f"起始 {str(ubi_bars[0].dt)[:10]} | "
                            f"最高 {ubi_high:.2f} | 最低 {ubi_low:.2f}"
                        )

                # ── Footer ───────────────────────────────────────────────
                st.divider()
                st.caption(
                    f"本报告由 CZSC 缠中说禅技术分析工具生成 | "
                    f"数据范围: {bars[0].dt} ~ {bars[-1].dt} | "
                    f"周期: {freq} | 复权: {fq}"
                )
