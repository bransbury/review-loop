# Changelog

All notable changes to review-loop are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/bransbury/review-loop/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/bransbury/review-loop/releases/tag/v0.2.0
[0.1.0]: https://github.com/bransbury/review-loop/commit/1f6fceb
