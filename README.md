# OKX 永续合约自主交易系统 v3.1

> 小灵（AI 交易员）在 OKX USDT-M 永续合约上的自动化交易系统。
> 核心流程为：数据采集 -> 数据库存储 -> Agent 决策 -> 交易执行 -> 每日复盘。

本仓库面向两类使用方式：

1. 本地脚本运行：手动初始化、健康检查、触发采集和复盘脚本。
2. OpenClaw 定时运行：用 Cron 唤醒 Job A / B / C / E，形成完整自动化闭环。

如果你是第一次接触这个项目，先看 [skill.md](skill.md) 了解规则边界，再看 [config.md](config.md) 填配置，最后按本文完成部署。

---

## 项目概览

### 系统能力

- 市场范围：OKX 全部 USDT-M 永续合约。
- 运行模式：全自动自主运行，默认实盘 profile = live。
- 决策逻辑：五维评分提供参考锚点，最终由 Agent 综合裁量。
- 数据落盘：行情、账户、持仓、评分、交易事件、经验库全部写入 SQLite。

### 四大 Job

| Job | 频率 | 职责 | 入口 |
|-----|------|------|------|
| Job A | 每 15 分钟 | 快速采集行情、账户、新闻、15m 数据 | `scripts/collect_data.py`（当前占位，需补齐后启用） |
| Job E | 每 1 小时 | 慢源采集 1H/4H/1D/1W/1M、宏观与情绪 | `scripts/collect_slow.py` |
| Job B | 每 15 分钟 | 全币种扫描、推理、下单、写交易痕迹 | `prompts/jobb-prompt.md` |
| Job C | 每日 00:30 | 复盘、归因、参数建议、经验更新 | `scripts/self_review.py` + `prompts/jobc-prompt.md` |

### 数据流

```text
Job A / Job E -> market.db / news.db / account.db -> Job B -> trade_events / scoring_history
Job C <- account.db / lessons.db / market.db / news.db <- Job B 运行痕迹
```

---

## 运行要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10+ |
| Python | 3.14+ |
| PowerShell | pwsh 7+，不要使用 Windows PowerShell 5.1 |
| Node.js | 18+ |
| OKX CLI | `npm install -g @okx_ai/okx-trade-cli` |
| OpenClaw | 最新版，用于 Cron 自动调度 |
| 网络 | 能访问 `okx.com`、FRED、CoinGecko、DefiLlama |

---

## 部署前准备

### 1. 约定项目路径

本文统一使用以下占位符：

- `<PROJECT_ROOT>`：项目根目录，例如 `D:\AUTO-OKX-USDT-M`
- `<DB_DIR>`：数据库目录，例如 `D:\AUTO-OKX-USDT-M\db`

如果你沿用其他路径，所有命令中的目录都要同步替换，不要继续照抄 `E:\OKX`。

### 2. 安装依赖

```powershell
# 安装 OKX CLI
npm install -g @okx_ai/okx-trade-cli

# 验证基础工具
python --version
pwsh --version
node --version
okx --version
```

### 3. 配置 OKX CLI

```powershell
okx config init
okx config show
okx account balance --profile live
```

要求：

- profile 使用 `live`
- 能正常返回余额
- 凭证保存在用户目录的 `.okx/config.toml`，不在项目仓库内

### 4. 填写 config.md 必填项

首次运行前，至少补齐 [config.md](config.md) 里的以下内容：

| 项目 | 用途 |
|------|------|
| 数据库目录 | SQLite 落盘位置 |
| FRED API Key | 宏观数据 |
| CoinGecko API Key | 市场宏观与 ETF 代理数据 |
| 妙想 API Key | 新闻与地缘数据 |
| QQ Bot 推送目标 | Job B / Job C 通知 |

---

## 首次部署

推荐按下面的顺序执行，一步一步确认，不要直接跳到定时任务。

### Step 1. 初始化数据库与目录

```powershell
Set-Location <PROJECT_ROOT>
python scripts/init_okx2.py --root <PROJECT_ROOT> --db-dir <DB_DIR>
```

这个脚本会做三件事：

1. 检查 [config.md](config.md) 的必填项是否已填写。
2. 创建数据库目录、报告目录等运行目录。
3. 初始化 `market.db`、`news.db`、`account.db`、`lessons.db`。

### Step 2. 执行健康检查

```powershell
Set-Location <PROJECT_ROOT>
python scripts/health_check.py
```

健康检查通过后，应至少确认：

- OKX API 可访问
- FRED / DefiLlama / CoinGecko 可访问
- Python 自带 SQLite 可用
- OKX CLI 可执行

### Step 3. 手动跑一次 Job E

```powershell
Set-Location <PROJECT_ROOT>
python scripts/collect_slow.py --db-root <DB_DIR>
```

如果你想首次一次性补齐所有长周期时间框架，可改为：

```powershell
python scripts/collect_slow.py --db-root <DB_DIR> --force-all-timeframes
```

### Step 4. 手动跑一次 Job C

```powershell
Set-Location <PROJECT_ROOT>
python scripts/self_review.py --db-root <DB_DIR>
```

如果要复盘指定日期：

```powershell
python scripts/self_review.py --date 2026-05-15 --db-root <DB_DIR>
```

---

## 使用说明

### 1. 手动运行模式

适合首次部署、联调和故障排查。

常用命令：

```powershell
# 初始化
python scripts/init_okx2.py --root <PROJECT_ROOT> --db-dir <DB_DIR>

# 健康检查
python scripts/health_check.py

# Job E：慢源采集
python scripts/collect_slow.py --db-root <DB_DIR>

# Job E：强制全周期采集
python scripts/collect_slow.py --db-root <DB_DIR> --force-all-timeframes

# Job C：每日复盘
python scripts/self_review.py --db-root <DB_DIR>

# OKX CLI 检查
okx config show
okx account balance --profile live
okx account positions --instType SWAP --profile live
```

