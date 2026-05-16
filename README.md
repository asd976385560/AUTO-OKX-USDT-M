# OKX 永续合约自主交易系统 v3.1

> 小灵（AI 交易员）在 OKX 永续合约上的全自动自主交易系统。
> **可迁移、可分享**——拿到这个目录，配置好环境和 API Key，即可运行。
> 全币种扫描（300+ 合约），五维评分为参考锚点，小灵拥有最大裁量权。
> 所有交易数据、状态、经验全部写入数据库。

---

## 快速开始
⚠️使用openclaw/qclaw等等个人智能助手
⚠️聊天窗口发送：读取分析E:\OKX  目录下skill.md，config.md，README.md，按照skill里面内容部署OKX 永续合约自主交易系统,部署完成后进行测试，缺少key或者需要我决定的，最后列个表。
⚠️最好使用opus 4.7或者gpt-5.5安装，其他的大模型，装好后再让他测试一遍。
⚠️注意：config.md文件里面需要填一些配置，像指令E:\OKX这些目录按实际的修改。

### Step 1 — 安装运行环境

```bash
# 1. Python 3.14+（内置 sqlite3）
#    https://www.python.org/downloads/

# 2. pwsh 7+（PowerShell Core）— 禁止使用 PS 5.1
#    https://github.com/PowerShell/PowerShell/releases

# 3. Node.js 18+
#    https://nodejs.org/

# 4. OKX CLI
npm install -g @okx_ai/okx-trade-cli
okx config init
# 配置 API Key / Secret / Passphrase，profile = live

# 5. OpenClaw（定时任务调度）
#    https://docs.openclaw.ai
```

### Step 2 — 配置 API Key

编辑 `config.md`，填写以下必填项：

| # | 必填项 | 说明 | 获取方式 |
|---|--------|------|---------|
| 1 | 数据库目录 | SQLite 存放路径 | 本地创建，如 `E:\OKX\db` |
| 2 | FRED API Key | 美联储经济数据 | https://fred.stlouisfed.org/docs/api/api_key.html |
| 3 | CoinGecko API Key | 加密市场宏观数据 | https://www.coingecko.com/en/api |
| 4 | 妙想 API Key | 东方财富资讯 | 联系妙想平台获取 |
| 5 | QQ Bot 推送目标 | QQ C2C 目标 ID | OpenClaw QQ Bot 配置 |

### Step 3 — 初始化数据库

```bash
python scripts/init_okx2.py --root E:\OKX --db-dir <数据库目录>
```

### Step 4 — 健康检查

```bash
python scripts/health_check.py
okx config show
okx account balance --profile live
```

### Step 5 — 启动定时任务

通过 OpenClaw Cron 配置四大定时任务（详见下方 §定时任务配置）。

---

## 外部工具与技能

### 1. OKX CLI（`@okx_ai/okx-trade-cli`）

**安装**：
```bash
npm install -g @okx_ai/okx-trade-cli
```

**配置**：
```bash
okx config init
# 输入 API Key / Secret / Passphrase
# 选择 profile = live
```

**用途**：
| 命令 | 用途 | 使用场景 |
|------|------|---------|
| `okx market instruments --instType SWAP` | 获取所有永续合约列表 | 品种发现（300+ 合约） |
| `okx market ticker --instId BTC-USDT-SWAP` | 获取单个合约行情 | 单币种查询 |
| `okx account balance --profile live` | 账户余额 | Job A / Job B |
| `okx account positions --instType SWAP --profile live` | 当前持仓 | Job A / Job B |
| `okx swap place --profile live ...` | 下单 | Job B 交易执行 |
| `okx swap close-all --profile live ...` | 全部平仓 | 紧急平仓 |
| `okx algo order --profile live ...` | 挂 algo 止损 | Job B 开仓后必挂 |

**验证**：
```bash
okx config show              # 确认凭证
okx account balance --profile live  # 确认余额
```

### 2. OpenClaw Cron（定时任务调度）

**配置方法**：通过 OpenClaw 的 `cron` 工具设置定时任务。

每个 Job 的配置包括：
- `schedule`：Cron 表达式（如 `5,20,35,50 * * * *`）
- `payload.kind`：`agentTurn`（Agent 执行）
- `payload.message`：Agent prompt（见 `prompts/` 目录）
- `sessionTarget`：`isolated`（独立会话）
- `delivery`：QQ Bot 推送配置

**配置示例**：
```json
{
  "name": "OKX-JobA-快速采集",
  "schedule": { "kind": "cron", "expr": "1,16,31,46 * * * *" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "运行 OKX Job A 快速采集: py E:\\OKX\\scripts\\collect_data.py --profile live --db-root E:\\OKX\\db",
    "model": "MiniMax-M2.7-highspeed",
    "lightContext": true
  }
}
```

### 3. OKX CLI 技能路由（OpenClaw 技能）

以下技能由 OpenClaw 管理，为 OKX CLI 提供高级封装：

