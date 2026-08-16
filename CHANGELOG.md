# Changelog

All notable changes to review-loop are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-08-16

### Changed

- Default non-Claude builders to GPT-5.6 Sol at medium effort and reviewers to
  GPT-5.6 Luna at xhigh effort, with availability-aware model fallback.
- Default Claude reviewers to the rolling Opus alias at low effort while
  retaining Opus at high effort for the builder.
- Document how to update installer, clone, copy, and plugin-based skill
  installations and reload each host afterward.
- Add a one-command release preparation workflow that updates manifests and
  changelog metadata, then runs every required local release check.

## [0.3.0] - 2026-08-12

### Added

- Credential-free fake-CLI end-to-end coverage for clean approval, validation
  and reviewer-driven repair, reviewer failure, permission mappings, detached
  completion, same-worktree locking, non-Git operation, snapshot invalidation,
  and fail-closed Git errors.
- Process-tree timeout tests and isolated installer integration tests across
  copy, symlink, update, uninstall, spaces, and custom Claude config paths.

### Changed

- Make clean baseline validation the safe default; baseline-repair tasks must
  explicitly set `require_clean_baseline: false`.
- Canonicalize configured repository subdirectories to the actual Git
  worktree root for state, locking, validation, diffs, and agent execution.
- Move owned state and the live lock outside the worktree, authenticate explicit
  run paths, and reject symlinked or unrecognized state objects.
- Map `acceptEdits` and `bypassPermissions` truthfully for Claude, Copilot, and
  Codex while preserving enforced read-only reviewer isolation.
- Use Claude Code JSON Schema output when the installed CLI supports it, with
  safe fallback for older versions.
- Require a monotonic blocking-severity threshold and bound panel, command,
  timeout, input, diff, and transcript sizes before launch.

### Fixed

- Fail closed on invalid runtime configuration, Git safety/diff failures,
  nonzero agent exits, and unexpected orchestration exceptions while still
  producing terminal events, a final report, and releasing the lock.
- Terminate complete agent and validation process trees on timeout (including
  descendants in new sessions that clear their environment), make forced stop
  drain active trees before lock reclamation, and retain bounded partial
  stdout/stderr for diagnosis.
- Refuse to recursively replace or uninstall an unrecognized installer
  destination; quarantine and revalidate exact destination-bound ownership
  before deletion, and serialize concurrent installer operations.
- Validate the complete fallback review schema and invalidate approval when the
  worktree changes between validation/diff collection and reviewer completion.
- Preserve partial run history and failure stage/detail in unexpected terminal
  reports, and make stdout event echo best-effort after durable recording.

## [0.2.0] - 2026-08-09

### Added

- Baseline validation, explicit validation states, and an
  `approved_unverified` outcome for deliberately ungated runs.
- Atomic per-worktree locking and clean-repository safety checks.
- A persistent finding ledger that tracks resolved and reappeared findings
  across review rounds.
- Configurable reviewer suggestions based on task keywords and repository
  signals.
- Progress rendering, bounded agent transcripts, and token-usage reporting.
- Diff-noise filtering for generated files, lockfiles, snapshots, and other
  high-volume inputs.

### Changed

- Treat failed, malformed, or empty reviewer output as an incomplete review,
  never as approval.
- Enforce Copilot reviewer isolation by denying shell access as well as write
  tools.
- Stop early when an implementation round makes no progress.
- Preserve independently corroborated findings while deduplicating equivalent
  reports.

### Fixed

- Prevent concurrent runs from modifying the same worktree.
- Prevent missing validation commands and missing reviewer responses from
  being represented as successful gates.
- Prevent untracked symlinks and excluded state files from leaking unrelated
  content into review payloads.

## [0.1.0] - 2026-08-09

### Added

- Initial adversarial review-loop skill for Codex, Claude Code, and GitHub
  Copilot CLI.
- Parallel reviewer personas, structured findings, validation gates, and
  iterative implementer handoffs.
- Universal symlink-or-copy installer.

[Unreleased]: https://github.com/bransbury/review-loop/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/bransbury/review-loop/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/bransbury/review-loop/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/bransbury/review-loop/releases/tag/v0.2.0
[0.1.0]: https://github.com/bransbury/review-loop/commit/1f6fceb
