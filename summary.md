# 工作摘要

**时间:** 2026-05-19

## 变更概要

### tdxdata 新增 fetch_kline 接口
- **`TdxData.fetch_kline(stock_code, period, count)`**: 通过 mootdx `bars()` 直接获取任意周期（1m/5m/15m/30m/1h/1d/1w/1mon）的完整 OHLCV K线
- 新增 `MinuteKlineSource` 数据源类（注册名 `minute_kline`），遵循 tdxdata 插件架构
- 新增测试 `test_minute_kline.py`（8 个用例），tdxdata 全量 164 个测试通过

### czsc 盘中实时数据导入重构
- **替换数据源**: 实时数据从 `minutes()`（单点 price，无 OHLC）改为 `fetch_kline()`（完整 OHLCV+datetime）
- **补全复权**: 实时数据现在与历史数据一样经过 `_apply_adjust()` 复权，拼接处不再价格跳变
- **简化流程**: 所有周期通过 `RT_KLINE_PARAMS` 直接映射，不再需要多层重采样
- 删除旧函数 `_fetch_realtime_daily()` 和 `_fetch_realtime_minute()`，新增统一函数 `_fetch_realtime_kline()`
- czsc 全量 319 个测试通过

## 版本变更
- `1.2.0` → `1.3.0`

## 最近提交
```
a303cf1 feat: 预测脚本增强——并发控制、TDX自选股读取、股票名称展示
34d8a89 chore: 更新工作摘要，新增 Claude Code skill 配置
5d00d92 feat: 预测报告新增多周期缠论综合解读，含共振分析与风险提示
4b779d4 feat: 多股票合并预测报告，支持综合概览汇总
48827f3 fix: 统一预测报告文件名格式为 output/czsc_<symbol>.md
```
