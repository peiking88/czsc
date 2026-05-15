# TDX Connector 数据导入—周期选择与复权处理逻辑

## 一、整体入口

两个公共入口，共享同一套底层逻辑：

| 入口 | 用途 |
|------|------|
| `get_raw_bars(symbol, freq, sdt, edt, fq)` | 单标的、单周期、指定日期范围 |
| `sync_bars(symbol, fq)` | 单标的、全周期批量同步 |
| `sync_all(symbols, fq)` | 多标的、全周期批量同步（端点续跑） |

---

## 二、周期选择逻辑

### 2.1 周期映射表 `FREQ_MAP`

```
czsc 周期       →  tdxdata 本地读取周期
────────────────────────────────────
1分钟              1m    (直接读 .lc1)
5分钟              5m    (直接读 .lc5)
15分钟             5m    (读 5m → 重采样)
30分钟             5m    (读 5m → 重采样)
60分钟             5m    (读 5m → 重采样)
日线               1d    (直接读 .day)
周线               1d    (读 1d → 重采样)
月线               1d    (读 1d → 重采样)
```

**关键约束**：通达信本地只存储 `1m` / `5m` / `1d` 三个粒度，其余周期（15m/30m/60m/周线/月线）全部通过重采样得到。

### 2.2 两种路径分流

```
                    输入周期
                       │
            ┌──────────┴──────────┐
            ▼                      ▼
    RESAMPLE_FREQS?          原生周期
    (15m/30m/60m/W/M)      (1m/5m/1d)
            │                      │
            ▼                      ▼
    FREQ_MAP 映射到            FREQ_MAP 映射到
    基础周期读取               1d/1m/5m 读取
            │                      │
            ▼                      ▼
        _read_local()           _read_local()
        读 5m 或 1d             读对应文件
            │                      │
            ▼                      │
        _apply_adjust()            │
        对读出的基础               │
        数据施加复权               │
            │                      │
            ▼                      ▼
        _to_czsc_columns()     复权? → _apply_adjust()
            │                      │
            ▼                      ▼
        czsc.resample_bars()   _to_czsc_columns()
        重采样到目标周期
```

### 2.3 本地文件读取 `_read_local`

`czsc/connectors/tdx_connector.py:130`

区分市场（sh/sz/bj），直接构造 TDX 文件路径，绕过 mootdx 的市场解析（`get_stock_market` 无法区分同代码不同市场）。

| 周期 | 方法 | 文件路径 | 解析器 |
|------|------|---------|--------|
| 1d | daily | `vipdoc/{market}/lday/{market}{code}.day` | MooTdxDailyBarReader |
| 5m | minute_5 | `vipdoc/{market}/fzline/{market}{code}.lc5` | TdxLCMinBarReader |
| 1m | minute_1 | `vipdoc/{market}/minline/{market}{code}.lc1` | TdxMinBarReader |

归一化处理：
- 日期索引重置为列、统一列名 `date`
- `volume` 列名统一、四舍五入为整数（TDX .day 文件以 float 存储含浮点误差）

### 2.4 重采样实现 `czsc.resample_bars`

`czsc/py/bar_generator.py:199`

核心机制：

1. 对每条输入数据计算 `freq_end_time(dt, target_freq, market)` —— 得到该时间点所属的目标周期结束时刻
2. 按 `freq_edt` 分组聚合：

```
symbol → first
dt     → last
open   → first
close  → last
high   → max
low    → min
vol    → sum
amount → sum
```

3. 默认 `drop_unfinished=True`：若最后一根 K 线未完成则丢弃

**分钟重采样特例**：取尾部 2000 条的 `HH:MM` 唯一值序列，调用 `check_freq_and_market()` 推断交易所市场（期货 vs 股票），以正确对齐交易时段边界。

### 2.5 tdxdata 的并行实现 `resample_kline`

`tdxdata/sources/base.py:53`

tdxdata 也实现了 `resample_kline`（pandas 原生 `resample().agg()`），聚合规则一致。tdx_connector 当前统一使用 `czsc.resample_bars`，因其额外处理了市场时区对齐、未完成 K 线裁剪和 RawBar 转换。

---

## 三、复权处理逻辑

### 3.1 复权类型映射 `FQ_MAP`

```
czsc 中文    →  tdxdata 内部
──────────────────────────
前复权         front → qfq
后复权         back  → hfq
不复权         none  (跳过复权)
```

