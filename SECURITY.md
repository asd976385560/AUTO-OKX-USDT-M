# Security Policy

## Public repository boundary

This repository must not contain credentials, private configuration, trading databases, account or order data, logs, reports, memory, push targets, device identifiers, or private network addresses.

Use environment variables or an ignored local `config.md`. Keep `OKX_EXECUTOR_DRYRUN=1` and `OKX_TRIGGER_DRYRUN=1` while validating a new deployment.

## Reporting a leak

Do not paste a suspected secret into a public issue, pull request, commit message, or log. Revoke or rotate the affected credential first, preserve only redacted evidence, and contact the repository owner through a private channel.

If a secret is found in Git history, rotate it immediately. History rewriting requires a separate owner-approved incident procedure and is not part of normal release synchronization.
