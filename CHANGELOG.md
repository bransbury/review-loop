# Changelog

All notable changes to review-loop are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-08-20

### Fixed

- Stop terminating agents for being verbose. `max_log_chars` was passed to the
  process runner as a leash: crossing it sent SIGTERM/SIGKILL mid-run and the
  dead agent was reported as a failure. It now governs only how much of a
  transcript is retained, which is what its name always implied. Long reviews
  and large implementations were being destroyed at the exact point they were
  doing the most work, and every token they had spent was discarded.
- Stop rejecting an agent's result because its transcript was long. The result
  file was checked against the log-retention budget; it now has its own
  ceiling, since how much an agent explored says nothing about whether its
  verdict is valid.
- Keep the tail when truncating error text. Errors were cut with `[:600]`,
  which preserved the CLI's startup banner and discarded the actual reason
  appended at the end — leaving runs undiagnosable. All error emissions now
  keep both ends.
- A validation command that passes while printing a lot is no longer reported
  as a failing gate.
- An empty diff can no longer be approved. Reviewers handed nothing approve in
  seconds, and the run reported that as `approved` — a clean verdict over a
  change nobody made. The run now ends as the new `empty_diff` outcome without
  spending a panel.
- `render` no longer head-slices agent errors, which had been re-hiding the
  cause that the event stream now preserves.

### Added

- `base_ref`: the commit to diff from, so a finished branch can be reviewed
  against the commit its work started from. Previously the base was always
  HEAD, so already-committed work diffed against itself and the panel was
  handed nothing. Must resolve to a commit and be an ancestor of HEAD.
- `review_only`: skip the initial implementation pass and go straight to
  review. Pointing a build agent at a finished branch invites it to
  re-implement what is already there. The implementer still runs for fix
  rounds. Requires `base_ref` or `allow_dirty`.
- `min_iterations`: review rounds to run even when a round comes back clean.
  `max_iterations` is a cap, not a target, so a clean first round used to end a
  run where two independent panels had been asked for.
- Codex token usage is now recorded and included in the `spend:` line. Only
  Claude reported usage before, so Codex runs showed no spend at all. Copilot
  still reports nothing: the adapter passes `--silent`, which suppresses its
  stats. That is now documented at the parser rather than left looking like an
  oversight.
- Codex's startup banner and internal tracing are stripped from error summaries,
  so a failure leads with its actual reason instead of a models-cache warning.
  The full transcript is still written to `logs/`, and a failure whose only
  output was the banner still reports the banner rather than nothing.
- Provider rate limits are detected and reported as the new `rate_limited`
  outcome, separately from agent failure. Hitting a usage or session limit
  previously surfaced as an agent with an empty error message, which the loop
  treated as "this reviewer contributed nothing" — so it carried on, shrank the
  panel without saying so, and retried the build agent twice more against the
  same wall in under four seconds. Detection is structural where the CLI allows
  it (Claude's `api_error_status`), and phrase-based otherwise; the bare number
  429 is never treated as a signal. The reset time is parsed out of the
  message when present.
- `rate_limit_wait_seconds` (default 0): let a detached run sleep until the
  quota resets and retry the cut-off agent once, instead of ending. Off by
  default — a run should not silently sleep for hours nobody asked for.

- `stop --kill` no longer leaves a run with no terminal report. It SIGTERMs the
  orchestrator deliberately, but nothing handled the signal, so the process
  died mid-round: no `run_complete`, no `final.md`, and a caller polling the
  event stream waited forever for an event that was never coming. SIGTERM now
  unwinds through the normal terminal path and reports `stopped_by_user`.
- An agent killed while a stop is pending is reported as `stopped_by_user`
  rather than `implementer_failed`.
- `final.md` no longer claims "Validation: NOT CONFIGURED — nothing was
  independently verified" when the build agent failed after the baseline gates
  had passed. It now reports the baseline result that actually ran.

### Changed

- Reviewer concurrency now defaults to 2 (`max_parallel_reviewers`), down from
  an effective 4. Reviewers bill one account unless spread across `config_dir`s
  and they all start together, immediately after the build agent's longest
  session — the worst moment to burst. Serialising costs wall-clock, which a
  detached run has to spare.
- The reviewer prompt now bounds repository exploration, with the budget
  configurable via `reviewer_read_budget` (default 25 files). Reviewers were
  reading many times the diff's worth of surrounding code, repeated by every
  reviewer on every round.
- A large diff is reported once, with the panel multiplication done, as a
  `large_diff` event — the only warning about task size that can be made from
  inside the loop.
- The default Claude build agent is now Sonnet at high effort; reviewers stay
  on Opus. The builder runs the longest agentic session and repeats it every
  fix round: measured runs put it at 49–93% of total spend, against a panel
  costing a fraction of that. The strong model now goes where the value is,
  matching what the Codex adapter already did (Sol builds, Luna reviews). The
  wizard still offers a stronger builder for tasks that warrant one.

Repository reads (`_git_capture`) deliberately keep the strict behaviour via a
new explicit `fail_on_overflow`: truncated repository state is not a smaller
truth.

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

[Unreleased]: https://github.com/bransbury/review-loop/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/bransbury/review-loop/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/bransbury/review-loop/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/bransbury/review-loop/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/bransbury/review-loop/releases/tag/v0.2.0
[0.1.0]: https://github.com/bransbury/review-loop/commit/1f6fceb
