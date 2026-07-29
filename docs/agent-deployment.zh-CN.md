<!--
doc-version: V2.0-agent-deployment
last-updated: 2026-07-29
updated-by: Codex
change-summary: Align public Agent deployment with the latest stage supervision and dry-run contracts.
-->

# Agent 与 OpenClaw 部署指南

[简体中文](agent-deployment.zh-CN.md) · [English](agent-deployment.en.md)

本文只说明公开、可移植的部署方法。它不包含真实模型、凭证、消息目标、cron job ID、设备标识、账户标识或宿主绝对路径。

> [!CAUTION]
> 创建 Agent 和 cron 会修改本机 OpenClaw 状态。首次部署必须使用隔离数据库，保持 `OKX_EXECUTOR_DRYRUN=1` 与 `OKX_TRIGGER_DRYRUN=1`，并逐项创建、逐项核验。不要批量复制生产配置。

## 1. 部署边界

公开仓库提供：

- 5 个 Agent 的角色规则；
- 每个 Agent 的 `IDENTITY.md` 与 `SOUL.md`；
- 公开调度表达式；
- dry-run 和隔离验证方法；
- 不含真实值的配置示例。

部署者自行提供：

- OpenClaw、Python、Node 和 OKX CLI；
- 模型与工具权限配置；
- 消息通道与推送目标；
- 仓库外的凭证；
- 本机 workspace 和数据库路径；
- cron 创建后返回的真实 job ID。

## 2. 角色与 workspace 映射

| Agent ID 示例 | 角色源 | Persona | 触发方式 |
|---|---|---|---|
| `okx-analyst` | `agents/analyst.md` | `agents/personas/analyst/` | 手工回滚 |
| `okx-live-trader` | `agents/live_trader.md` | `agents/personas/live_trader/` | dispatcher |
| `okx-demo-trader` | `agents/demo_trader.md` | `agents/personas/demo_trader/` | dispatcher |
| `okx-news-scout` | `agents/news_scout.md` | `agents/personas/news_scout/` | 独立 cron，可选 |
| `okx-reviewer` | `agents/reviewer.md` | `agents/personas/reviewer/` | 日频 cron |

这些 ID 是公开示例，不是生产 job ID 或设备标识。

## 3. 准备环境

```powershell
Set-Location <PROJECT_ROOT>

python -m pip install -r requirements.txt
Copy-Item config.example.md config.md

$env:OKX_ROOT = (Resolve-Path .).Path
$env:OKX_DB_ROOT = Join-Path $env:TEMP 'auto-okx-v20-db'
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'
```

OKX API Key、Secret 和 Passphrase 只放在仓库外的 OKX CLI profile 或部署环境。不要填入命令行、Agent Markdown、cron message 或 GitHub Actions。

## 4. 创建独立 workspace

```powershell
$openclawRoot = Join-Path $HOME '.openclaw'

$workspaces = @{
  'okx-analyst'     = Join-Path $openclawRoot 'workspace-okx-analyst'
  'okx-live-trader' = Join-Path $openclawRoot 'workspace-okx-live-trader'
  'okx-demo-trader' = Join-Path $openclawRoot 'workspace-okx-demo-trader'
  'okx-news-scout'  = Join-Path $openclawRoot 'workspace-okx-news-scout'
  'okx-reviewer'    = Join-Path $openclawRoot 'workspace-okx-reviewer'
}

foreach ($entry in $workspaces.GetEnumerator()) {
  openclaw agents add $entry.Key --workspace $entry.Value --non-interactive
}

openclaw agents list --bindings
```

不要让多个交易 Agent 共用同一个 workspace。角色规则、记忆和工具边界必须隔离。

## 5. 安装角色文件

每个 workspace 至少需要 `AGENTS.md`、`IDENTITY.md` 和 `SOUL.md`：

```powershell
$roles = @{
  'okx-analyst'     = 'analyst'
  'okx-live-trader' = 'live_trader'
  'okx-demo-trader' = 'demo_trader'
  'okx-news-scout'  = 'news_scout'
  'okx-reviewer'    = 'reviewer'
}

foreach ($agentId in $roles.Keys) {
  $role = $roles[$agentId]
  $workspace = $workspaces[$agentId]
  Copy-Item "agents/$role.md" "$workspace/AGENTS.md"
  Copy-Item "agents/personas/$role/IDENTITY.md" "$workspace/IDENTITY.md"
  Copy-Item "agents/personas/$role/SOUL.md" "$workspace/SOUL.md"
}
```

配置更新后，再次执行 `openclaw agents list --bindings`，确认每个 ID 只指向预期 workspace。

## 6. 模型、工具与通道

在本机 OpenClaw 配置中为每个 Agent 设置：

- `<MODEL_ID>` 和受控 fallback；
- 最小工具白名单；
- 独立 workspace；
- 必要的消息通道；
- 仓库外环境变量。

公开角色文件不提供具体模型名。Agent 不得直接写 SQLite 表、拼接 OKX 下单命令、读取 raw credential 或绕过 writer。

建议的最低权限：

| Agent | 最低能力 |
|---|---|
| analyst | 只读数据库、报告写入适配器 |
| live trader | 只读证据、决策卡、受控 order executor/writer |
| demo trader | 只读证据、demo executor/writer |
| news scout | 配置的新闻检索、`news_writer` |
| reviewer | 只读报告、复盘 writer；默认禁止交易执行 |

## 7. 初始化隔离数据库

