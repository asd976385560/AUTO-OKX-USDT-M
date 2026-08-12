<!--
doc-version: V2.0-security
last-updated: 2026-08-12
updated-by: Codex
change-summary: Reconfirm sanitized runtime boundaries, live-only execution and report-only public autoheal.
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
The public runtime supports only `profile=live`. Legacy autoheal write flags and environment
switches do not authorize bookkeeping writes: they produce a non-zero structured refusal and
leave the trade ledger and repair queue unchanged. No recovery setting authorizes order
placement, amendment, or replay.

CI runs `scripts/check_public_boundary.py` across every tracked path and UTF-8
source file. It blocks runtime database/log/report artifacts, concrete delivery
routes, private IP addresses, user-home paths, and production-root paths while
redacting matched values from its output. This complements the independent
credential scan; neither check replaces credential rotation after a real leak.

## Reporting a leak

Do not paste a suspected secret into a public issue, pull request, commit message, or log. Revoke or rotate the affected credential first, preserve only redacted evidence, and contact the repository owner through a private channel.

If a secret is found in Git history, rotate it immediately. History rewriting requires a separate owner-approved incident procedure and is not part of normal release synchronization.
