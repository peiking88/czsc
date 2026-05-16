# 工作摘要

**时间:** 2026-05-16 18:05:00

## 变更概要

- **fix**: `_normalize_symbol` / `_get_market` 增强股票代码前缀/后缀剪裁能力
  - 新增小写后缀支持（`.sh` / `.sz` / `.bj`）
  - 新增无点前缀支持（`sh600519` / `sz000858` / `bj899050`）
  - 新增有点前缀支持（`sh.600519` / `SH.600519`）
  - 用精确的 `startswith`/`endswith` + 切片 替换 `.replace()`
  - 与 mootdx `StdHqAdapter._clean_code` 行为对齐
  - 补充 16 个测试用例，覆盖全部新增格式

## 最近提交
```
ff671f6 feat: 盘中实时数据导入 + 交易日历集成
0b50f4f refactor: 移除因子缓存层，简化复权委托，新增 Streamlit 分析前端
933cfab fix: env key 大小写兼容、resample_bars → resample_kline 适配、pyarrow 版本约束
d892f91 fix: 同步 tdxdata apply_adjust 修复——qfq归一化、datetime64精度对齐、空merge防护
2da6fde fix: 修复代码审查发现的 P0/P1/P2 级问题
```