| 技能 | 用途 | 安装 |
|------|------|------|
| `okx-cex-market` | 行情 / K线 / 深度 / 资金费率 / OI / 技术指标 | `clawhub install okx-cex-market` |
| `okx-cex-portfolio` | 余额 / 持仓 / P&L / 费率 / 转账 | `clawhub install okx-cex-portfolio` |
| `okx-cex-trade` | 下单 / 撤单 / 改单 / 止损止盈 | `clawhub install okx-cex-trade` |
| `okx-cex-bot` | Grid 网格 / DCA 马丁策略 | `clawhub install okx-cex-bot` |
| `mx-search` | 妙想地缘政治新闻搜索 | `clawhub install mx-search` |

**注意**：这些技能是 OpenClaw 生态的一部分，需要在 OpenClaw 环境中运行。独立运行采集脚本（`collect_data.py`）不需要这些技能，脚本通过 `_okx_http.py` 直接调用 OKX API。

### 4. 外部数据源

| 数据源 | 用途 | API Key | 免费额度 |
|--------|------|---------|---------|
| **FRED** | DXY / VIX / S&P500 + 日变化率 | config.md §4.1 | 120 req/min |
| **DefiLlama** | 全链 TVL | 无需 | 无限 |
| **CoinGecko** | BTC 市占率 / 加密总市值 / 24h 交易量 | config.md §4.3 | 30 req/min |
| **妙想（mx-search）** | 地缘政治新闻 | config.md §4.4 | 按配额 |

**配置**：所有 Key 填入 `config.md` 对应位置。

### 5. QQ Bot 推送

| 项目 | 值 |
|------|-----|
| 推送通道 | QQ C2C |
| 目标 ID | config.md §8 |
| 推送内容 | Job B 每轮决策要点、Job C 每日复盘摘要 |
| 配置方式 | OpenClaw `cron` 工具的 `delivery` 字段 |

---

## 目录结构

```
E:\OKX\
├── README.md              # 本文件
├── REFACTOR_PLAN.md       # 重构计划
├── skill.md               # 交易系统最高权威 v3.1
├── config.md              # 运行时配置 v3.1
├── schema.sql             # 数据库表结构参考
│
├── scripts/               # 核心脚本（仅 8 个）
│   ├── collect_data.py    # Job A 快速采集（300+ 币种 Ticker/Funding/K线/账户/新闻）
│   ├── collect_slow.py    # Job E 慢源采集（1H/4H 每小时，1D/1W/1M 分频 + 跨市场宏观 + 情绪）
│   ├── init_okx2.py       # 数据库初始化（幂等）
│   ├── health_check.py    # 运行前连通性检测
│   ├── self_review.py     # Job C 每日复盘
│   ├── _okxcli.py         # OKX CLI 封装
│   ├── _okx_http.py       # OKX HTTP 批量并发封装
│   └── _timeutils.py      # 时间工具
│
├── prompts/               # Cron Agent Prompt 备份
│   ├── jobb-prompt.md     # Job B 决策执行完整 prompt
│   └── jobc-prompt.md     # Job C 复盘完整 prompt
│
├── reports/               # 报告输出
│   ├── jobb/              # JobB 每轮决策报告
│   ├── self-reviews/      # JobC 每日自省报告
│   └── daily/             # 日报备份
│
└── docs/                  # 项目文档
    ├── architecture.md    # 系统架构
    ├── data-flow.md       # 数据流与存储模式
    ├── cron-jobs.md       # 定时任务配置方法
    ├── risk-limits.md     # 风控硬上限
    └── trading-summary.md # 交易总结
```

---

## 系统架构

### 五层架构

```
采集层（Job A/E）→ 数据库层 → 决策层（Job B）→ 执行层 → 复盘层（Job C）
```

### 四大定时任务

| 任务 | 频率 | 职责 |
|------|------|------|
| **Job A** | 每 15 分钟 | 提供最新 15 分钟市场快照（300+ 币种 Ticker / 资金费率 / 15m K线 / 账户 / 新闻） |
| **Job E** | 每 1 小时 | 采集长期数据（1H/4H/1D K线 / DXY / 黄金 / VIX / SPX / ETF / 情绪 / Regime） |
| **Job B** | 每 15 分钟 | 全币种扫描 → 五维评分 → 小灵推理 → 交易执行（实时获取账户信息） |
| **Job C** | 每日 00:30 | 短期+长期复盘 → 优化 A/B/E 策略 → 补充缺失数据 → 解决运行异常 |

### 数据流

```
Job A (15min) ──→ market.db / news.db / account.db ──┐
                                                      ├──→ Job B (15min) ──→ 交易执行
Job E (1h)    ──→ market.db / news.db              ──┘         │
                                                                │
Job C (daily) ◄── account.db / lessons.db / market.db ◄────────┘
     │
     ├──→ 优化 Job B 策略
     ├──→ 优化 Job A 搜索配置
     └──→ 优化 Job E 采集
```

---

## 定时任务配置

### OpenClaw Cron 配置