### 2. OpenClaw 自动运行模式

完整自动化依赖 OpenClaw Cron。当前推荐的错峰执行为：

| 任务 | Cron | 说明 |
|------|------|------|
| Job A | `1,16,31,46 * * * *` | 快速采集 |
| Job B | `5,20,35,50 * * * *` | 决策与执行 |
| Job E | `10 * * * *` | 慢源采集 |
| Job C | `30 0 * * *` | 每日复盘 |

注意：当前仓库里的 `scripts/collect_data.py` 是空文件，Job A 需要先补齐实现再接入 Cron。未补齐前，可以先联调 Job E、Job C 和 OKX CLI 连通性。

调度原则：

- 先采集，后决策。
- Job A 与 Job B 至少错开 4 分钟，给数据库写入留时间。
- Job C 只做复盘，不允许任何交易动作。

Job A 的 OpenClaw 示例：

```json
{
  "name": "OKX-JobA-快速采集",
  "schedule": { "kind": "cron", "expr": "1,16,31,46 * * * *" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "运行 OKX Job A 快速采集: py <PROJECT_ROOT>\\scripts\\collect_data.py --profile live --db-root <DB_DIR>",
    "model": "MiniMax-M2.7-highspeed",
    "lightContext": true
  }
}
```

### 3. 与 AI 助手协作部署

如果你用 OpenClaw、qclaw 或其他个人助手协助部署，建议让它同时读取：

1. [skill.md](skill.md)
2. [config.md](config.md)
3. [README.md](README.md)

这样它能同时拿到规则边界、运行配置和部署流程，避免只按单个文件做错误假设。

---

## 核心脚本

| 脚本 | 用途 | 关键参数 |
|------|------|----------|
| `scripts/init_okx2.py` | 初始化目录与数据库 | `--root`、`--db-dir`、`--skip-config-check` |
| `scripts/health_check.py` | 启动前连通性检查 | 无 |
| `scripts/collect_data.py` | Job A 快速采集 | 当前为空文件，补齐后应支持 `--profile live --db-root <DB_DIR>` |
| `scripts/collect_slow.py` | Job E 慢源采集 | `--db-root`、`--force-all-timeframes` |
| `scripts/self_review.py` | Job C 每日复盘 | `--date`、`--db-root`、`--okx-root` |

说明：

- `init_okx2.py` 是幂等的，数据库缺表或目录缺失时可以重复执行。
- `collect_slow.py` 当前支持按小时选择 1D/1W/1M，首次联调建议加 `--force-all-timeframes`。
- `self_review.py` 默认复盘昨天的数据。

---

## 数据与目录

### 目录结构

```text
<PROJECT_ROOT>
├── README.md
├── skill.md
├── config.md
├── schema.sql
├── scripts/
├── prompts/
├── reports/
└── docs/
```

### 核心数据库

| 数据库 | 作用 |
|--------|------|
| `market.db` | 行情、K 线、衍生品、跨市场宏观 |
| `news.db` | 新闻事件、币种情绪 |
| `account.db` | 账户、持仓、评分、交易事件、系统状态 |
| `lessons.db` | 经验库、错判模式、参数建议、复盘结果 |

存储模式为时间序列增量追加，详细表结构见 [schema.sql](schema.sql) 和 [docs/data-flow.md](docs/data-flow.md)。

---

## 风控硬上限

以下限制来自 [skill.md](skill.md)，不能被 prompt、学习结果或临时判断放宽：

| 限制项 | 默认推荐 | 硬上限 |
|--------|----------|--------|
| 单笔仓位占净值 | <= 5% | <= 10% |
| 单次开仓保证金 | - | <= 10% 净值 |
| 杠杆（BTC/ETH） | 10x | <= 10x |
| 杠杆（山寨） | 5x | <= 10x |
| 同侧暴露 | <= 50% | <= 60% |
| 并发持仓 | <= 3 仓 | <= 6 仓 |

任一条被突破，都应立即进入 PAUSE，而不是继续尝试开仓。

---

## 常见问题

| 问题 | 处理方式 |
|------|----------|
| 初始化时报配置未填写 | 回到 [config.md](config.md) 补齐必填项，再重跑 `init_okx2.py` |
| `okx account balance --profile live` 失败 | 重新执行 `okx config init`，确认是 `live` profile |
| 健康检查访问失败 | 先查网络，再查 API Key 是否有效 |
| 数据库表缺失 | 重新执行 `python scripts/init_okx2.py --root <PROJECT_ROOT> --db-dir <DB_DIR>` |
| Job E 没有写入长周期数据 | 用 `--force-all-timeframes` 做一次全量补齐 |
| Job C 没有输出复盘 | 检查 `--okx-root` 指向的运行目录是否存在记录文件 |

---

## 参考文档

- [skill.md](skill.md)：最高权威，定义硬上限、Job 流程和学习边界
- [config.md](config.md)：运行配置、API、路径与 Cron 参数
- [schema.sql](schema.sql)：数据库表结构参考
- [docs/architecture.md](docs/architecture.md)：系统架构
- [docs/cron-jobs.md](docs/cron-jobs.md)：定时任务配置
- [docs/risk-limits.md](docs/risk-limits.md)：风控硬上限详解
- [docs/trading-summary.md](docs/trading-summary.md)：交易总结

---

## 版本历史

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-05-11 | v3.0 | 项目独立化；全币种扫描；四大 Job 重定义 |
| 2026-05-15 | v3.1 | 精简最高权威文件；统一 stale 规则；Job E 长周期分频采集 |