### 3.2 复权因子获取链路

```
_apply_adjust()
    │
    ▼
_get_adjust_factors(code, adjust_type)
    │
    ├── [缓存命中且新鲜] → 直接返回本地 parquet 缓存
    │
    └── [缓存过期/缺失/force_refresh]
            │
            ▼
        tdxdata.fetch_factor(code, adjust_type, quotes_client)
            │   tdxdata/sources/adjust.py:19
            ▼   (重试 3 次，退避 1s → 2s → 4s)
        _retry_fetch()
            │
            ▼
        quotes_client.xdxr(symbol)      ← 新浪财经除权除息事件
        quotes_client.get_k_data(...)     ← 新浪财经日线 K 线（取前收盘价）
            │
            ▼
        compute_factor_from_xdxr()
            │   tdxdata/sources/adjust.py:49
            ▼
        返回 factor DataFrame (index=日期, columns=factor)
```

### 3.3 因子计算公式 `compute_factor_from_xdxr`

`tdxdata/sources/adjust.py:49-104`

对每个除权除息事件迭代累乘：

```
fenhong     = 每股分红 / 10（若值 ≥ 1）
peigujia    = 配股价
songzhuangu = 每股送转股 / 10（若值 ≥ 1）
peigu       = 每股配股 / 10（若值 ≥ 1）

numerator   = pre_close - fenhong + peigujia * peigu
denominator = pre_close * (1 + songzhuangu + peigu)

qfq_factor = numerator / denominator     (前向累乘)
hfq_factor = denominator / numerator     (后向累乘)
```

**遍历方向**：qfq 按日期逆序（最近 → 最早），hfq 按日期正序（最早 → 最近）。这保证了累乘方向与价格调整方向一致。

### 3.4 因子缓存策略

`tdx_connector.py:246-292`

```
缓存目录:   ~/.czsc/tdxdata/{code}/
文件格式:   adjust_factor_{qfq|hfq}.parquet
新鲜度阈值: 1 天（最后因子日期距今 ≤ 1 天视为新鲜）
```

流程：

1. **新鲜缓存** → 直接使用，不走网络
2. **过期缓存** → 调用 `fetch_factor` 增量拉取，与旧缓存合并（新因子覆盖同日期旧值）
3. **拉取失败** → 降级使用过期缓存
4. **`force_refresh=True`** → 跳过新鲜度检查，强制重拉

### 3.5 因子应用 `_apply_adjust`

`tdx_connector.py:295-367`

```
输入: 包含 stock_code, date, open, high, low, close, volume 的 DataFrame
输出: OHLC 已复权的 DataFrame
```

**步骤分解：**

#### a) 精度对齐

统一转为 `datetime64[us]`，避免 pandas 3.x 下不同来源 datetime64 精度不一致（`[s]` vs `[us]`）导致 `merge_asof` 报 incompatible merge keys。

#### b) 分钟数据日期剥离

`df["_adj_date"] = df[date].dt.floor("D")`

复权因子按**自然日**生效。分钟数据的时间戳（如 `09:35:00`）会干扰 `merge_asof` 的匹配方向——例如 `"2025-01-15 09:35:00" > "2025-01-15"`，hfq 的正向匹配会跳过当日因子匹配到下一个。floor("D") 后统一为日期级别，消除此问题。

#### c) merge_asof 方向

```
qfq → direction="backward" : 每条 K 线匹配 ≤ 自身日期的最近因子
hfq → direction="forward"  : 每条 K 线匹配 ≥ 自身日期的最近因子
```

分钟数据因 floor("D") 处理后，backward 匹配当日因子、forward 匹配当日或次日因子，保证了分钟 K 线在除权日的正确调整。

#### d) 缺失填充

`merged["factor"].ffill().bfill().fillna(1.0)`

无因子覆盖的日期用 1.0（不做调整）。

#### e) qfq 归一化

`merged["factor"] = merged["factor"] / latest_factor`

以最新因子为基准缩放，使最新价格保持真实价值。

**不归一化的后果**：qfq 累积因子随时间增长可能极大（如数十倍），导致早期价格被过度缩小，图形严重失真。

#### f) 价格调整

`open / high / low / close * factor`

### 3.6 与 tdxdata `apply_adjust` 的对比

