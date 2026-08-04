<!--
doc-version: V2.0-public-scope
last-updated: 2026-08-04
updated-by: Codex
change-summary: Add the guarded profile-lease migration, ledger-autoheal opt-ins, alert routing and expanded regression boundary.
-->

# Public release scope

This file records the intended V2.0 public synchronization boundary.

## Included

- deterministic collectors, writers, risk validation, order execution and dispatcher code;
- Agent role sources and report templates;
- `db/schema.sql` without runtime databases;
- the public-only `scripts/lifecycle.json` and its read-only validator;
- audited current schema migrations that default to read-only dry-run and require
  explicit `--apply --backup-dir` with verified SQLite online backups before writes;
- guarded ledger reconciliation and autoheal code; Live autoheal is permanently read-only,
  while Demo runtime wiring requires separate explicit opt-ins for exact close and open bookkeeping;
- isolated regression tests that do not connect to production databases, place orders, start Agents or send messages;
- `config.example.md`, `.gitignore`, dependency metadata and bilingual public documentation;
- privacy-preserving aggregate star history data, SVG, generator and scheduled workflow.

## Retained locally but excluded

- `docs/archive/` and `scripts/archive/`;
- internal host runbooks, execution records and OpenClaw baselines;
- full-environment orchestration and host sampling scripts;
- Drill, Phase 5 and legacy/compatibility one-off backfill tools;
- the local ANT/Clash bridge tool tree, which has its own host and network security boundary;
- all credentials, databases, SQLite sidecars, execution journals (`*.jsonl`), logs,
  reports, memory, temporary data, local dependencies and caches.

No current runtime module imports a file from `docs/archive/` or `scripts/archive/`. Active documentation references to excluded history are removed from the public map.

## Public Agent deployment

`README.md` and `README.en.md` provide the public system map. The matching Agent deployment guides describe isolated OpenClaw workspaces, placeholder-only cron definitions, least-privilege boundaries, dry-run validation and rollback.

The guides do not contain real models, tokens, channel destinations, cron job ids, device ids, account ids, host paths, or OpenClaw state. They do not authorize live trading, external delivery, production database writes, or automatic daily maintenance.

## Regression boundary

The published tests cover isolated execution-intent, ledger-position, fill, stop-loss,
writer, dispatcher, report, macro-source and runtime-repair contracts. They are not a
complete money-path or real exchange/OpenClaw end-to-end suite. Scripts excluded above
are also excluded from the public lifecycle manifest and its tests.

## Public write gates

The profile-lease schema migration defaults to inspection and requires `--apply` plus a
verified SQLite backup directory. Live ledger autoheal is permanently read-only at runtime,
direct API, and CLI boundaries; a requested Live write is classified fully, returned as a
non-zero structured block, and never changes the trade ledger or repair queue. Live repairs
remain manual and require one unique exchange `ordId`, verified SQLite backups, one-record apply,
and fresh exchange-position, reconciliation, and invariant checks. For Demo only,
`OKX_LEDGER_AUTOHEAL_APPLY=1` permits exact GHOST close bookkeeping, while an exact
UNRECORDED open additionally requires `OKX_LEDGER_AUTOHEAL_UNRECORDED=1`, a matching
`execution_intent` ordId, and a confirmed existing exchange-side protective stop; no-intent
T2 or missing/unknown-stop findings remain P0 report-only. Neither path places or replays an
order. Existing ledgers fail closed until the lease migration is run with
`--apply --backup-dir`; dispatcher never performs that upgrade implicitly. Business and
alert delivery use separate environment-only destinations and have no embedded fallback
identifiers. Deterministic push and runtime artifacts are namespaced by non-default DB root.
Because OpenClaw Gateway Agent tools do not deterministically inherit the local runner's
environment, real Agent launch with a non-default DB root is rejected; that combination is
available only under trigger dry-run until Gateway-level propagation is separately verified.

## Star statistics

`.github/workflows/update-star-stats.yml` refreshes `docs/data/star-history.json` and `docs/assets/star-history.svg` on a schedule. The generator requests only the repository's aggregate star count and never requests stargazer usernames, ids or avatars.

The workflow uses the repository-scoped `GITHUB_TOKEN` only at runtime. No personal access token or usable credential is stored in the repository.

## Version lineage

The synchronized source identifies itself as V2.0. The remote repository previously used a `v3.1` README label. This change does not create a tag, Release, or final semantic-version decision; the maintainer will choose the public version number separately.
