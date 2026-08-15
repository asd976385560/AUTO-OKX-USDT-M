# Changelog

All notable public-release changes are recorded here. Public versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.1] - 2026-08-16

### Added

- Added OKX announcement collection, official instrument and contract-history
  snapshots, kline BOLL/OBV evidence, positioning batch identity, and bounded
  recovery for incomplete market-feature collection.
- Added complete-cycle SLA, market-field, market-feature, positioning, periodic
  report, and delivery audits, plus deterministic live-position action handling
  and explicit stage side-effect failure receipts.

### Changed

- Extended the live decision contract with REDUCE and ADJUST_PROTECTION,
  explicit exit modes, exact multitimeframe selection evidence, deadline-aware
  analysis writes, and stronger report-to-exchange attestations.
- Updated collectors, writers, dispatcher, executor, report pipelines, role
  manuals, templates, schema export, lifecycle metadata, and isolated tests to
  the sanitized 2026-08-15 runtime snapshot.
- Corrected the Push documentation to the current 16 static required sections;
  versioned multitimeframe and execution evidence remain independent hard gates.

### Security

- Retained project-root portability, isolated database-root propagation,
  fail-closed dry-run behavior, permanently read-only public autoheal, and
  verified pre-write SQLite backups for the newly synchronized migrations.
- Excluded credentials, routing destinations, host-specific scheduler helpers,
  databases, logs, runtime state, real-order microtests, and incident-specific
  repair utilities from the public synchronization.

## [1.1.0] - 2026-08-12

### Added

- Synchronized the sanitized public tree with the current live-only runtime,
  including consolidated hourly and quarter-hour collection runners, per-step
  collection evidence, and the unified Live-to-Push dispatch chain.
- Added exact closed-bar 15m/1H/4H decision evidence, independent writer and
  executor revalidation, actor attestation, asset-class and instrument context,
  EV calculations, news time layers, and versioned experience contracts.
- Added source-health, report-completeness, positioning, multitimeframe,
  contract-statistics, and model-shadow audit tools, plus a 17-item Push report
  contract and hardened periodic-report validation.

### Changed

- Retired Demo execution, its Agent role, database initialization target, and
  automatic dispatch path. Trading entry points now accept only `profile=live`
  and fail closed for every other profile.
- Consolidated the former independent fast, slow, and registry-news schedules
  into deterministic aggregate runners while preserving source-level failure
  isolation and read-only dry-run support.
- Updated all public role manuals, deployment guides, templates, lifecycle
  metadata, schema exports, and bilingual system documentation to match the
  current runtime and portable project-root contract.

### Fixed

- Added a 15% equity cap for incremental order IMR, a 5% equity cap for
  stop-loss risk, finite-number validation, and post-fill audits so NaN, infinity,
  oversized orders, or inconsistent risk evidence cannot reach exchange I/O.
- Namespaced session, status, deduplication, journal, and Push artifacts by the
  selected database root; invalid cycle identifiers and real Agent launches
  against non-default roots now fail before creating runtime artifacts.
- Bound analyst, trade, collection-monitor, reconciliation, and Push reads to
  the explicitly selected root, preventing isolated validation from falling
  back to canonical runtime databases.
- Release validation preserves the authoritative remote annotated tag in an
  isolated Git ref, and an explicit retry path can republish an existing
  immutable tag without creating, moving, or deleting it.
- Declared the NumPy and pandas dependencies required by the published
  multitimeframe diagnostics, and isolated the persistent-dispatch latch test
  from CI's global trigger dry-run guard.

### Security

- Public ledger autoheal is permanently report-only. Direct write flags and
  legacy write environment settings return a structured non-zero refusal and
  never modify a trade ledger, repair queue, or exchange order.
- Published migrations default to read-only inspection and require explicit
  apply authorization plus a verified SQLite online backup before any target
  write; failed preflight leaves every target unchanged.
- Removed host-specific paths, credentials, routing identifiers, runtime data,
  private exchange-auth helpers, real-order microtests, account-history tools,
  OpenClaw host baselines, and incident-repair utilities from the public tree.

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

[Unreleased]: https://github.com/asd976385560/AUTO-OKX-USDT-M/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.1.1
[1.1.0]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.1.0
[1.0.0]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.0.0
