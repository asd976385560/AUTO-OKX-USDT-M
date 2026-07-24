<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<h1 align="center">AUTO-OKX-USDT-M</h1>

<p align="center">An autonomous trading system V2.0 for OKX USDT perpetual swaps</p>

<p align="center">
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/asd976385560/AUTO-OKX-USDT-M?style=flat-square&logo=github"></a>
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/network/members"><img alt="GitHub Forks" src="https://img.shields.io/github/forks/asd976385560/AUTO-OKX-USDT-M?style=flat-square&logo=github"></a>
  <a href="https://github.com/asd976385560/AUTO-OKX-USDT-M/commits/main"><img alt="Last Commit" src="https://img.shields.io/github/last-commit/asd976385560/AUTO-OKX-USDT-M?style=flat-square"></a>
</p>

V2.0 keeps market collection, risk checks, order execution, bookkeeping, push delivery, and stage dispatch in deterministic code. Isolated Agents handle analysis, trading decisions, reviews, and news sources without APIs. Live and demo execution share the same hard risk limits and stop-loss requirements; only the execution environment changes.

> [!WARNING]
> This project can execute real trades. Keep `OKX_EXECUTOR_DRYRUN=1` and `OKX_TRIGGER_DRYRUN=1` during initial deployment, and validate everything against isolated databases. This project is not investment advice and does not guarantee profit.

## Contents

