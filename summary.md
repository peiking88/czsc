# 工作摘要

**时间:** 2026-05-15

## 任务：适配 tdxdata 最新变更

同步 `tdxdata` v0.8.2 中 `apply_adjust` 的三处修复到 `czsc.connectors.tdx_connector._apply_adjust`：

1. **qfq 归一化** — 以最新因子为基准缩放，使 qfq 价格反映真实价值
2. **datetime64 精度对齐** — 统一转为 `datetime64[us]`，兼容 pandas 3.x
3. **空 merge 防护** — 防止空 DataFrame 导致 `iloc[-1]` 越界

## 变更文件

- `czsc/connectors/tdx_connector.py` — 新增：通达信数据源连接器
- `czsc/__init__.py` — 版本号升至 0.10.13
- `docs/tdx_connector_data_flow.md` — 新增：数据导入周期选择与复权处理逻辑文档

## 测试

`test/test_tdx_connector.py` 全部 49 个测试通过。
