# 数据流与存储模式

## 总体模式：增量追加（时间序列）

每轮 Job A/E 只插入**本轮新获取的数据**，历史数据全部保留。数据库是时间序列型存储。

## 各表存储详情

| 表 | 每轮插入量 | SQL 模式 | 去重策略 |
|----|-----------|---------|---------|
| tick_snapshots | ~313 行（全币种） | `INSERT OR REPLACE` | PK: (ts, symbol) |
| kline_cache | ~18K 行（60 bars × 313 币） | `INSERT OR REPLACE` | PK: (ts, symbol, tf) |
| derivatives | ~313 行 | `INSERT OR REPLACE` | PK: (ts, symbol) |
| cross_market | 1 行 | `INSERT OR REPLACE` | PK: (ts) |
| news_items | 仅新新闻 | `INSERT OR IGNORE` | hash 去重 |
| coin_sentiment | 全币种情绪 | `INSERT OR REPLACE` | PK: (ts, symbol, period) |
| account_snapshots | 1 行 | `INSERT OR REPLACE` | PK: (ts, profile) |
| position_snapshots | 有持仓时插入 | DELETE + INSERT | 同 ts/profile 先删后插 |

## 数据增长估算

| 表 | 每轮 | 1 天（96 轮 A） | 1 月 |
|----|------|----------------|------|
| tick_snapshots | ~313 行 | ~30K 行 | ~900K 行 |
| kline_cache | ~18K 行 | ~180K 行 | ~5.4M 行 |
| derivatives | ~313 行 | ~30K 行 | ~900K 行 |

## 数据新鲜度要求

| 场景 | 要求 | 处理 |
|------|------|------|
| Job A 最近成功 ≤ 15 min | ✅ FRESH | 正常决策 |
| Job A 15-60 min | ⚠️ 降级 | dim4 cap 6，禁止激进加仓 |
| Job A > 60 min | 🔴 STALE | 禁止新开仓，仅允许 CLOSE/IDLE |

## Job B 读取方式

```sql
-- 最新 Ticker
SELECT * FROM tick_snapshots WHERE ts = (SELECT MAX(ts) FROM tick_snapshots)

-- 最近 60 根 15m K线
SELECT * FROM kline_cache WHERE symbol = ? AND tf = '15m' ORDER BY ts DESC LIMIT 60

-- 最新跨市场
SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1

-- 最新账户
SELECT * FROM account_snapshots ORDER BY ts DESC LIMIT 1

-- OKX live 持仓（必须实时查询，不能仅依赖 DB）
okx account positions --instType SWAP --profile live
```

## 重要：OKX live vs DB 不一致

position_snapshots 可能与 OKX live 实际持仓不一致。

**Job B 必须先查 OKX live 确认持仓，再决定动作。**
