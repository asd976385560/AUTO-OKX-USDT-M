<!--
doc-version: V2.0
last-updated: 2026-08-04
updated-by: Codex
change-summary: Add the 1.0.0 public release contract, changelog and gated tag-to-Release workflow.
-->

<p align="center">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<h1 align="center">AUTO-OKX-USDT-M</h1>

<p align="center">面向 OKX USDT 永续合约的自主交易系统 V2.0</p>

<p align="center">
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/asd976385560/AUTO-OKX-USDT-M?style=flat-square&logo=github"></a>
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/network/members"><img alt="GitHub Forks" src="https://img.shields.io/github/forks/asd976385560/AUTO-OKX-USDT-M?style=flat-square&logo=github"></a>
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/commits/main"><img alt="Last Commit" src="https://img.shields.io/github/last-commit/asd976385560/AUTO-OKX-USDT-M?style=flat-square"></a>
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/asd976385560/AUTO-OKX-USDT-M/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"></a>
</p>

V2.0 将市场采集、风控、下单、记账、推送和阶段派发放在确定性代码中，将分析、交易判断、复盘和无 API 新闻取数交给隔离 Agent。系统同时支持 live 与 demo；两者共用执行意图幂等、账仓一致性、成交确认和止损安全路径，但容量按 profile 分治：Live 使用组合 IMR，Demo 使用实时方向性 max-size。

> [!WARNING]
> 本项目包含真实交易执行能力。首次部署必须保持 `OKX_EXECUTOR_DRYRUN=1` 和 `OKX_TRIGGER_DRYRUN=1`，并在隔离数据库中完成验证。本项目不构成投资建议，也不保证盈利。

## 目录

