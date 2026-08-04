# Changelog

All notable public-release changes are recorded here. Public versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-04

### Added

- Initial sanitized public release of the V2.0 runtime source, role contracts,
  deterministic trading pipeline, database DDL, templates, and public documentation.
- Portfolio IMR controls for Live, directional real-time max-size controls for Demo,
  profile leases, point-in-time reporting semantics, and split business/alert routing.
- A repository release contract backed by `VERSION`, this changelog, CI validation,
  annotated `vMAJOR.MINOR.PATCH` tags, and gated GitHub Release automation.

### Security

- Production databases, execution journals, credentials, destinations, host paths,
  logs, reports, private configuration, and order replay data remain excluded.
- Live ledger autoheal remains permanently read-only; schema migrations remain
  dry-run by default and require explicit apply plus verified backups.

[Unreleased]: https://github.com/asd976385560/AUTO-OKX-USDT-M/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.0.0
