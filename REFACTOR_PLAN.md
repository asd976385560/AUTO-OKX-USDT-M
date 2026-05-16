# OKX 交易系统重构计划 v2

> 创建时间：2026-05-11 22:16 UTC+8
> 基于：主人 22:16 反馈修订

---

## 一、重构目标

### 核心定位
**E:\OKX\ 是一个可迁移、可分享的独立项目**——任何人拿到这个目录，按 README 配置好环境和 API Key，就能运行一套完整的 OKX 永续合约 AI 自主交易系统。

### 四大定时任务职责定义

```
┌─────────────────────────────────────────────────────────────┐
│  Job A — 快速采集（每 15 分钟）                               │
│  职责：提供最新 15 分钟市场快照                                │
│  · 300+ USDT-M 永续合约 Ticker / 资金费率 / OI               │
│  · 15m K 线 + 技术指标（MA/ATR/RSI/MACD）                    │
│  · 账户余额 / 持仓快照                                       │
│  · OKX 新闻 + 妙想地缘新闻（可配置搜索关键词）                 │
│  · 根据配置的搜索功能进行全网搜索相关数据                       │
│  输出 → market.db / news.db / account.db                     │
├─────────────────────────────────────────────────────────────┤
│  Job E — 慢源采集（每小时）                                   │
│  职责：采集长期数据供决断                                     │
│  · 1H / 4H / 1D K 线 + 技术指标                              │
│  · 跨市场宏观数据（DXY / 黄金 / VIX / SPX / ETF 流向）       │
│  · CoinGecko（BTC 市占率 / 加密总市值 / 24h 交易量）          │
│  · DefiLlama（全链 TVL）                                     │
│  · 币种情绪数据（coin_sentiment）                             │
│  · Regime 计算（trend_up / trend_down / range / low_vol）     │
│  输出 → market.db / news.db                                  │
├─────────────────────────────────────────────────────────────┤
│  Job B — 决策执行（每 15 分钟）                               │
│  职责：根据 A+E 数据进行决断，实时交易                         │
│  · 全币种扫描（300+ 币种 ctVal 过滤 → 五维评分）              │
│  · 实时获取 OKX 账户信息（余额 / 持仓 / algo 订单）           │
│  · 小灵综合推理（80% 裁量 + 20% 五维参考）                    │
│  · 执行交易（开/平/加/减仓 + algo 止损）                      │
│  · 吸取 Job C 复盘经验，调整交易策略                          │
│  · 写入 scoring_history / trade_events / hypotheses           │
│  输出 → account.db / QQ Bot 推送                              │
├─────────────────────────────────────────────────────────────┤
│  Job C — 每日复盘（每日 00:30）                               │
│  职责：短期 + 长期复盘，优化 A/B/E 任务                       │
│  · 验证 JobB 假设（confirmed / falsified / undecided）        │
│  · 交易归因（盈/亏/错失的原因分析）                            │
│  · 五维评分质量评估（各维度命中率/失效场景）                    │
│  · 优化 Job B 策略：更新 playbook / lessons / param_suggestions│
│  · 优化 Job A：缺失数据补充、搜索关键词调整                    │
│  · 优化 Job E：数据源异常检测、采集频率建议                    │
│  · 识别运行异常，写入 error_patterns / missed_opportunities    │
│  输出 → lessons.db / account.db.playbook / self-reviews/      │
└─────────────────────────────────────────────────────────────┘
```

### 任务间数据流

```
Job A (15min) ──→ market.db / news.db / account.db ──┐
                                                      ├──→ Job B (15min) ──→ 交易执行
Job E (1h)    ──→ market.db / news.db              ──┘         │
                                                                │
Job C (daily) ◄── account.db / lessons.db / market.db ◄────────┘
     │
     ├──→ 优化 Job B 策略（playbook / lessons）
     ├──→ 优化 Job A 搜索配置（缺失数据补充）
     └──→ 优化 Job E 采集（异常检测 / 频率建议）
```

---

## 二、项目目录结构

