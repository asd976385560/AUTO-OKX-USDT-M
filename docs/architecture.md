# 系统架构

## 五层架构

```
┌─────────────────────────────────────────────────────────────┐
│                     采集层（Job A + Job E）                   │
│  · Job A（15min）：全币种 Ticker / 资金费率 / 15m K线 /      │
│    账户 / 新闻 / 搜索数据                                    │
│  · Job E（1h）：1H/4H/1D K线 / 跨市场宏观 / 情绪 / Regime   │
├─────────────────────────────────────────────────────────────┤
│                     数据库层（4 SQLite）                       │
│  market.db · news.db · account.db · lessons.db              │
│  增量追加（时间序列），非全量覆盖                               │
├─────────────────────────────────────────────────────────────┤
│                     决策层（Job B）                           │
│  全币种扫描 → ctVal 过滤 → 五维评分 → 小灵推理（80%裁量）     │
│  实时获取 OKX 账户信息                                       │
│  吸取 Job C 复盘经验调整策略                                  │
├─────────────────────────────────────────────────────────────┤
│                     执行层                                   │
│  okx-cex-trade：下单 / 撤单 / 改单                           │
│  algo 强制挂单：止损必挂，失败则市价平仓                       │
├─────────────────────────────────────────────────────────────┤
│                     复盘层（Job C）                           │
│  假设验证 / 交易归因 / 五维评估 / 错判识别                    │
│  优化 A/B/E：策略 / 搜索配置 / 采集异常                       │
│  输出：playbook / lessons / daily_reports                    │
└─────────────────────────────────────────────────────────────┘
```

## Job 交互关系

```
Job A (15min) ──→ market.db / news.db / account.db ──┐
                                                      ├──→ Job B (15min) ──→ 交易执行
Job E (1h)    ──→ market.db / news.db              ──┘         │
                                                                │
Job C (daily) ◄── account.db / lessons.db / market.db ◄────────┘
     │
     ├──→ 优化 Job B：playback / lessons / param_suggestions
     ├──→ 优化 Job A：缺失数据补充、搜索关键词调整
     └──→ 优化 Job E：数据源异常检测、采集频率建议
```

## 技术栈

| 层 | 技术 |
|----|------|
| 采集 | Python + OKX HTTP API（`_okx_http.py` 批量并发）|
| 数据库 | SQLite 3（WAL 模式） |
| 决策 | OpenClaw Agent（isolated session） |
| 执行 | OKX CLI（`okx swap place` / `okx algo order`） |
| 推送 | QQ Bot（OpenClaw cron delivery） |
| 复盘 | OpenClaw Agent + Python 脚本 |