- [公开发布边界](#公开发布边界)
- [版本线说明](#版本线说明)
- [本次同步](#本次同步)
- [架构](#架构)
- [Agent 角色](#agent-角色)
- [快速开始](#快速开始)
- [部署 Agent](#部署-agent)
- [配置定时任务](#配置定时任务)
- [配置与安全验证](#配置与安全验证)
- [测试边界](#测试边界)
- [风控摘要](#风控摘要)
- [Stars 趋势](#stars-趋势)
- [许可证](#许可证)
- [安全报告](#安全报告)

## 公开发布边界

公开版本只包含源码、Agent 角色规则、模板、数据库 DDL、配置示例和公开文档，不包含：

- `config.md`、`.env*` 或任何真实凭证；
- SQLite 运行库、WAL/SHM、日志、报告、记忆和临时文件；
- QQ/Webhook 真实目标、账户、设备、Agent 或主机标识；
- 本地依赖目录 `Lib/`、缓存和字节码；
- 真实 OpenClaw 配置、cron job ID、模型分配和宿主状态；
- 内部宿主运维资料、历史归档和一次性兼容脚本。

复制 `config.example.md` 为本地 `config.md` 后再填写。凭证读取以环境变量为最高优先级；公开代码中没有真实值或可用默认凭证。

## 版本线说明

公开发行采用语义化版本，唯一版本源是 [`VERSION`](VERSION)；对应 Git tag 和
GitHub Release 名称统一为 `v<VERSION>`。每次发布的非空说明及版本链接记录在
[`CHANGELOG.md`](CHANGELOG.md)，发布工作流直接使用匹配版本段作为 Release 正文。

`V2.0` 继续表示系统架构、业务契约、文档和 schema 代际，不是公开发行版本，
也不会被公开发行版本替换。只有版本变更 PR 合并到 `main` 后，维护者创建并推送
匹配的 annotated tag，且发布工作流的版本、主分支可达性、完整 CI 全部通过，
才会创建 GitHub Release。

## 本次同步

2026-08-04 的公开同步在 7 月 29 日基线上继续带出以下生产语义，同时保留动态路径、空凭证和显式写入授权：

- Live OPEN/ADD 预计成交后组合 `account.imr/totalEq` 不得超过 66.6%，超限整笔拒绝；Demo 按目标杠杆后的方向性 `account max-size` 定仓；
- `stage_profile_leases` 防止同一 profile 跨 cycle 重叠，runner 还会核验真实 trade 终态；
- DXY observation-date 去重与 stale 语义、交易时点 regime、固定 08:00 报告窗口和推送持仓投影；
- GHOST-EXACT / UNRECORDED 精确证据链检查；Live autoheal 永久只读，Demo close/open 写入分别使用两级显式 opt-in；Demo UNRECORDED 还必须确认 intent、ordId 与现有交易所止损，且永不下单或重放订单；
- 业务推送与告警目标分离，分别只从 `OKX_QQ_TARGET`、`OKX_QQ_ALERT_TARGET` 读取；
- 扩展后的隔离回归、生命周期清单及带备份门禁的 profile-lease 迁移。

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
              stage_dispatch 闩锁 + profile lease
                                      │
                 ┌────────────────────┴───────────────────┐
                 v                                        v
       unified live trader                         demo trader
       analysis + live execution                   demo execution
                 │                                        │
                 └───────────────┬────────────────────────┘
                                 v
                    stage_runner 终态/业务产物核验
                                 │
                                 v
                     scripts/push_pipeline.py
                     render -> validate -> archive/send
```

核心不变量：

- `skill.md` 是 V2.0 业务事实源，本文是公开系统地图；
- `ledger.db.stage_dispatch(cycle_id, stage)` 是阶段派发幂等真值；
- `ledger.db.execution_intents` 在任何交易所 I/O 前阻断每个 profile 的未决或含糊意图；精确补账转为 `reconciled` 后只阻断原逻辑单重下，不再冻结整个 profile；
- 每张表或明确键域只有一个权威 writer，读者使用 SQLite `mode=ro`；
- live 开仓只经 `core/order_executor.py`，内部强制调用 `core/risk_validator.py`；
- 下单前必须使 OKX 全量现仓与该 profile 的已确认交易账本一致；
- live/demo 都要求止损，并共用同一套硬上限；
- 当前持仓以 OKX API 为准，不能由本地快照推断；
- confirmed fill 必须具备权威 `fill_sz`、`fill_px`、`fill_ts` 和 `ts_source`；
- push 固定走 `scripts/push_pipeline.py`，不由 Agent 临时拼接执行链。

## Agent 角色

| Agent | 公开角色源 | 职责 | 默认触发 |
|---|---|---|---|
| analyst | `agents/analyst.md` | 人工回滚分析，不参与默认主链 | 手工 |
| unified live trader | `agents/live_trader.md` | 分析、实盘判断、硬风控和执行 | dispatcher |
| demo trader | `agents/demo_trader.md` | 共用分析与硬风控的模拟执行 | dispatcher |
| news scout | `agents/news_scout.md` | X/无 API 新闻取数和结构化 | 独立 cron，可选 |
| reviewer | `agents/reviewer.md` | 日/周/月复盘和经验摘要 | 日频 cron |

每个 Agent 使用独立 OpenClaw workspace，并将自己的角色文件部署为 `AGENTS.md`。模型、通道、工具权限和真实 workspace 路径只在部署环境配置，不进入仓库。

## 快速开始

运行依赖：

- Python 3.11 或更高版本；
- `httpx`；
- SQLite（Python 标准库）；
- Agent 调度需要 OpenClaw；
- 实际下单需要在仓库外配置 OKX CLI profile；当前代码按 OKX CLI 1.4.2 命令契约验证；
- Windows 包装器和运维脚本使用 PowerShell 7。

```powershell
git clone https://github.com/asd976385560/AUTO-OKX-USDT-M.git
Set-Location AUTO-OKX-USDT-M

python -m pip install -r requirements.txt
Copy-Item config.example.md config.md

$env:OKX_ROOT = (Resolve-Path .).Path
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'
```

首次验证必须使用隔离数据库：

```powershell
$isolatedDb = Join-Path $env:TEMP ('auto-okx-v20-db-' + [guid]::NewGuid())
$env:OKX_DB_ROOT = $isolatedDb
python scripts/init_v20_dbs.py --root $isolatedDb --verify
python collectors/sources/_registry.py --validate
python scripts/check_trader_docs_sync.py
```

`init_v20_dbs.py` 目前只初始化它负责的 V2.0 schema 子集；成功退出不代表
`market.db`、`news.db` 和 `account.db` 已由采集链完整创建，也不代表可直接开启实盘。

## 部署 Agent

以下示例只创建 workspace，不配置真实模型、消息通道或凭证：

```powershell
$openclawRoot = Join-Path $HOME '.openclaw'

openclaw agents add okx-analyst --workspace (Join-Path $openclawRoot 'workspace-okx-analyst') --non-interactive
openclaw agents add okx-live-trader --workspace (Join-Path $openclawRoot 'workspace-okx-live-trader') --non-interactive
openclaw agents add okx-demo-trader --workspace (Join-Path $openclawRoot 'workspace-okx-demo-trader') --non-interactive
openclaw agents add okx-news-scout --workspace (Join-Path $openclawRoot 'workspace-okx-news-scout') --non-interactive
openclaw agents add okx-reviewer --workspace (Join-Path $openclawRoot 'workspace-okx-reviewer') --non-interactive

openclaw agents list
```

随后把每个 `agents/<role>.md` 复制为相应 workspace 的 `AGENTS.md`，并复制匹配的 `IDENTITY.md` 与 `SOUL.md`。完整步骤、角色映射、权限边界、dry-run 验证和回滚方式见：

- [Agent 与 OpenClaw 部署指南（中文）](docs/agent-deployment.zh-CN.md)
- [Agent and OpenClaw Deployment Guide (English)](docs/agent-deployment.en.md)

## 配置定时任务

默认调度口径来自 `skill.md`：

| 工作 | 类型 | 调度 |
|---|---|---|
| fast collect | command | `0,15,30,45 * * * *` |
| slow collect | command | `2 * * * *` |
| dispatcher | command | `*/2 * * * *` |
| registry news | command | `3,18,33,48 * * * *` |
| news scout | agent | `5,20,35,50 * * * *`，可选 |
| daily maintenance | command | 每日一次，启用前必须单独授权 |
| reviewer | agent | 每日一次 |

创建定时任务前必须保持 dry-run，并用占位符替换本机路径：

```powershell
openclaw cron create '*/2 * * * *' `
  --name 'okx-dispatcher' `
  --command-argv '["python","<PROJECT_ROOT>/core/dispatcher.py","--db-root","<ISOLATED_DB_ROOT>"]' `
  --timeout-seconds 120 `
  --no-deliver
```

用 `openclaw cron list`、`openclaw cron show <CRON_JOB_ID>` 和 `openclaw cron run <CRON_JOB_ID> --wait` 核验。公开仓库不提供真实 job ID，也不会自动修改本机 cron。

## 配置与安全验证

主要环境变量：

| 变量 | 用途 |
|---|---|
| `OKX_ROOT` | 项目根目录；未设置时从源码位置推导 |
| `OKX_DB_ROOT` | 数据库目录；默认 `<PROJECT_ROOT>/db` |
| `OKX_PYTHON_BIN` | Python 可执行文件 |
| `OKX_SITE_PACKAGES` | 可选额外依赖目录 |
| `OKX_CONFIG_MD` | 本地配置页；默认 `<PROJECT_ROOT>/config.md` |
| `FRED_API_KEY` | FRED 数据源凭证 |
| `COINGECKO_API_KEY` | CoinGecko 数据源凭证 |
| `MX_APIKEY` | 妙想数据源凭证 |
| `OKX_PROXY_URL` | 可选代理 URL |
| `OKX_QQ_TARGET` | QQ 目标；无默认值 |
| `OKX_QQ_ALERT_TARGET` | 告警 QQ 目标；无默认值 |
| `OKX_EXECUTOR_DRYRUN` | `1` 时阻止交易变更命令 |
| `OKX_TRIGGER_DRYRUN` | `1` 时阻止 Agent/push 起棒 |
| `OKX_LEDGER_AUTOHEAL_APPLY` | 仅 Demo：`1` 时允许精确 GHOST close 补账；Live 永久只读 |
| `OKX_LEDGER_AUTOHEAL_UNRECORDED` | 仅 Demo：与上一开关同时为 `1`，且 execution_intent、ordId 与同侧足量保护止损均确认时才允许精确 UNRECORDED open 补账；任一证据缺失永远只报告 |

`OKX_DB_ROOT` 可用于确定性脚本、writer、push 与隔离 dry-run。真实 Agent turn
在 OpenClaw Gateway 服务端执行，本地子进程环境不能证明会传入远端工具进程；因此公开版对
非默认 DB root 的真实 Agent 起棒会 fail-closed，只允许在 `OKX_TRIGGER_DRYRUN=1` 下验证。
完成隔离验收后，真实 Agent 部署须使用规范 `<PROJECT_ROOT>/db`，或由维护者另行实现并验证
Gateway 级 DB-root 注入后再改此硬闸。

升级已有 `ledger.db` 时，dispatcher 不会隐式创建新增租约表。先对隔离副本 dry-run，确认后再显式指定备份目录：

```powershell
python scripts/apply_stage_profile_lease_schema.py --db-root <ISOLATED_DB_ROOT>
python scripts/apply_stage_profile_lease_schema.py --db-root <AUTHORIZED_DB_ROOT> --apply --backup-dir <VERIFIED_BACKUP_DIR>
```

不访问交易所、不推送、不写运行数据库的基础检查：

```powershell
python scripts/check_release_version.py --json
python -m compileall -q collectors core scripts tests
python -m unittest discover -s tests -p "test_*.py" -v
python collectors/sources/_registry.py --validate
python scripts/check_script_lifecycle.py --json
python scripts/check_trader_docs_sync.py
python scripts/check_doc_versions.py --static-only
python scripts/update_star_stats.py --self-test
```

涉及数据库的工具必须传入隔离目录。涉及 Agent、QQ、OpenClaw 或 OKX 的入口只允许 dry-run，或在缺少外部环境时明确跳过；不要把隔离 DB root 的 dry-run 直接改成真实 Agent 起棒。

## 测试边界

`tests/` 是最小、分层、无生产副作用的回归集，覆盖执行意图、账仓一致性、
成交和止损契约、writer/dispatcher/profile lease、报告、公开宏观和受控账本修复。它不连接生产数据库，
不发送消息，也不代表完整 money-path、真实交易所或 OpenClaw 端到端验收已恢复。

## 风控摘要

权威值以 `core/risk_validator.py` 为准：

- Live OPEN/ADD 预计成交后组合 IMR 比例不超过 66.6%（`MAX_PORTFOLIO_IMR_RATIO`），超限整笔拒绝；
- Demo OPEN 只按交易所实时方向性 `account max-size` 和合约 `minSz/lotSz` 定仓，不回退 Live 余额公式；
- 最多使用当前可用 USDT 保证金的 98%（`AVAILABLE_MARGIN_USE_PCT`）；
- 杠杆不超过 10x（`MAX_LEVERAGE`）；
- 单笔名义价值不低于权益的 1%（`MIN_NOTIONAL_PCT`）；
- 止损偏离 mark price 不超过 30%（`MAX_SL_DEVIATION`）；
- 同一 profile 存在未决交易意图时，新交易在任何交易所 I/O 前 fail-closed；
- OKX 现仓与本地交易账本不一致时，在读取 mark 或下单前 fail-closed；
- 独立止损必须回读本次 `algoId`，confirmed fill 必须来自权威端点；
- 合约规格、余额、可用保证金或成交确认缺失时 fail-safe 拒绝。

Live 账本 autoheal 永久只读；即使直接向 API/CLI 传 `--apply` 或设置环境开关，也只会
完成只读分类并以非零结构化结果指向受控人工流程。Live 修复必须唯一命中一个 `ordId`，
写前创建并验证 SQLite 备份，逐笔 apply，随后重拉交易所现仓并复跑 reconciliation 与
ledger invariants。两个环境开关仅授权 Demo；Demo UNRECORDED open 仍需精确 fills、订单
归属、同侧足量保护止损、单轮上限和 runner 互斥检查。所有路径只修账，不会下单。

这些限制没有因公开发布、文档国际化或 Stars 统计而修改。

## Stars 趋势

动态徽章显示当前 Stars 数；下图由仓库自己的 GitHub Actions 每 6 小时检查并生成，只保存每日聚合数量，不保存用户身份。

[![GitHub Star History](docs/assets/star-history.svg)](https://github.com/asd976385560/AUTO-OKX-USDT-M/stargazers)

统计脚本仅使用工作流运行期间的仓库级 `GITHUB_TOKEN`，不需要个人 PAT，也不会把 Token 写入文件、日志或图表。明细见 `docs/data/star-history.json`。

## 许可证

本项目采用 [MIT License](LICENSE)。任何个人或组织均可将本项目用于私人或商业用途，也可复制、修改、合并、发布、分发、再授权或销售副本，但须保留原版权声明和许可证声明。

本软件不提供任何明示或默示担保；项目的交易风险与法律、监管合规责任仍由使用者自行承担。

## 安全报告

发现凭证或公开历史泄漏时，不要在 issue 中粘贴真实值。请先轮换凭证，再通过私密渠道联系维护者。更多边界见 [SECURITY.md](SECURITY.md) 和 [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md)。
