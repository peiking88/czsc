# CLAUDE.md

CZSC（缠中说禅技术分析工具）是基于缠论的分型/笔/线段自动识别量化交易 Python 库。采用 Rust/Python 混合架构，优先使用 Rust 版本（rs-czsc），Python 版本作为回退。

## 常用开发命令

```bash
# 依赖同步
uv sync --extra dev

# 测试（配置见 pyproject.toml [tool.pytest.ini_options]）
uv run pytest                          # 全部测试
uv run pytest test/test_analyze.py -v  # 指定文件
uv run pytest --cov=czsc               # 带覆盖率

# 代码质量（ruff + basedpyright，配置见 pyproject.toml）
uv run ruff check czsc/ test/
uv run ruff format czsc/ test/ --line-length 120
uv run basedpyright czsc/
```

## 目录架构

| 目录 | 职责 |
|------|------|
| `czsc/core.py` | 混合架构入口，智能选择 Rust/Python 实现 |
| `czsc/py/` | Python 版核心算法（分型、笔、线段识别） |
| `czsc/traders/` | 交易执行框架（CzscTrader、权重管理、回测） |
| `czsc/signals/` | 按类别组织的信号函数（bar/pos/cxt/tas/vol） |
| `czsc/sensors/` | CTA 研究框架、特征分析、事件检测 |
| `czsc/utils/` | K线生成、缓存、Streamlit 组件、技术指标 |
| `czsc/connectors/` | 多数据源连接器（天勤、Tushare、聚宽、CCXT） |
| `czsc/svc/` | 统计与可视化服务（回测分析、因子、相关性） |

## 关键约束

### 测试规范
- 测试数据统一通过 `czsc.mock.generate_symbol_kines` 生成，**不得硬编码**
- 测试文件位于 `test/`，命名 `test_*.py`
- 真实测试优先级高于 mock，第三方组件不计入覆盖率

### 代码规范（pyproject.toml 中配置）
- 行长度 120 字符
- 信号函数版本化命名（如 `V241013`）
- 公共函数必须有 docstring
- 使用模块级常量消除魔法值，优先类型提示

### 数据流模式
```python
from czsc.core import CZSC, format_standard_kline, Freq
from czsc.mock import generate_symbol_kines

df = generate_symbol_kines('000001', '30分钟', '20240101', '20240105')
bars = format_standard_kline(df, freq=Freq.F30)
czsc_obj = CZSC(bars)
```

## 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `CZSC_USE_PYTHON` | 强制使用 Python 实现 | 空（用 Rust） |
| `CZSC_HOME` | 缓存/数据根目录 | `~/.czsc` |
| `czsc_min_bi_len` | 最小笔长度（含K线数） | `6` |
| `czsc_max_bi_num` | 单级别最大保存笔数 | `50` |
| `czsc_verbose` | 输出执行过程详情 | 空（关闭） |
| `czsc_welcome` | 输出版本标识与缠论摘记 | `"0"`（关闭） |
| `czsc_research_cache` | 投研数据缓存路径 | `D:\CZSC投研数据` |

## 缓存管理

- 缓存位置：`CZSC_HOME` 环境变量或 `~/.czsc`
- 超过 1GB 时显示清理提示
- 函数：`czsc.utils.cache.home_path`、`czsc.empty_cache_path()`、`czsc.get_dir_size()`

## 重要链接

- [项目文档](https://s0cqcxuy3p.feishu.cn/wiki/wikcn3gB1MKl3ClpLnboHM1QgKf)
- [API 文档](https://czsc.readthedocs.io/en/latest/modules.html)
