# Changelog

All notable public-release changes are recorded here. Public versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added a hard rule to the risk gate, ported from V3: a stop-loss farther than
  0.8 × (1/leverage − maintenance margin rate) is rejected as
  `sl_beyond_margin_distance` because liquidation would arrive first; the live
  trader manual states the limit and callers may pass the exchange tier rate.

### Changed

- Similarity retrieval now scores experiences in a v4 feature space aligned with
  the V3 similarity design: 1H ATR%, RSI, EMA20/50/200 alignment, 4h/16h returns,
  volume z-score, stop distance and opening hour combined by geometric mean, with
  a 0.9 cross-symbol factor and a 0.35 default threshold. New rows store v4
  features next to the frozen v3 payload, and older rows are rebuilt as-of their
  own timestamp from the K-line cache so history stays comparable.
- Exit categories are derived from prices as in V3: exchange-side fills resolve to
  `tp_hit`, `sl_hit`, `breakeven_stop` or `trail_stop` by the nearest level within
  1.5% (sign-based fallbacks carry an `_inferred` suffix), agent closes split by
  the recorded invalidation price, and close events now keep the close reason and
  exchange-side flag so backfills classify identically.
- The missed-opportunity simulation follows the V3 rule: stop =
  clamp(1 × ATR1h, 3%, 6%) with a 4% default, take-profit +5%, same-bar double
  touches count as `ambiguous` instead of losses, and rows carry a `sim_rule` tag
  so results computed under the previous rule stay frozen.
- News collectors share one coin-name table with V3's matching rules: the longest
  name wins ("Bitcoin Cash" is BCH, "Ethereum Classic" is ETC), bare tickers must
  be upper-case and at least three letters, Polygon maps to POL and stablecoins
  are not emitted.
- Optimized the experience-feature derivation shared by the trade experience
  writer, the similarity finder and the instrument context against the V3
  similarity design: the strict 24h volatility window now anchors to the 15m bar
  grid (fill-time timestamps previously never matched the grid and left
  `vol_24h_pct` empty), planned RR is derived from `open_execution_package_v1`
  when no decision card exists, numeric inputs are validated as finite, callers
  can request a subset of market features, and one shared builder replaces three
  copies of the stop-distance and RR logic.
- Experience summaries with sufficient samples now also report the Wilson 95%
  lower-bound win rate, similarity-weighted win rate and average return, and the
  mean net R of path-instrumented neighbours; the feature version report
  classifies `v3_epoch_mismatch` rows separately using the same acceptance rule
  as the finder.

### Fixed

- The trade experience writer now stamps `path_metric_version` from the single
  path-metrics source instead of a stale literal, so freshly closed rows are no
  longer re-flagged as outdated by the backfill or read under the retired
  gross-R convention.

## [1.1.2] - 2026-09-14

### Added

- Added the minimal full-closure decision protocol, side-neutral candidate manifests,
  deterministic facts handoff, atomic position-plan publication and independent
  refusal evidence that preserves failed actions while allowing unrelated actions.
- Added optional public-market WebSocket cache and service, market-source diagnostics,
  dated report artifacts, forward-window business and reporting audits, and isolated
  tests for execution, protection, reconciliation and delivery contracts.

### Changed

- Synchronized collectors, writers, role manuals, templates and execution helpers
  with the 2026-09-14 runtime source snapshot. Historical multitimeframe/card evidence
  remains readable; new full-closure decisions use `open_execution_package_v1`.
- Added official tick-size alignment, exact flat-side protection cleanup, bounded
  mark-price read recovery, terminal-state diagnostics and failure-preserving receipts.
- Updated report validation, missed-opportunity evidence, delivery timing and QQ
  Gateway transport. Unknown delivery remains non-retryable and business failures
  remain separate from delivery verification.

### Security

- Retained permanently read-only public autoheal, isolated database-root propagation,
  fail-closed dry runs, explicit migration apply flags and verified SQLite backups.
- Kept credentials, real destinations, runtime databases, incident repair tools,
  host scheduler configuration and private report artifacts outside the release.
- Published synthetic order identities in the close-reconciliation fixture and
  retained the existing protected PR, CI and annotated-tag release workflow.

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

[Unreleased]: https://github.com/asd976385560/AUTO-OKX-USDT-M/compare/v1.1.2...HEAD
[1.1.1]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.1.1
[1.1.0]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.1.0
[1.0.0]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.0.0

[1.1.2]: https://github.com/asd976385560/AUTO-OKX-USDT-M/releases/tag/v1.1.2
