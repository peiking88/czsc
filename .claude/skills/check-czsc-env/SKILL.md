---
name: check-czsc-env
description: czsc 项目开发环境健康检查，涵盖工具链、依赖、测试套件和已知排障。
---

# 检查开发环境完整性

对 czsc 项目进行环境健康检查。这是一个**诊断工具**——发现问题时报告并建议修复方式，不要擅自修改代码或配置，除非用户明确要求。

## 执行步骤

并行执行以下五个检查组，收集结果后汇总报告。

### 检查组 1：基础工具链

| 检查项 | 命令 | 通过条件 |
|--------|------|----------|
| UV | `uv --version` | 版本号输出 |
| Python | `uv run python -c "import sys; print(sys.version)"` | 3.10+ |
| Git 状态 | `git status --short` | 列出变更 |
| Git 远程 | `git remote -v` 且 `git log --oneline -1` | origin 存在 |
| .venv | 检查 `.venv/bin/python` 是否存在 | 存在 |

### 检查组 2：系统级依赖

检查编译 Python C 扩展所需的系统库是否存在：

- `dpkg -l libta-lib0 ta-lib-dev` — ta-lib C 库
- `cmake --version` — 构建工具（pyarrow 等需要）
- `ninja --version` 或 `make --version` — 构建后端

任一缺失时标记为阻塞项，提示安装命令后**跳过后续依赖同步**。

### 检查组 3：依赖同步

仅在检查组 2 全部通过时执行：

1. 运行 `uv sync --extra dev`
2. 如果失败，分析错误信息并对照下方的"已知排障指南"给出建议，**不要自动修改 lock 文件或代码**
3. 记录结果（成功/失败+原因）

### 检查组 4：工具链与导入

仅在依赖同步成功时执行：

| 检查项 | 命令 | 通过条件 |
|--------|------|----------|
| ruff | `.venv/bin/ruff --version` | 版本号输出 |
| basedpyright | `.venv/bin/basedpyright --version` | 版本号输出 |
| czsc 导入 | `uv run python -c "import czsc; print(czsc.__version__)"` | 版本号输出 |

### 检查组 5：测试套件

仅在检查组 4 通过时执行：

运行 `uv run pytest test/ -q --tb=line`，记录：
- 通过数 / 失败数 / 跳过数
- 每个失败测试的名称和一行原因

**不要自动修复测试失败**，仅报告。如果用户要求修复，退出本命令后单独处理。

## 报告格式

检查完毕后输出如下格式：

```
## 环境检查报告

| 检查项 | 状态 | 说明 |
|--------|------|------|
| UV x.x.x | ✅/❌ | ... |
| Python x.x.x | ✅/❌ | ... |
| ... | ... | ... |

**结论：环境完整 / 存在 N 个阻塞项 / 存在 N 个警告**
```

- ✅ 正常
- ❌ 阻塞（必须修复才能继续开发）
- ⚠️ 警告（不影响核心开发但需关注）

## 已知排障指南

执行过程中如果遇到以下问题，按对应建议处理：

| 问题 | 原因 | 建议操作 |
|------|------|----------|
| pyarrow 构建失败 | Python 版本太新，无预编译 wheel | `uv lock --upgrade-package pyarrow` 升级到有 wheel 的版本 |
| ta-lib 编译失败 | 缺少系统 C 库 | `sudo apt install -y libta-lib0 ta-lib-dev` |
| test_pdf_report 失败 | snap chromium 不支持 `--remote-debugging-pipe`，kaleido 无法通过管道通信 | 运行 `choreo_get_chrome --i -1` 下载 Chrome for Testing，choreographer 会优先使用它 |
| pandas FutureWarning | ChainedAssignment 在 pandas 3.0 将变更 | 来自 eda.py、backtest_report.py，已知问题 |
| env key 大小写 | Linux 上 `os.environ` 区分大小写 | 函数用小写 key（如 `czsc_verbose`），测试需匹配；已修复 `test_envs.py` |
| 依赖下载慢 | PyPI 默认源在国外 | 已在 `pyproject.toml` `[tool.uv]` 配置阿里云镜像；如仍慢可临时用 `UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` |
| 数据单位不一致 | mootdx 日线 volume 输出"手"而非"股" | 已在 `tdx_connector.py` 的 `_read_local()` 和 `_fetch_realtime_kline()` 中 `× 100` 修复；详见 `docs/tdx_connector_data_flow.md` 第七章 |
| 1分钟 OHLC 异常（百万级） | `TdxMinBarReader` 将 .lc1 float 当 int 解析 | 已改用 `TdxLCMinBarReader`；若缓存了错误数据需删除 `~/.czsc/tdxdata/` 下对应 1m 缓存 |

## 数据单位规范速查

`get_raw_bars` / `sync_bars` 返回数据的统一单位：

| 字段 | 单位 |
|------|------|
| open / high / low / close | 元 |
| volume | 股 |
| amount | 元 |

各数据源原始单位：`.lc1`/`.lc5`（股）、`.day`（股，mootdx 误转为手）、`bars()`（手）、`transaction()`（手）。修复点均在 `tdx_connector.py` 内部，用户无需关心。
