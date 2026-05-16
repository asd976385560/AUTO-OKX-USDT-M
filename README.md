# OKX 永续合约自主交易系统 v3.1

> 小灵（AI 交易员）在 OKX 永续合约上的全自动自主交易系统。
> **可迁移、可分享**——拿到这个目录，配置好环境和 API Key，即可运行。
> 全币种扫描（300+ 合约），五维评分为参考锚点，小灵拥有最大裁量权。
> 所有交易数据、状态、经验全部写入数据库。

---

## 快速开始

git clone https://github.com/asd976385560/AUTO-OKX-USDT-M.git
填写 config.md 必填项

⚠️使用openclaw/qclaw等等个人智能助手
⚠️聊天窗口发送：读取分析E:\OKX  目录下skill.md，config.md，README.md，按照skill里面内容部署OKX 永续合约自主交易系统,部署完成后进行测试，缺少key或者需要我决定的，最后列个表。

**⚠️最好使用opus 4.7或者gpt-5.5安装，其他的大模型，装好后再让他测试一遍。**
**⚠️注意：config.md文件里面需要填一些配置，像指令E:\OKX这些目录按实际的修改。**

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
