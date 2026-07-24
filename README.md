# AUTO-OKX-USDT-M

V2.0 是一个面向 OKX USDT 永续合约的自主交易系统代码库。它将市场采集、风控、下单、记账、推送和阶段派发放在确定性代码中，将分析、交易判断、复盘和无 API 新闻取数交给独立 Agent。系统同时支持 live 与 demo；两者共用同一套硬风控和止损要求，只切换执行环境。

> 风险提示：本项目包含真实交易执行能力。首次部署应保持 `OKX_EXECUTOR_DRYRUN=1` 和 `OKX_TRIGGER_DRYRUN=1`，在隔离数据库中完成验证后再单独评估是否启用外部调用。本仓库不提供投资建议，也不保证盈利。

## 公开发布边界

公开版本只包含源码、角色规则、模板、数据库 DDL、配置示例和公开文档，不包含：

- `config.md`、`.env*` 或任何真实凭证；
- SQLite 运行库、WAL/SHM、日志、报告、记忆和临时文件；
- QQ/Webhook 真实目标、账户、设备、主机或用户标识；
- 本地依赖目录 `Lib/`、缓存和字节码；
- 内部宿主运维资料、历史归档和一次性兼容脚本。

复制 `config.example.md` 为本地的 `config.md` 后再填写。`config.md` 已被 `.gitignore` 强制排除。凭证读取以环境变量为优先级最高的来源；公开代码中没有真实值或可用默认凭证。

## 版本线说明

当前源码事实源自称 `V2.0`。GitHub 远端旧 README 曾使用 `v3.1` 标识，两者不是在本次同步中自动推导出的可比较语义版本。本次只同步并脱敏 V2.0 代码，不创建标签或 Release；最终公开版本号由仓库维护者另行决定。

## 架构

```text
OpenClaw cron
  ├─ fast_collect.py ───────────────┐
  ├─ slow_collect.py ───────────────┼─> ledger.db
  └─ sources/news_collect.py ───────┘
                                      │
                                      v
                              core/dispatcher.py
                                      │
                    stage_dispatch 唯一键幂等闩锁
                                      │
                 ┌────────────────────┴───────────────────┐
                 v                                        v
       unified live trader                         demo trader
       analysis + live execution                   demo execution
                 │                                        │
                 └───────────────┬────────────────────────┘
                                 v
                     scripts/push_pipeline.py
                     render -> validate -> archive/send
```

核心不变量：

- `skill.md` 是 V2.0 业务事实源，本文是公开系统地图；
- `ledger.db.stage_dispatch(cycle_id, stage)` 是阶段派发幂等真值；
- 每张表或明确键域只有一个权威 writer，读者使用 SQLite `mode=ro`；
- live 开仓只经 `core/order_executor.py`，内部强制调用 `core/risk_validator.py`；
- live/demo 都要求止损，并共用同一套硬上限；
- 当前持仓以 OKX API 为准，不能由本地快照推断；
- push 固定走 `scripts/push_pipeline.py`，不由 Agent 临时拼接执行链。

## 目录

```text
agents/       Agent 角色规则与公开 persona 源
collectors/   市场、新闻、账户采集，writer 与 ledger
core/         风控、订单执行、决策卡与 dispatcher
db/           仅公开 schema.sql；运行数据库不会进入 Git
docs/         公开文档索引
scripts/      查询、维护、迁移、报告与推送工具
templates/    分析、交易、推送和日报模板
```

`docs/archive/`、`scripts/archive/` 以及发布排除清单中的内部/兼容文件会保留在本地工作区，但不进入公开分支。当前代码没有完整 `tests/` 目录；不要引用旧文档中的历史测试数量。

## 运行依赖

- Python 3.11 或更高版本；
- `httpx`；
- SQLite（Python 标准库）；
- 需要 Agent 调度时安装并配置 OpenClaw；
- 需要实际下单时安装并在仓库外配置 OKX CLI profile；
- PowerShell 7（Windows 包装器与运维脚本）。

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

项目根目录优先读取 `OKX_ROOT`；未设置时，Python 代码从当前文件位置推导根目录。包装器优先读取 `OKX_PYTHON_BIN`，否则使用 `PATH` 中的 Python。公开版不包含 `Lib/`。

## 配置

```powershell
Copy-Item config.example.md config.md
$env:OKX_ROOT = (Resolve-Path .).Path
$env:OKX_PYTHON_BIN = (Get-Command python).Source
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'
```

主要环境变量：

| 变量 | 用途 |
|---|---|
| `OKX_ROOT` | 项目根目录；未设置时从源码位置推导 |
| `OKX_DB_ROOT` | 数据库目录；默认 `<PROJECT_ROOT>/db` |
| `OKX_PYTHON_BIN` | Python 可执行文件 |
| `OKX_SITE_PACKAGES` | 可选的额外依赖目录 |
| `OKX_CONFIG_MD` | 本地配置页；默认 `<PROJECT_ROOT>/config.md` |
| `FRED_API_KEY` | FRED 数据源凭证 |
| `COINGECKO_API_KEY` | CoinGecko 数据源凭证 |
| `MX_APIKEY` | 妙想数据源凭证 |
| `OKX_PROXY_URL` | 可选代理 URL |
| `OKX_QQ_TARGET` | QQ 目标，例如 `group:<openid>`；无默认值 |
| `OKX_EXECUTOR_DRYRUN` | `1` 时阻止交易变更命令 |
| `OKX_TRIGGER_DRYRUN` | `1` 时阻止 Agent/push 起棒 |

OKX API key、secret 和 passphrase 由 OKX CLI profile 或部署环境管理，不写入仓库。

## 安全验证

不访问交易所、不推送、不写运行数据库的基础检查：

```powershell
# Python 语法
python -m compileall -q collectors core scripts

# registry 结构
python collectors/sources/_registry.py --validate

# 角色规则同步检查（只读）
python scripts/check_trader_docs_sync.py
```

涉及数据库的工具应传入隔离目录。涉及 Agent、QQ、OpenClaw 或 OKX 的入口在发布验证中只允许 dry-run，或因缺少外部运行环境而明确跳过。

## 风控摘要

权威值以 `core/risk_validator.py` 为准：

- 单笔保证金最多为权益的 20%；
- 最多使用当前可用 USDT 保证金的 98%；
- 杠杆不超过 10x；
- 单笔名义价值不低于权益的 1%；
- 止损偏离 mark price 不超过 30%；
- 合约规格、余额、可用保证金或成交确认缺失时 fail-safe 拒绝。

这些限制没有因公开发布而修改。

## 安全报告

发现凭证或公开仓库历史泄漏时，不要在 issue 中粘贴真实值。请先轮换凭证，再通过仓库的私密安全报告渠道联系维护者。更多边界见 `SECURITY.md`。
