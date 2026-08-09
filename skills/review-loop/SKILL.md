---
name: review-loop
version: 0.2.0
description: "Implement a task with a build agent, then have a configurable panel of adversarial reviewer personas review it in parallel, fix the findings, and re-review from scratch until tests pass and no blocking findings remain."
---

# Review Loop

Use this skill when a task is worth more than one pass: anything security-sensitive, anything touching data or a public contract, anything you would normally want a second engineer to look at before merge.

Core loop:

```text
Configure → Implement → Validate → Review (parallel) → Merge findings → Fix → Re-review → Done
```

The point is not "three agents talking". It is a deterministic workflow where models are replaceable workers and a small program decides who acts, when, on what information, and when the job is allowed to finish.

## What runs where

The interactive configuration is yours to run. The loop itself is `scripts/review_loop.py` — stdlib Python 3, no dependencies. It runs detached, so a long loop cannot hit a tool-call timeout or be lost to context compaction.

You are the wizard and the renderer. The script is the orchestrator. Do not reimplement the loop yourself.

## Hard rules

- **Never skip the wizard on the first run in a repository.** Model and reviewer choice is the whole product. Defaults are a convenience, not an assumption.
- **Reviewers are read-only, enforced by CLI flags** (`--permission-mode plan`, `--deny-tool write --deny-tool shell`, `-s read-only`), not by asking politely in a prompt. Copilot's `write` permission explicitly does not cover shell invocations, so its shell is denied outright rather than command by command.
- **Reviewers never see the implementer's reasoning or self-assessment.** They get the task, the repository, and the diff. Nothing else.
- **Every review round starts from scratch.** Never ask "did the implementer fix ARCH-001?". Anchoring on prior findings hides regressions introduced by the fixes.
- **Tests are an independent gate.** Agreement between models is not a definition of correctness. If validation fails, the loop keeps going even when every reviewer approves.
- **A missing answer is never a clean one.** A review counts only if it parses *and* is structurally a review — explicit verdict, `findings` list, objects inside it. If any reviewer fails that, the round is incomplete and the run cannot be approved, no matter how quiet the rest of the panel was.
- **A gate that never ran did not pass.** A run with no validation commands is rejected at launch; approving without one takes an explicit `allow_missing_validation`, and reports as `approved_unverified`.
- **One harness by default.** Every slot uses the CLI you were invoked from unless the user explicitly opts into mixing.
- **Never run this on a dirty working tree** without telling the user. Uncommitted work will be mixed into the diff under review and may be modified by the implementer. The script refuses to start on a dirty or non-git tree unless the config says `allow_dirty` / `allow_non_git`, and it holds a lock so only one run at a time can touch a worktree.

## Token discipline

Be concise. Do not paste the diff, the prompts, or the raw JSON into the conversation. Use `render` for progress. Summarise findings; the full detail is on disk.

The orchestrator is where the real spend happens, and a panel multiplies it — the diff goes to every reviewer, every round. So:

- **Recommend the smallest panel that covers the task.** Two or three reviewers. Every extra reviewer is a whole extra agent invocation per round, and per-invocation overhead dominates the diff itself.
- **Do not raise `max_iterations` above 5** unless asked. Rounds are the most expensive unit in the system.
- **Do not disable `exclude_noise`** unless the user is specifically reviewing a lockfile or generated output.
- **Never re-read files to summarise them yourself.** The reviewers already have the diff; reading it again into your own context buys nothing.

## 1. Preflight

Run the detector:

```bash
python3 <skill-dir>/scripts/review_loop.py detect
```

It returns installed CLIs, their available models and effort ladders, the persona library, and a guess at which harness invoked you. Model availability varies by plan and by org policy — never hardcode a model list, always use what `detect` reports.

Then check the repository state. The script enforces all of this itself and will refuse to launch, but checking here lets you explain the problem instead of relaying an error:

- Confirm you are in a git repository. If not, stop and say so.
- Check `git status`. If the tree is dirty, tell the user what is uncommitted and ask whether to continue, commit first, or stop. Only set `allow_dirty` if they choose to continue.
- Identify validation commands from the project itself — `package.json` scripts, `Makefile`, `pyproject.toml`, CI config. Propose what you find; do not invent commands that do not exist. If the project genuinely has none, say plainly that the run will have no independent gate and that its result is model consensus only, then set `allow_missing_validation`.

If no task was supplied with the invocation, ask for one before anything else.

## 2. Configure

Ask these questions using your host's interactive question mechanism. In Claude Code that is `AskUserQuestion`; in Copilot CLI it is the `ask_user` tool; elsewhere ask in plain text with numbered options. Group them so the user answers in as few steps as possible.

