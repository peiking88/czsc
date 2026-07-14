# czsc - 缠中说禅技术分析工具

基于缠中说禅理论的量化交易 Python 库，核心算法（分型、笔、中枢）用 Rust 实现，通过 PyO3 暴露。Python ≥ 3.12。

```bash
pip install czsc -U
```

```python
from czsc import CZSC, Freq, format_standard_kline
from czsc.mock import generate_symbol_kines

bars = format_standard_kline(generate_symbol_kines('000001','30分钟','20240101','20240601'), freq=Freq.F30)
c = CZSC(bars)
print(f"笔：{len(c.bi_list)}, 中枢：{len(c.zs_list)}")
```

**特性**：222 个信号函数 · 多级别联立分析 · 信号-事件-交易体系 · HTML 可视化（plotly/lightweight-charts）

**[文档](https://s0cqcxuy3p.feishu.cn/wiki/wikcn3gB1MKl3ClpLnboHM1QgKf)** | **[B站教程](https://space.bilibili.com/243682308/channel/series)**
