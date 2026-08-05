# Changelog

All notable public-release changes are recorded here. Public versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Release validation now preserves the authoritative remote annotated tag in an
  isolated Git ref, and an explicit retry path can republish an existing immutable
  tag without creating, moving or deleting that tag.

## [1.0.0] - 2026-08-04

### Added

- Initial sanitized public release of the V2.0 runtime source, role contracts,
  deterministic trading pipeline, database DDL, templates, and public documentation.
- Portfolio IMR controls for Live, directional real-time max-size controls for Demo,
  profile leases, point-in-time reporting semantics, and split business/alert routing.
- A repository release contract backed by `VERSION`, this changelog, CI validation,
  annotated `vMAJOR.MINOR.PATCH` tags, and gated GitHub Release automation.

### Fixed

- Demo UNRECORDED recovery now terminalizes its execution intent without fabricating
  an executor receipt, and fill reconciliation prefers exact order identity while
  treating equal-size identity-free candidates as ambiguous.
- Non-finite price and instrument inputs now fail closed before order submission;
  unexpected validator exceptions also clean the reserved intent.
- The OpenClaw state database setting now honors the documented prefixed variable,
  with the legacy name retained as a lower-priority compatibility alias.
- Release validation now requires non-empty changelog notes and link references,
  supports chronological stable/prerelease maintenance lines, rechecks current-main
  ancestry before publication, and publishes the validated changelog section.

### Security

- Production databases, execution journals, credentials, destinations, host paths,
  logs, reports, private configuration, and order replay data remain excluded.
- Live ledger autoheal remains permanently read-only; schema migrations remain
  dry-run by default and require explicit apply plus verified backups.
- CI now scans the complete candidate tree for concrete delivery routes, private
  host paths and runtime artifacts without echoing matched values.

[Unreleased]: https://github.com/asd976385560/AUTO-OKX-USDT-M/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.0.0