Load `~/.review-loop/defaults.json` if it exists and use it to pre-fill every answer. Offer to save the answers back there at the end of a successful configuration.

**Question 1 — Build agent.** Which model and effort implements the task. Default to the strongest model the invoking CLI offers, at `high`.

**Question 2 — Reviewers.** Do not make the user choose blind. Run:

```bash
python3 <skill-dir>/scripts/review_loop.py suggest --task "<the task>"
```

It reads the task wording and the repository's tracked files and returns a recommended panel with a reason for each. Present those as the pre-selected options, with the rest of the library available underneath. Show the reason — "task mentions oauth, sso" tells the user more than the persona name does.

Two or three reviewers is the useful range. More than four mostly produces duplicate findings and a slower loop; say so if the user picks a large panel.

The same persona may appear twice on different models — that is a legitimate way to get two independent opinions from one lens, and the orchestrator gives each slot its own identity so they can corroborate each other.

**Question 3 — Model and effort per reviewer.** Default every reviewer to the global default. Let the user override each one individually. Reviewers benefit from more effort than the implementer: they get one pass and no feedback.

**Question 4 — Mixing (only if `detect` found more than one CLI).** Default is no. If the user opts in, re-ask question 3 with models from every detected CLI, labelled by which one they come from. Cross-family reviewers disagree more usefully than same-family ones, which is the main reason to bother.

**Question 5 — Build agent permissions.** The build agent has to run tests unattended. Under the default `acceptEdits` it can edit files but shell commands are denied, so it will often report success having verified nothing. Offer:

- **Full permissions (`bypassPermissions`)** — recommended, and strongly recommended *inside a git worktree or container* so an autonomous agent cannot touch anything you care about. Suggest `git worktree add ../<name>-review -b <branch>` and running there.
- **Edits only (`acceptEdits`)** — safer, but the agent probably cannot run your test suite, which weakens the independent gate that makes this tool worth using.

Say which you are using and why. Do not silently pick the permissive one.

Then confirm the run settings, defaulting to: max 5 iterations, blocking severities `blocker`/`high`/`medium`, reviewers in parallel, and the validation commands you found in step 1.

## 3. Launch

Write the config and start the run:

```bash
python3 <skill-dir>/scripts/review_loop.py start --config <path>
```

Config shape:

```json
{
  "task": "Implement configurable rate limiting for the public API.",
  "repo": "/abs/path/to/repo",
  "max_iterations": 5,
  "blocking_severities": ["blocker", "high", "medium"],
  "parallel": true,
  "permission_mode": "acceptEdits",
  "validation": { "commands": ["npm test", "npm run lint", "npm run typecheck"] },
  "require_clean_baseline": false,
  "implementer": { "cli": "claude", "model": "opus", "effort": "high" },
  "reviewers": [
    { "persona": "principal-engineer", "label": "Principal Engineer",
      "cli": "claude", "model": "opus", "effort": "max" },
    { "persona": "adversarial-qa", "label": "Adversarial QA",
      "cli": "codex", "model": "gpt-5.6-luna", "effort": "max" }
  ]
}
```

`start` returns a run directory and exits immediately. It keeps `.review-loop/` out of git by writing a `.gitignore` inside that directory, so it never modifies a file the user tracks.

Safety escape hatches, all off by default. Only set one because the user chose it, and say which you set:

| Key | Effect |
|---|---|
| `allow_dirty` | Start on a working tree with uncommitted changes. |
| `allow_non_git` | Start somewhere that is not a git working tree. Nothing the build agent does will be reviewable or reversible. |
| `allow_missing_validation` | Allow approval with no validation commands. The outcome becomes `approved_unverified`. |
| `require_clean_baseline` | Abort if validation is already failing before the task starts, instead of recording it and continuing. |
| `max_untracked_files` | How many new files are inlined into the diff (default 60). The rest are listed by path so reviewers know to read them. |

Before the build agent runs, the loop records a **baseline** validation result in `validation-00.json`. If the suite was already failing, that is reported to the implementer, to the reviewers, and in `final.md`, so a later failure is never misattributed to the change under review.

Each reviewer slot accepts an optional `config_dir`, which sets `CLAUDE_CONFIG_DIR` or `CODEX_HOME` for that agent. Use it only if the user has a second authenticated account and asks for it — it spreads rate limits across subscriptions. It is off by default.

## 4. Report progress

Do not read `progress.jsonl` yourself or reformat it. Run:

```bash
python3 <skill-dir>/scripts/review_loop.py render
```

