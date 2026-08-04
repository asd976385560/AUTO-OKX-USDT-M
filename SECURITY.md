<!--
doc-version: V2.0-security
last-updated: 2026-08-04
updated-by: Codex
change-summary: Reconfirm environment-only delivery targets and explicit database-write gates for the synchronized release.
-->

# Security Policy

## Public repository boundary

This repository must not contain credentials, private configuration, trading databases,
SQLite sidecars, execution journals, account or order data, logs, reports, memory, push
targets, device identifiers, or private network addresses.

Use environment variables or an ignored local `config.md`. Keep `OKX_EXECUTOR_DRYRUN=1` and `OKX_TRIGGER_DRYRUN=1` while validating a new deployment.

Business and alert destinations must be supplied separately through `OKX_QQ_TARGET` and
`OKX_QQ_ALERT_TARGET`; there is no built-in fallback. Schema migration writes require
their documented `--apply --backup-dir` gate. Live ledger autoheal is permanently read-only,
including direct API/CLI calls; Live repair remains a unique-ordId, verified-backup,
one-record-at-a-time manual workflow with fresh post-write reconciliation and invariant checks.
The close/open opt-ins authorize Demo only. Demo UNRECORDED bookkeeping requires matching
intent/ordId evidence and a confirmed active reduce-only stop covering the same position;
missing or unknown protection remains report-only. These
settings do not authorize order placement or replay.

## Reporting a leak

Do not paste a suspected secret into a public issue, pull request, commit message, or log. Revoke or rotate the affected credential first, preserve only redacted evidence, and contact the repository owner through a private channel.

If a secret is found in Git history, rotate it immediately. History rewriting requires a separate owner-approved incident procedure and is not part of normal release synchronization.
