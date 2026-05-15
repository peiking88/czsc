"""CZSC 分型/笔 可视化前端"""
from datetime import datetime, timedelta

import streamlit as st
from czsc import CZSC, Freq, format_standard_kline
from czsc.mock import generate_symbol_kines
from czsc.utils.plotting.kline import plot_czsc_chart

st.set_page_config(page_title="CZSC 分型/笔分析", layout="wide")
st.title("CZSC 分型/笔 可视化分析")

col1, col2, col3 = st.columns(3)
with col1:
    symbol = st.text_input("标的代码", "600519.SH")
with col2:
    freq = st.selectbox("周期", ["1分钟", "5分钟", "15分钟", "30分钟", "60分钟", "日线"], index=5)
with col3:
    days = st.slider("数据天数", 30, 500, 200)

if st.button("开始分析", type="primary"):
    edt = datetime.now().strftime("%Y%m%d")
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    with st.spinner(f"获取 {symbol} {freq} 数据..."):
        df = generate_symbol_kines(symbol, freq, sdt, edt)
        bars = format_standard_kline(df, freq)

    with st.spinner("分析分型/笔..."):
        freq_enum = Freq(freq)
        czsc_obj = CZSC(bars, freq=freq_enum)

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("分型", len(czsc_obj.fx_list))
    col_b.metric("笔", len(czsc_obj.bi_list))
    col_c.metric("线段", len(czsc_obj.seg_list))

    chart = plot_czsc_chart(czsc_obj, height=700)
    st.plotly_chart(chart.fig, use_container_width=True)

    with st.expander("笔列表"):
        data = []
        for bi in czsc_obj.bi_list:
            data.append({
                "方向": "↑" if bi.direction == "up" else "↓",
                "起始": bi.sdt.strftime("%Y-%m-%d %H:%M"),
                "结束": bi.edt.strftime("%Y-%m-%d %H:%M"),
                "高": bi.high,
                "低": bi.low,
            })
        st.dataframe(data[::-1], use_container_width=True)