```
E:\OKX\
├── README.md                 # 项目使用说明（新版，含外部工具/技能/配置）
├── REFACTOR_PLAN.md          # 本重构计划
├── skill.md                  # 交易系统最高权威 v3.0
├── config.md                 # 运行时配置 v2.0
├── schema.sql                # 数据库表结构参考
│
├── scripts/                  # 核心脚本（仅 8 个，全部在用）
│   ├── collect_data.py       # Job A 快速采集
│   ├── collect_slow.py       # Job E 慢源采集
│   ├── init_okx2.py          # 数据库初始化
│   ├── health_check.py       # 运行前连通性检测
│   ├── self_review.py        # Job C 复盘脚本
│   ├── _okxcli.py            # OKX CLI 封装
│   ├── _okx_http.py          # OKX HTTP 批量并发封装
│   └── _timeutils.py         # 时间工具
│
├── prompts/                  # Cron Agent Prompt 备份（可读可改）
│   ├── jobb-prompt.md        # Job B 决策执行完整 prompt
│   └── jobc-prompt.md        # Job C 复盘完整 prompt
│
├── reports/                  # 报告输出目录
│   ├── jobb/                 # JobB 每轮决策报告
│   ├── self-reviews/         # JobC 每日自省报告
│   └── daily/                # 日报备份
│
└── docs/                     # 项目文档
    ├── architecture.md       # 系统架构说明
    ├── data-flow.md          # 数据流与存储模式说明
    ├── cron-jobs.md          # 定时任务配置方法
    ├── risk-limits.md        # 风控硬上限详细说明
    └── trading-summary.md    # 最近交易总结与实战经验
```

---

## 三、重构步骤

### Step 1 — 创建 E:\OKX\ 目录结构 + 复制核心脚本

| 操作 | 文件 |
|------|------|
| 创建目录 | `E:\OKX\scripts\` `E:\OKX\prompts\` `E:\OKX\reports\jobb\` `E:\OKX\reports\self-reviews\` `E:\OKX\reports\daily\` `E:\OKX\docs\` |
| 复制 8 个核心脚本 | `collect_data.py` `collect_slow.py` `init_okx2.py` `health_check.py` `self_review.py` `_okxcli.py` `_okx_http.py` `_timeutils.py` |
| 复制参考文件 | `schema.sql` |

### Step 2 — 重构 skill.md v3.0

基于 v2.1.2 迭代更新，主要变更：

| 变更项 | 说明 |
|--------|------|
| 项目根目录 | `E:\OKX\` |
| 品种范围 | 全币种扫描（300+ USDT-M 永续合约），无白名单 |
| 全币种扫描 | 新增 §8 全币种扫描要求（ctVal 过滤 → 批量评分 → TOP 20 → 深度分析 TOP 3） |
| 数据目录 | `E:\OKX\db\`（可配置，见 config.md） |
| 四大 Job 职责 | 按本计划 §一 定义写入 |
| 废弃概念清理 | 移除"动态阈值 32 分"等已废弃描述，统一为"小灵 80% 裁量" |
| 数据流架构图 | 新增采集 → DB → 决策 → 交易 → 复盘闭环图 |
| Job C 优化职责 | 明确 C 可优化 A/B/E 的策略、搜索配置、采集频率 |

### Step 3 — 重构 config.md v2.0

| 变更项 | 说明 |
|--------|------|
| 项目根目录 | `E:\OKX\` |
| 数据目录 | `E:\OKX\db\`（可配置） |
| Cron 表达式 | 与当前实际同步（A: `1,16,31,46` / E: `10 * * * *` / B: `5,20,35,50` / C: `30 0 * * *`） |
| Job 职责描述 | 按本计划 §一 更新 |
| 搜索配置 | 新增 Job A 可配置全网搜索关键词说明 |

### Step 4 — 编写新 README.md

**核心要求：外部工具、技能、配置必须写清楚。**

README 大纲：

```
1. 项目简介
   - 小灵 AI 交易员 + OKX USDT-M 永续合约全自动自主交易
   - 可迁移、可分享：拿到目录 + 配置环境 = 可运行

2. 快速开始
   - Step 1: 安装运行环境（Python / pwsh / Node.js / OKX CLI）
   - Step 2: 配置 API Key（OKX / FRED / CoinGecko / 妙想）
   - Step 3: 初始化数据库
   - Step 4: 健康检查
   - Step 5: 启动定时任务