| 维度 | tdxdata apply_adjust | tdx_connector _apply_adjust |
|------|---------------------|----------------------------|
| 因子获取 | 每次调用 fetch_factor（无缓存） | 带缓存的 _get_adjust_factors |
| 分钟匹配 | 直接按时间戳 merge_asof | dt.floor("D") 日期剥离 |
| datetime64 精度 | 统一 `datetime64[us]` | 统一 `datetime64[us]` |
| qfq 归一化 | ✓ | ✓ |
| 空 merge 防护 | ✓ | ✓ |
| 列命名 | tdxdata 标准 `stock_code/date` | 适配 tdxdata 列名，输入灵活 |

---

## 四、端到端数据流示例

以 `get_raw_bars("600519.SH", "30分钟", "2024-01-01", "2024-12-31", "后复权")` 为例：

```
 1. Freq("30分钟") → freq_val = "30分钟"
 2. FREQ_MAP["30分钟"] → tdx_period = "5m"
 3. FQ_MAP["后复权"] → dividend_type = "back"
 4. "30分钟" in RESAMPLE_FREQS → need_resample = True

 5. _read_local("600519.SH", "5m", tdxdir)
    → 读 vipdoc/sh/fzline/sh600519.lc5
    → TdxLCMinBarReader 解析
    → DataFrame: [stock_code, date, open, high, low, close, volume, amount]

 6. _apply_adjust(df, "600519", "back")
    → _get_adjust_factors("600519", "hfq")
       → 缓存未命中
       → fetch_factor("600519", "hfq", quotes_client)
          → 新浪 xdxr + kline
          → compute_factor_from_xdxr → 全量历史因子
       → 写入 ~/.czsc/tdxdata/600519/adjust_factor_hfq.parquet
    → merge_asof(df, factor_df, left_on="_adj_date", direction="forward")
    → OHLC * factor

 7. _to_czsc_columns(df, "600519.SH")
    → stock_code → symbol, date → dt, volume → vol
    → symbol 列全部填充为 "600519.SH"
    → 去重、排序

 8. czsc.resample_bars(df, target_freq="30分钟", base_freq="5分钟")
    → 每行计算 freq_end_time(dt, Freq.F30, market)
    → groupby("freq_edt") 聚合 OHLCV
    → 生成 RawBar 对象列表

 9. return list[RawBar] (30分钟周期，后复权)
```

---

## 五、sync_bars 全周期批量逻辑

`sync_bars` 采用分层策略避免重复读取：

```
第 1 层: 读取原生数据
  ├── _read_local("5m") → 复权 → 存缓存 → result["5分钟"]
  └── _read_local("1d")  → 复权 → 存缓存 → result["日线"]

第 2 层: 从 5分钟 重采样
  ├── resample_bars(5分钟 → 15分钟) → 存缓存
  ├── resample_bars(5分钟 → 30分钟) → 存缓存
  └── resample_bars(5分钟 → 60分钟) → 存缓存

第 3 层: 从 日线 重采样
  ├── resample_bars(日线 → 周线) → 存缓存
  └── resample_bars(日线 → 月线) → 存缓存
```

每种周期的数据独立缓存为 `~/.czsc/tdxdata/{code}/{freq_file}.parquet`。

增量更新时，只从 TDX 文件读取原生数据（5m/1d），对比缓存中的 `last_dt`，仅追加新行，再重新派生重采样周期。

---

## 六、关键常量速查

```python
# 周期映射 — czsc → tdxdata 本地读取周期
FREQ_MAP = {
    "1分钟": "1m", "5分钟": "5m", "15分钟": "5m", "30分钟": "5m",
    "60分钟": "5m", "日线": "1d", "周线": "1d", "月线": "1d",
}

# 需要重采样的周期
RESAMPLE_FREQS = {"15分钟", "30分钟", "60分钟", "周线", "月线"}

# 重采样基础周期
RESAMPLE_BASE = {
    "15分钟": "5分钟", "30分钟": "5分钟", "60分钟": "5分钟",
    "周线": "日线", "月线": "日线",
}

# 复权类型映射
FQ_MAP = {"前复权": "front", "后复权": "back", "不复权": "none"}

# tdxdata 内部复权别名
ADJUST_MAP = {"front": "qfq", "back": "hfq", "none": None}

# 缓存文件名缩写
_FREQ_FILENAME = {
    "1分钟": "1m", "5分钟": "5m", "15分钟": "15m",
    "30分钟": "30m", "60分钟": "60m",
    "日线": "1d", "周线": "1w", "月线": "1M",
}
```