| 任务 | Cron 表达式 | 触发方式 | 脚本/Prompt |
|------|------------|---------|-----------|
| Job A | `1,16,31,46 * * * *` | `isolated agentTurn` | `py E:\OKX\scripts\collect_data.py --profile live --db-root E:\OKX\db` |
| Job E | `10 * * * *` | `isolated agentTurn` | `py E:\OKX\scripts\collect_slow.py --db-root E:\OKX\db` |
| Job B | `5,20,35,50 * * * *` | `isolated agentTurn` | 见 `prompts/jobb-prompt.md` |
| Job C | `30 0 * * *` | `isolated agentTurn` | 见 `prompts/jobc-prompt.md` |

> 错开设计：A 在 :01/:16/:31/:46 采集 → B 在 :05/:20/:35/:50 决策（给 A 4 分钟完成写入）→ E 在 :10 慢源采集

### 配置参数

| 任务 | model | thinking | lightContext | timeout |
|------|-------|----------|-------------|---------|
| Job A | MiniMax-M2.7-highspeed | off | true | 666s |
| Job E | MiniMax-M2.7-highspeed | off | true | 999s |
| Job B | （默认模型） | off | false | 888s |
| Job C | （默认模型） | off | false | 999s |

---

## 核心脚本说明

| 脚本 | 用途 | 参数 | 依赖 |
|------|------|------|------|
| `collect_data.py` | Job A 快速采集 | `--profile live --db-root <DB>` | _okxcli.py, _okx_http.py, _timeutils.py |
| `collect_slow.py` | Job E 慢源采集 | `--db-root <DB> [--force-all-timeframes]` | _okxcli.py, _okx_http.py, _timeutils.py |
| `init_okx2.py` | 数据库初始化 | `--root <ROOT> --db-dir <DB>` | — |
| `health_check.py` | 健康检查 | — | config.md |
| `self_review.py` | Job C 复盘 | `--db-root <DB>` | — |
| `_okxcli.py` | OKX CLI 封装 | — | okx CLI |
| `_okx_http.py` | OKX HTTP 封装 | — | requests |
| `_timeutils.py` | 时间工具 | — | — |

---

## 数据库说明

4 个 SQLite 数据库，26+ 张核心表：

| 库 | 核心表 | 说明 |
|----|--------|------|
| market.db | tick_snapshots | 300+ 币种 Ticker 快照（每轮 300+行） |
| market.db | kline_cache | K 线 + 指标（15m/1H/4H/1D） |
| market.db | cross_market | 跨市场宏观快照 |
| market.db | derivatives | 资金费率 / OI / 溢价 |
| news.db | news_items | 新闻事件（哈希去重） |
| news.db | coin_sentiment | 币种情绪 |
| account.db | 全部 | 账户/持仓/评分/事件/报告/系统状态/经验库 |
| lessons.db | 全部 | 信号表现/错判模式/参数建议/错失机会 |

**存储模式**：增量追加（时间序列），非全量覆盖。详见 `docs/data-flow.md`。

---

## 风控硬上限

触碰任一条 → 立即停手 + 进入 PAUSE，需主人手动恢复。

| 限制项 | 默认推荐 | 硬上限 |
|--------|---------|--------|
| 单笔仓位占净值 | ≤ 5% | ≤ **10%** |
| 杠杆（BTC/ETH） | 10x | ≤ **10x** |
| 杠杆（山寨） | 5x | ≤ **10x** |
| 同侧暴露 | ≤ 50% | ≤ **60%** |
| 并发持仓 | ≤ 3 仓 | ≤ **6 仓** |
| 单次开仓保证金 | — | ≤ **10% 净值** |

---

## 常用命令

```bash
# 健康检查
python scripts/health_check.py

# 初始化
python scripts/init_okx2.py --root E:\OKX --db-dir E:\OKX\db

# 手动触发 Job A
python scripts/collect_data.py --profile live --db-root E:\OKX\db

# 手动触发 Job E
python scripts/collect_slow.py --db-root E:\OKX\db

# 验证 OKX CLI
okx config show
okx account balance --profile live
```

---

## 故障排查

| 问题 | 排查 |
|------|------|
| 初始化中断 | 检查 `config.md` 必填项是否全部填写 |
| `okx config show` 无输出 | 运行 `okx config init` |
| 健康检查某接口失败 | 检查网络 / VPN / API Key |
| 数据库表缺失 | 重新运行 `init_okx2.py`（幂等） |
| Job B 只评 BTC/ETH/SOL | 检查 prompt 中是否有全币种扫描要求 |
| cross_market 数据 stale | 检查 Job E 是否正常运行 |
| coin_sentiment 数据 stale | 检查 Job E 采集脚本 |

---

## 版本

| 版本 | 日期 | 说明 |
|------|------|------|
| v2.1.2 | 2026-04-23 | 旧版（仅 BTC/ETH/SOL） |
| **v3.0** | 2026-05-11 | 全币种扫描；项目独立化；外部工具文档化；四大 Job 重定义 |
| **v3.1** | 2026-05-15 | 精简最高权威与 prompt；统一 stale 规则；Job E 长周期分频采集 |