3. 外部工具与技能（★ 必须写清楚）
   3.1 OKX CLI（@okx_ai/okx-trade-cli）
       - 安装：npm install -g @okx_ai/okx-trade-cli
       - 配置：okx config init（API Key / Secret / Passphrase）
       - 用途：行情查询、账户查询、下单、止损
   3.2 OpenClaw Cron
       - 配置方法：通过 OpenClaw cron 工具设置定时任务
       - 每个 Job 的 cron 表达式、payload、session 配置
   3.3 OKX CLI 技能路由
       - okx-cex-market：行情 / K线 / 深度 / 资金费率 / OI
       - okx-cex-portfolio：余额 / 持仓 / PnL
       - okx-cex-trade：下单 / 撤单 / 改单 / 止损止盈
       - okx-cex-bot：Grid / DCA Bot
   3.4 外部数据源
       - FRED API：DXY / VIX / S&P500
       - CoinGecko API：BTC 市占率 / 加密总市值
       - DefiLlama API：全链 TVL
       - 妙想 API（mx-search）：地缘政治新闻
   3.5 QQ Bot 推送
       - 配置方法
       - 推送目标 ID

4. 目录结构

5. 系统架构
   - 采集层（A/E）→ 数据库层 → 决策层（B）→ 执行层 → 复盘层（C）
   - 四大 Job 职责定义

6. 数据流说明
   - 存储模式：增量追加（时间序列）
   - 各表增长估算
   - 数据新鲜度要求

7. 定时任务配置
   - 每个 Job 的 cron 表达式、触发方式、脚本/agent
   - OpenClaw cron 配置示例

8. 核心脚本说明
   - 每个脚本的用途、参数、依赖

9. 数据库说明
   - 4 库 26+ 表的核心表用途

10. 风控硬上限

11. 交易总结
    - 近期实战数据
    - 关键教训

12. 故障排查
```

### Step 5 — 编写 docs/ 文档

| 文件 | 内容 |
|------|------|
| `architecture.md` | 系统五层架构图 + 四大 Job 交互关系 |
| `data-flow.md` | 数据采集 → 入库 → 读取 → 决策 → 写入闭环；存储模式说明 |
| `cron-jobs.md` | OpenClaw cron 配置方法；每个 Job 的完整配置示例 |
| `risk-limits.md` | 风控硬上限详细说明 + 计算公式 |
| `trading-summary.md` | 近期交易总结、5 笔已平仓记录、关键教训 |

### Step 6 — 导出 Cron Prompt 到 prompts/

| 文件 | 来源 | 说明 |
|------|------|------|
| `jobb-prompt.md` | Job B cron payload.message | 决策执行完整 prompt（含全币种扫描） |
| `jobc-prompt.md` | Job C cron payload.message | 每日复盘完整 prompt |

### Step 7 — 写入交易总结到 docs/trading-summary.md

- 账户净值变化（$768 → $1,221 USDT）
- 5 笔已平仓交易记录
- 关键教训：
  - position_state DB vs OKX live 不一致 → 必须先查 live
  - CPI week restraint 4 次验证有效
  - SOL 1H ATR 压缩规律
  - 全币种扫描上线（2026-05-11）
  - cross_market / coin_sentiment 数据源需监控

---

## 四、执行顺序与耗时

| 阶段 | 步骤 | 预计耗时 |
|------|------|---------|
| Phase 1 | 创建目录 + 复制核心脚本 | 10 min |
| Phase 2 | 重构 skill.md v3.0 | 20 min |
| Phase 3 | 重构 config.md v2.0 | 10 min |
| Phase 4 | 编写 README.md（含外部工具/技能/配置） | 25 min |
| Phase 5 | 编写 docs/ 5 个文档 | 25 min |
| Phase 6 | 导出 Cron Prompt | 5 min |
| Phase 7 | 写入交易总结 | 10 min |
| **总计** | | **~105 min** |

---

## 五、确认事项

| # | 事项 | 决定 |
|---|------|------|
| 1 | E:\OKX\ 作为可迁移可分享的独立项目 | ✅ 确认 |
| 2 | 旧脚本归档在原位，不迁移到新目录 | ✅ 确认 |
| 3 | skill.md 升为 v3.0 | ✅ 确认 |
| 4 | 不需要同步更新 Cron 配置 | ✅ 确认（Cron 仍指向原路径） |
| 5 | API Key 不迁移到环境变量 | ✅ 确认（保留在 config.md） |
| 6 | README 必须写清外部工具/技能/配置 | ✅ 确认 |
