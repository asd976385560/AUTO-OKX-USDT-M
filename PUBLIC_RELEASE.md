# Public release scope

This file records the intended V2.0 public synchronization boundary.

## Included

- deterministic collectors, writers, risk validation, order execution and dispatcher code;
- Agent role sources and report templates;
- `db/schema.sql` without runtime databases;
- `config.example.md`, `.gitignore`, public documentation and dependency metadata.

## Retained locally but excluded

- `docs/archive/` and `scripts/archive/`;
- internal host runbooks, execution records and OpenClaw baselines;
- full-environment orchestration and host sampling scripts;
- Drill, Phase 5 and one-off migration/backfill compatibility tools;
- all credentials, databases, logs, reports, memory, temporary data, local dependencies and caches.

No current runtime module imports a file from `docs/archive/` or `scripts/archive/`. Active documentation references to excluded history are removed from the public map.

## Version lineage

The synchronized source identifies itself as V2.0. The remote repository previously used a `v3.1` README label. This change does not create a tag, Release, or final semantic-version decision; the maintainer will choose the public version number separately.
