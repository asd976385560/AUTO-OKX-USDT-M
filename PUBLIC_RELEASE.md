<!--
doc-version: V2.0-public-scope
last-updated: 2026-08-04
updated-by: Codex
change-summary: Define public version 1.0.0 and a gated annotated-tag GitHub Release workflow.
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
- `VERSION`, `CHANGELOG.md`, release-contract validation and the gated tag-to-Release workflow;
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

## Public release versioning

Public releases follow Semantic Versioning. `VERSION` is the single release-version
source and `CHANGELOG.md` contains the matching dated, non-empty release entry and
link references. Git tags and GitHub Releases add the `v` prefix.

The internal V2.0 identifier remains the authority for architecture, business
contracts, document headers and schema lineage. It is deliberately independent from
the public release number and is not rewritten during a release bump.

Every release must follow this sequence:

1. update `VERSION` and add the matching newest entry to `CHANGELOG.md` in a PR;
2. pass the complete isolated CI, public-contract checks and an independent
   redacted secret scan, including `scripts/check_release_version.py`;
3. merge the reviewed PR into `main` and confirm the `main` CI result;
4. create an annotated `v<version>` tag on that exact `main` commit and push only the tag;
5. let `.github/workflows/release.yml` verify the tag object, version/changelog match,
   `main` ancestry and reusable CI before it creates the GitHub Release.

The release workflow never creates a tag. It uses `--verify-tag`, rechecks the tag
commit against the current `main` immediately before publication, publishes the
validated matching `CHANGELOG.md` section as release notes, and marks SemVer
prerelease identifiers such as `1.1.0-beta.1` as GitHub prereleases.

Before the first tag is pushed, the repository owner should protect `main`, restrict
creation of release tags to maintainers, block updates and deletions for `v*`, and
configure approval rules on the `github-release` Environment when a manual publication
gate is desired. Workflow self-checks complement but do not replace repository rules.
