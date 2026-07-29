<!--
doc-version: V2.0-agent-deployment
last-updated: 2026-07-29
updated-by: Codex
change-summary: Align public Agent deployment with the latest stage supervision and dry-run contracts.
-->

# Agent and OpenClaw Deployment Guide

[简体中文](agent-deployment.zh-CN.md) · [English](agent-deployment.en.md)

This guide only covers public, portable deployment steps. It contains no real models, credentials, messaging destinations, cron job ids, device identifiers, account identifiers, or host-specific absolute paths.

> [!CAUTION]
> Creating Agents and cron jobs changes local OpenClaw state. Use isolated databases, keep `OKX_EXECUTOR_DRYRUN=1` and `OKX_TRIGGER_DRYRUN=1`, and create and verify one item at a time. Never copy production configuration in bulk.

## 1. Deployment boundary

The public repository provides:

- role rules for five Agents;
- `IDENTITY.md` and `SOUL.md` for each Agent;
- public scheduling expressions;
- dry-run and isolated validation procedures;
- configuration examples without real values.

The deployer supplies:

- OpenClaw, Python, Node, and the OKX CLI;
- model and tool-permission configuration;
- messaging channels and delivery destinations;
- credentials outside the repository;
- local workspace and database paths;
- real job ids returned after cron creation.

## 2. Role and workspace mapping

| Example Agent id | Role source | Persona | Trigger |
|---|---|---|---|
| `okx-analyst` | `agents/analyst.md` | `agents/personas/analyst/` | Manual rollback |
| `okx-live-trader` | `agents/live_trader.md` | `agents/personas/live_trader/` | dispatcher |
| `okx-demo-trader` | `agents/demo_trader.md` | `agents/personas/demo_trader/` | dispatcher |
| `okx-news-scout` | `agents/news_scout.md` | `agents/personas/news_scout/` | Optional cron |
| `okx-reviewer` | `agents/reviewer.md` | `agents/personas/reviewer/` | Daily cron |

These ids are public examples, not production job ids or device identifiers.

## 3. Prepare the environment

```powershell
Set-Location <PROJECT_ROOT>

python -m pip install -r requirements.txt
Copy-Item config.example.md config.md

$env:OKX_ROOT = (Resolve-Path .).Path
$env:OKX_DB_ROOT = Join-Path $env:TEMP 'auto-okx-v20-db'
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'
```

Keep the OKX API key, secret, and passphrase in an external OKX CLI profile or deployment environment. Never put them in command lines, Agent Markdown, cron messages, or GitHub Actions.

## 4. Create isolated workspaces

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

Do not share one workspace between trading Agents. Role rules, memory, and tool boundaries must stay isolated.

## 5. Install role files

Every workspace needs at least `AGENTS.md`, `IDENTITY.md`, and `SOUL.md`:

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

Run `openclaw agents list --bindings` again and verify that every id points to the intended workspace only.

## 6. Models, tools, and channels

Configure the following in the local OpenClaw deployment:

- `<MODEL_ID>` and controlled fallbacks;
- a least-privilege tool allowlist;
- the isolated workspace;
- required messaging channels;
- environment variables stored outside the repository.

Public role files intentionally contain no specific model names. Agents must not write SQLite tables directly, construct raw OKX order commands, read raw credentials, or bypass writers.

Suggested minimum capabilities:

| Agent | Minimum capability |
|---|---|
| analyst | Read-only databases and the report writer adapter |
| live trader | Read-only evidence, decision cards, controlled order executor/writer |
| demo trader | Read-only evidence and demo executor/writer |
| news scout | Configured news search and `news_writer` |
| reviewer | Read-only reports and review writer; trade execution denied |

## 7. Initialize isolated databases

```powershell
python scripts/init_v20_dbs.py --root $env:OKX_DB_ROOT --verify
python collectors/sources/_registry.py --validate
python scripts/check_trader_docs_sync.py
```

Confirm that `$env:OKX_DB_ROOT` does not point to production before continuing.

## 8. Create dry-run command cron jobs

Replace every placeholder one at a time. `--no-dispatch` is retained by the source only as a no-op compatibility option, so these examples do not depend on it. Isolation comes from `--dry-collect`, `OKX_TRIGGER_DRYRUN=1`, and a separate database root.

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

Without `--apply`, `news_collect.py` does not write runtime databases, but it still sends outbound requests to enabled data sources. Create the registry news job below only after outbound network access receives separate approval:

```powershell
openclaw cron create '3,18,33,48 * * * *' `
  --name 'okx-registry-news' `
  --command-argv '["<PYTHON_BIN>","<PROJECT_ROOT>/collectors/sources/news_collect.py","--db-root","<ISOLATED_DB_ROOT>"]' `
  --timeout-seconds 180 `
  --no-deliver
```

Cron creation returns a job id. Do not write it back to the repository.

## 9. Create optional Agent cron jobs

Only create these jobs after validating the Agent model, tool allowlist, and workspace:

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

The live and demo traders are started by the dispatcher according to `stage_dispatch`; do not add fixed periodic cron jobs for them. The analyst is manual rollback only.

`scripts/daily_maintenance.py` contains steps that write runtime data or contact external services, so this guide intentionally provides no one-command enablement. Create it only after isolated validation and separate maintainer approval.

## 10. Validate every job

```powershell
openclaw cron list
openclaw cron show <CRON_JOB_ID>
openclaw cron run <CRON_JOB_ID> --wait --wait-timeout 10m
openclaw cron runs --id <CRON_JOB_ID> --limit 10
```

Verify:

- schedule, timezone, Agent, and workspace;
- command paths point to isolated databases;
- dry-run variables are active;
- job messages contain no credentials or real destinations;
- replay cannot bypass `stage_dispatch` idempotency;
- no OKX order, QQ delivery, or production database write occurs.

## 11. Promote from dry-run

Only consider promotion when all conditions are satisfied:

1. independent security review passed;
2. isolated database validation passed;
3. Agent tool allowlists reviewed;
4. OKX demo validation completed;
5. external calls explicitly approved by the maintainer;
6. production credentials remain outside the repository;
7. every cron job has a recorded disable and rollback path.

Removing `--dry-collect`, adding `--apply`, or disabling trade dry-run are separate high-risk changes. The presence of this guide never authorizes them.

## 12. Rollback

```powershell
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'

openclaw cron disable <CRON_JOB_ID>
openclaw cron show <CRON_JOB_ID>
```

Disable the dispatcher first, then collectors and Agent cron jobs. Keep jobs for audit, and never delete OpenClaw state or databases without a verified backup.