- [Public release boundary](#public-release-boundary)
- [Version lineage](#version-lineage)
- [Architecture](#architecture)
- [Agent roles](#agent-roles)
- [Quick start](#quick-start)
- [Deploy Agents](#deploy-agents)
- [Configure scheduled jobs](#configure-scheduled-jobs)
- [Configuration and safe validation](#configuration-and-safe-validation)
- [Risk summary](#risk-summary)
- [Star history](#star-history)

## Public release boundary

The public release contains source code, Agent role rules, templates, database DDL, configuration examples, and public documentation. It does not contain:

- `config.md`, `.env*`, or usable credentials;
- runtime SQLite databases, WAL/SHM files, logs, reports, memory, or temporary output;
- real QQ/Webhook targets or account, device, Agent, and host identifiers;
- the local `Lib/` dependency tree, caches, or bytecode;
- real OpenClaw configuration, cron job ids, model assignments, or host state;
- internal host runbooks, historical archives, or one-off compatibility tools.

Copy `config.example.md` to a local `config.md` before filling it in. Environment variables have the highest credential priority; the public code contains no usable credential defaults.

## Version lineage

The synchronized source identifies itself as `V2.0`. The previous remote README used a `v3.1` label. Those labels are not automatically comparable semantic versions. This synchronization does not create a tag or Release; the maintainer will choose the final public version separately.

## Architecture

```text
OpenClaw cron
  ├─ fast_collect.py ───────────────┐
  ├─ slow_collect.py ───────────────┼─> ledger.db
  └─ sources/news_collect.py ───────┘
                                      │
                                      v
                              core/dispatcher.py
                                      │
                    unique stage_dispatch lock
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

Core invariants:

- `skill.md` is the V2.0 business fact source; this README is the public system map;
- `ledger.db.stage_dispatch(cycle_id, stage)` is the idempotent stage-dispatch truth;
- every table or explicit key domain has one authoritative writer, while readers use SQLite `mode=ro`;
- live opens only pass through `core/order_executor.py`, which always calls `core/risk_validator.py`;
- live and demo opens both require stop loss and share the same hard limits;
- current positions come from the OKX API, never from inferred local snapshots;
- push delivery always goes through `scripts/push_pipeline.py`.

## Agent roles

| Agent | Public role source | Responsibility | Default trigger |
|---|---|---|---|
| analyst | `agents/analyst.md` | Manual rollback analysis outside the default chain | Manual |
| unified live trader | `agents/live_trader.md` | Analysis, live decisions, hard risk checks, and execution | dispatcher |
| demo trader | `agents/demo_trader.md` | Demo execution using shared analysis and risk limits | dispatcher |
| news scout | `agents/news_scout.md` | Structured X and no-API news collection | Optional cron |
| reviewer | `agents/reviewer.md` | Daily, weekly, and monthly review summaries | Daily cron |

Each Agent uses an isolated OpenClaw workspace and receives its role source as `AGENTS.md`. Models, channels, tool permissions, and real workspace paths stay in the deployment environment.

## Quick start

Runtime requirements:

- Python 3.11 or later;
- `httpx`;
- SQLite from the Python standard library;
- OpenClaw for Agent scheduling;
- an OKX CLI profile configured outside the repository for order execution;
- PowerShell 7 for Windows wrappers and operations scripts.

```powershell
git clone https://github.com/asd976385560/AUTO-OKX-USDT-M.git
Set-Location AUTO-OKX-USDT-M

python -m pip install -r requirements.txt
Copy-Item config.example.md config.md

$env:OKX_ROOT = (Resolve-Path .).Path
$env:OKX_DB_ROOT = Join-Path $env:OKX_ROOT 'db'
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'
```

Use isolated databases for the first validation:

```powershell
$isolatedDb = Join-Path $env:TEMP 'auto-okx-v20-db'
python scripts/init_v20_dbs.py --root $isolatedDb --verify
python collectors/sources/_registry.py --validate
python scripts/check_trader_docs_sync.py
```

## Deploy Agents

The following example only creates workspaces. It does not configure real models, messaging channels, or credentials:

```powershell
$openclawRoot = Join-Path $HOME '.openclaw'

openclaw agents add okx-analyst --workspace (Join-Path $openclawRoot 'workspace-okx-analyst') --non-interactive
openclaw agents add okx-live-trader --workspace (Join-Path $openclawRoot 'workspace-okx-live-trader') --non-interactive
openclaw agents add okx-demo-trader --workspace (Join-Path $openclawRoot 'workspace-okx-demo-trader') --non-interactive
openclaw agents add okx-news-scout --workspace (Join-Path $openclawRoot 'workspace-okx-news-scout') --non-interactive
openclaw agents add okx-reviewer --workspace (Join-Path $openclawRoot 'workspace-okx-reviewer') --non-interactive

openclaw agents list
```

Copy each `agents/<role>.md` to the matching workspace as `AGENTS.md`, then copy the matching `IDENTITY.md` and `SOUL.md`. The complete role mapping, least-privilege boundaries, dry-run validation, scheduling, and rollback procedure are documented in:

- [Agent and OpenClaw Deployment Guide (English)](docs/agent-deployment.en.md)
- [Agent 与 OpenClaw 部署指南（中文）](docs/agent-deployment.zh-CN.md)

## Configure scheduled jobs

The default scheduling contract comes from `skill.md`:

| Work | Type | Schedule |
|---|---|---|
| fast collect | command | `0,15,30,45 * * * *` |
| slow collect | command | `2 * * * *` |
| dispatcher | command | `*/2 * * * *` |
| registry news | command | `3,18,33,48 * * * *` |
| news scout | agent | `5,20,35,50 * * * *`, optional |
| daily maintenance | command | once daily; requires separate approval before enabling |
| reviewer | agent | once daily |

Keep dry-run enabled and replace host paths with placeholders before creating a job:

```powershell
openclaw cron create '*/2 * * * *' `
  --name 'okx-dispatcher' `
  --command-argv '["python","<PROJECT_ROOT>/core/dispatcher.py","--db-root","<ISOLATED_DB_ROOT>"]' `
  --timeout-seconds 120 `
  --no-deliver
```

Validate with `openclaw cron list`, `openclaw cron show <CRON_JOB_ID>`, and `openclaw cron run <CRON_JOB_ID> --wait`. The public repository never provides real job ids or modifies local cron automatically.

## Configuration and safe validation

Main environment variables:

| Variable | Purpose |
|---|---|
| `OKX_ROOT` | Project root; derived from the source file when unset |
| `OKX_DB_ROOT` | Database root; defaults to `<PROJECT_ROOT>/db` |
| `OKX_PYTHON_BIN` | Python executable |
| `OKX_SITE_PACKAGES` | Optional extra dependency directory |
| `OKX_CONFIG_MD` | Local configuration page; defaults to `<PROJECT_ROOT>/config.md` |
| `FRED_API_KEY` | FRED data-source credential |
| `COINGECKO_API_KEY` | CoinGecko data-source credential |
| `MX_APIKEY` | MX data-source credential |
| `OKX_PROXY_URL` | Optional proxy URL |
| `OKX_QQ_TARGET` | QQ destination; no default |
| `OKX_EXECUTOR_DRYRUN` | `1` blocks trade-changing commands |
| `OKX_TRIGGER_DRYRUN` | `1` blocks Agent and push triggers |

Safe checks that do not contact OKX, send messages, or write runtime databases:

```powershell
python -m compileall -q collectors core scripts
python collectors/sources/_registry.py --validate
python scripts/check_trader_docs_sync.py
python scripts/update_star_stats.py --self-test
```

Pass an isolated directory to every database-writing tool. Agent, QQ, OpenClaw, and OKX entry points must remain in dry-run unless external execution is separately approved.

## Risk summary

Authoritative values live in `core/risk_validator.py`:

- margin per trade is at most 20% of equity (`MAX_MARGIN_PCT`);
- at most 98% of currently available USDT margin may be used (`AVAILABLE_MARGIN_USE_PCT`);
- leverage is capped at 10x (`MAX_LEVERAGE`);
- notional per trade is at least 1% of equity (`MIN_NOTIONAL_PCT`);
- stop-loss deviation from mark price is capped at 30% (`MAX_SL_DEVIATION`);
- missing contract specifications, balances, available margin, or fill confirmation fail safe.

These limits are unchanged by public release work, documentation internationalization, or star statistics.

## Star history

The badge shows the current star count. The repository's own GitHub Actions workflow checks every six hours and generates the chart below. Only daily aggregate totals are stored; user identities are discarded.

[![GitHub Star History](docs/assets/star-history.svg)](https://github.com/asd976385560/AUTO-OKX-USDT-M/stargazers)

The generator only uses the repository-scoped `GITHUB_TOKEN` while the workflow is running. It requires no personal PAT and never writes the token to files, logs, or the chart. Aggregate data is available in `docs/data/star-history.json`.

## Security reports

Never paste a suspected credential into a public issue. Rotate it first, preserve only redacted evidence, and contact the maintainer privately. See [SECURITY.md](SECURITY.md) and [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md) for the public boundary.