```powershell
python scripts/init_v20_dbs.py --root $env:OKX_DB_ROOT --verify
python collectors/sources/_registry.py --validate
python scripts/check_trader_docs_sync.py
```

确认 `$env:OKX_DB_ROOT` 不指向生产目录，再继续。

## 8. 创建 dry-run command cron

以下命令使用占位符，必须逐一替换。`--no-dispatch` 只是源码保留的无副作用兼容参数，因此示例不依赖它；隔离由 `--dry-collect`、`OKX_TRIGGER_DRYRUN=1` 和独立数据库共同保证。

```powershell
openclaw cron create '0,15,30,45 * * * *' `
  --name 'okx-fast-collect' `
  --command-argv '["<PYTHON_BIN>","<PROJECT_ROOT>/collectors/fast_collect.py","--db-root","<ISOLATED_DB_ROOT>","--dry-collect"]' `
  --command-env 'OKX_ROOT=<PROJECT_ROOT>' `
  --command-env 'OKX_DB_ROOT=<ISOLATED_DB_ROOT>' `
  --command-env 'OKX_TRIGGER_DRYRUN=1' `
  --timeout-seconds 240 `
  --no-deliver

openclaw cron create '2 * * * *' `
  --name 'okx-slow-collect' `
  --command-argv '["<PYTHON_BIN>","<PROJECT_ROOT>/collectors/slow_collect.py","--db-root","<ISOLATED_DB_ROOT>","--dry-collect"]' `
  --command-env 'OKX_ROOT=<PROJECT_ROOT>' `
  --command-env 'OKX_DB_ROOT=<ISOLATED_DB_ROOT>' `
  --command-env 'OKX_TRIGGER_DRYRUN=1' `
  --timeout-seconds 600 `
  --no-deliver

openclaw cron create '*/2 * * * *' `
  --name 'okx-dispatcher' `
  --command-argv '["<PYTHON_BIN>","<PROJECT_ROOT>/core/dispatcher.py","--db-root","<ISOLATED_DB_ROOT>"]' `
  --command-env 'OKX_ROOT=<PROJECT_ROOT>' `
  --command-env 'OKX_DB_ROOT=<ISOLATED_DB_ROOT>' `
  --command-env 'OKX_TRIGGER_DRYRUN=1' `
  --timeout-seconds 120 `
  --no-deliver
```

`news_collect.py` 在不带 `--apply` 时不会写运行数据库，但仍会向启用的数据源发起网络请求。因此，下面的 registry news job 只应在出站网络已单独获批后创建：

```powershell
openclaw cron create '3,18,33,48 * * * *' `
  --name 'okx-registry-news' `
  --command-argv '["<PYTHON_BIN>","<PROJECT_ROOT>/collectors/sources/news_collect.py","--db-root","<ISOLATED_DB_ROOT>"]' `
  --timeout-seconds 180 `
  --no-deliver
```

创建命令默认会返回 job ID。不要把 ID 写回仓库。

## 9. 创建可选 Agent cron

只有在 Agent 模型、工具白名单和 workspace 完成核验后，才能创建：

```powershell
openclaw cron create '5,20,35,50 * * * *' `
  'Run one news-scout collection cycle. Follow AGENTS.md and keep structured output only.' `
  --name 'okx-news-scout' `
  --session isolated `
  --agent okx-news-scout `
  --timeout-seconds 600 `
  --no-deliver

openclaw cron create '5 8 * * *' `
  'Run the scheduled review. Follow AGENTS.md and keep trading execution disabled.' `
  --name 'okx-reviewer' `
  --tz 'Asia/Shanghai' `
  --session isolated `
  --agent okx-reviewer `
  --timeout-seconds 3600 `
  --no-deliver
```

live trader 和 demo trader 由 dispatcher 根据 `stage_dispatch` 状态起棒，不应再创建独立固定周期 cron。analyst 仅用于人工回滚。

`scripts/daily_maintenance.py` 包含会写运行数据或访问外部服务的步骤，不提供一键启用示例。它必须在隔离验证完成后，经维护者单独批准再创建。

## 10. 核验每个 job

```powershell
openclaw cron list
openclaw cron show <CRON_JOB_ID>
openclaw cron run <CRON_JOB_ID> --wait --wait-timeout 10m
openclaw cron runs --id <CRON_JOB_ID> --limit 10
```

核验内容：

- schedule、timezone、Agent 和 workspace 正确；
- command 指向隔离数据库；
- dry-run 环境变量已生效；
- 没有真实凭证或目标出现在 job message；
- 重复执行不会绕过 `stage_dispatch` 幂等；
- 没有 OKX 下单、QQ 发送或生产数据库写入。

## 11. 从 dry-run 升级

只有全部条件同时满足时才考虑升级：

1. 独立安全审查通过；
2. 隔离数据库验证通过；
3. Agent 工具白名单已核对；
4. OKX demo 环境验证完成；
5. 维护者明确批准外部调用；
6. 生产凭证仍位于仓库外；
7. 每个 cron 都有已记录的禁用和回滚方式。

移除 `--dry-collect`、增加 `--apply` 或关闭交易 dry-run 都是独立的高风险变更，不能因为部署文档存在而自动执行。

## 12. 回滚

```powershell
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'

openclaw cron disable <CRON_JOB_ID>
openclaw cron show <CRON_JOB_ID>
```

回滚时先禁用 dispatcher，再禁用采集与 Agent cron。保留 job 供审计，不要在未备份的情况下删除 OpenClaw 状态或数据库。