It prints the finished tree. Pass it through to the user as-is — it is already the output format, and re-rendering it wastes tokens and drifts between runs.

```text
review-loop · 2 reviewers · max 5 rounds
  panel: principal-engineer, adversarial-qa

● Implementer — claude opus @ high
  ✓ complete (51.6s)

● Review round 1
  ✓ validation: npm test
  ├─ Principal Engineer
  │  1 high, 2 medium
  └─ Adversarial QA
     1 blocker, 1 medium
  → 4 findings after merge · 4 blocking · validation passing
```

Poll no faster than every 30 seconds.

Between polls, tell the user they can keep working and that `status` and `stop` are available.

Two events need surfacing immediately rather than at the end:

- **`permission_denied`** — the build agent was blocked from running commands, so its "success" is unverified. Tell the user at once and offer to stop and restart with full permissions.
- **`review_unparsed`** — a reviewer contributed nothing usable: it failed, returned output that would not parse or that was not a well-formed review (no verdict, a `findings` value that is not a list of objects), or requested changes without naming a single finding. The `reason` field says which. Its findings are missing from the round, so the panel is smaller than the user thinks. Say so rather than reporting a clean review. The loop will not approve a round in this state; it ends as `review_incomplete`.

## 5. Finish

When `run_complete` appears, read `final.md` from the run directory and report:

- the outcome
- rounds run, findings raised, findings resolved — `final.md` carries all of these, because the loop keeps a ledger of every finding across rounds rather than only the last round's list
- validation status, including whether it was already failing before the task started
- any finding the implementer rejected, with its stated evidence — those are in `implementer-NN.md`
- anything still outstanding, including any finding marked as having come back after being fixed

Then show the user the diff summary and hand off. Do not commit or push unless asked.

Outcomes and what to do about them:

| Outcome | Exit | Meaning |
|---|---|---|
| `approved` | 0 | Every reviewer reported, nothing blocking is open, validation passed. |
| `approved_unverified` | 0 | Same, but no validation commands existed. Model consensus only — say so. |
| `review_incomplete` | 2 | A reviewer contributed nothing. Part of the change went unreviewed. Not an approval. |
| `validation_not_configured` | 2 | Nothing blocking is open, but there was no gate and no explicit opt-in. |
| `baseline_failed` | 1 | Validation was failing before the task started, with `require_clean_baseline` set. |
| `max_iterations_reached` | 2 | Hit the cap with blocking findings open. |
| `no_progress` | 2 | A fix round changed nothing the reviewers cared about. |
| `implementer_failed` | 1 | The build agent could not complete a round. |

**If the outcome is `max_iterations_reached`, `no_progress` or `review_incomplete`, say so plainly and stop.** The first two mean the panel and the implementer did not converge. `no_progress` specifically means a fix round changed nothing the reviewers cared about — re-running would cost another full panel to receive the same answer. `review_incomplete` means part of the change was never reviewed; offer to re-run that reviewer, and never describe the result as clean. Do not raise the cap or restart without being asked.

## Severity policy

| Severity | Behaviour |
|---|---|
| `blocker` | Must fix. Loops. |
| `high` | Must fix. Loops. |
| `medium` | Normally fix. Loops by default. |
| `low` | Recorded in `final.md`. Does not loop. |
| `nit` | Ignored. |

Findings marked `corroborated_by` were raised independently by more than one reviewer. Treat them as high-confidence and mention them explicitly when reporting.

## Files it writes

```text
.review-loop/
  .gitignore           makes this directory invisible to git
  task.md
  final.md
  current-run
  lock                 one active run per worktree
  history/run-NN/
    config.json          resolved configuration
    progress.jsonl       event stream — poll this
    findings-NN.json     merged, deduplicated, this round only
    ledger.json          every finding across all rounds, with its state
    review-NN-<persona>.json
    validation-00.json   baseline, before the build agent ran
    validation-NN.json
    implementer-NN.md
    final.md
    logs/                full transcript per agent invocation
```

## Stop conditions

Stop and hand back to the user when:

- the repository is not a git repository, or the tree is dirty and they have not chosen how to proceed
- another run is already active in this worktree
- `detect` finds no usable CLI
- the project has no validation commands and the user has not accepted an unverified run
- the implementer fails twice in a row
- a reviewer returns unparseable output twice for the same round — the run ends as `review_incomplete`, which is not an approval
- the iteration cap is reached with blocking findings outstanding
- validation was already failing before the loop started — the loop records this as a baseline and continues, but say so; fix it first unless fixing it *is* the task
