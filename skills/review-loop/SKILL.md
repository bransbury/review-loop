---
name: review-loop
version: 0.5.0
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
- **Reviewers are read-only, enforced by CLI flags** (`--permission-mode plan --setting-sources ""`, explicit Copilot read-only approval plus write/shell/MCP denial, `-s read-only`), not by asking politely in a prompt. Permission-affecting Copilot environment variables are removed before launch, and Claude project settings/hooks are not loaded for reviewer sessions.
- **Reviewers never see the implementer's reasoning or self-assessment.** They get the task, the repository, and the diff. Nothing else.
- **Every review round starts from scratch.** Never ask "did the implementer fix ARCH-001?". Anchoring on prior findings hides regressions introduced by the fixes.
- **Tests are an independent gate.** Agreement between models is not a definition of correctness. If validation fails, the loop keeps going even when every reviewer approves.
- **A missing answer is never a clean one.** A review counts only if it parses *and* is structurally a review — explicit verdict, `findings` list, objects inside it. If any reviewer fails that, the round is incomplete and the run cannot be approved, no matter how quiet the rest of the panel was.
- **An empty diff is not a clean review.** If there is nothing to review, the panel will approve in seconds and mean nothing by it. The run ends as `empty_diff`. When the work is already committed, set `base_ref` to the commit it started from — do not reach for `allow_dirty` to manufacture a diff.
- **A gate that never ran did not pass.** A run with no validation commands is rejected at launch; approving without one takes an explicit `allow_missing_validation`, and reports as `approved_unverified`.
- **A failing baseline stops by default.** Do not silently expand the task to pre-existing failures. Only set `require_clean_baseline: false` when the user confirms that repairing the baseline is part of the task.
- **A provider limit is not a review.** When an agent is cut off because the account ran out of quota, the run ends as `rate_limited`. A reviewer that never ran did not examine the code and find nothing. Never describe such a round as clean, and do not immediately re-run: the next round hits the same wall.
- **One harness by default.** Every slot uses the CLI you were invoked from unless the user explicitly opts into mixing.
- **Never run this on a dirty working tree** without telling the user. Uncommitted work will be mixed into the diff under review and may be modified by the implementer. The script refuses to start on a dirty or non-git tree unless the config says `allow_dirty` / `allow_non_git`, and it holds a lock so only one run at a time can touch a worktree.

## Token discipline

Be concise. Do not paste the diff, the prompts, or the raw JSON into the conversation. Use `render` for progress. Summarise findings; the full detail is on disk.

The orchestrator is where the real spend happens, and a panel multiplies it — the diff goes to every reviewer, every round. So:

- **Recommend the smallest panel that covers the task.** Two or three reviewers. Every extra reviewer is a whole extra agent invocation per round, and per-invocation overhead dominates the diff itself.
- **Reviewers are the panel, the build agent is the bill.** Measured runs put the builder at half to over nine tenths of total spend, because it runs the longest session and repeats it every fix round. Reach for a smaller task before a smaller panel.
- **Reviewers explore the repository, and that costs more than the diff.** A reviewer typically reads many times the diff's worth of surrounding code. `reviewer_read_budget` (default 25 files) is the lever; lower it for a narrow change, raise it when a reviewer genuinely needs to trace a wide call graph.
- **Do not raise `max_parallel_reviewers`** just to finish sooner. Reviewers all bill one account unless spread across `config_dir`s, and they start together right after the build agent's heaviest session. Wall-clock is the cheap resource in a detached run.
- **Do not raise `max_iterations` above 5** unless asked. Rounds are the most expensive unit in the system.
- **Do not disable `exclude_noise`** unless the user is specifically reviewing a lockfile or generated output.
- **Never re-read files to summarise them yourself.** The reviewers already have the diff; reading it again into your own context buys nothing.

`render` prints a `spend:` line whenever the CLI reports usage — Claude gives a full breakdown with cost, Codex a single token total per invocation. Quote it when reporting a finished run: the cost of a panel is otherwise invisible until the bill arrives. Copilot reports nothing, because the adapter invokes it with `--silent`; say so plainly rather than implying a Copilot run was cheap.

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

**Then check the task is one PR, not three.** Cost scales with scope worse than linearly: a bigger task means a longer build session, a bigger diff paid for by every reviewer on every round, and more rounds to converge. If the task spans several independent concerns — a migration *and* an API change *and* a UI surface — say so and offer to split it with the `shape` skill, then run the loop per piece. Splitting is cheaper than a wide panel, and the findings are better because each panel sees a diff it can hold at once. The loop also emits a `large_diff` event once the payload passes the warning threshold; pass that on when it appears, because by then the only remaining lever is the next run.

## 2. Configure

Ask these questions using your host's interactive question mechanism. In Claude Code that is `AskUserQuestion`; in Copilot CLI it is the `ask_user` tool; elsewhere ask in plain text with numbered options. Group them so the user answers in as few steps as possible.

Load `~/.review-loop/defaults.json` if it exists and use it to pre-fill every answer. Offer to save the answers back there at the end of a successful configuration.

**Question 1 — Build agent.** Which model and effort implements the task. In Claude Code, default to Sonnet at `high`. In non-Claude hosts (Codex, Cursor, VS Code/Copilot, and similar), default to GPT-5.6 Sol at `medium` when `detect` reports it; otherwise use the detected harness fallback.

The builder is the single most expensive role in the system, by a wide margin. It runs the longest agentic session, and it runs again on every fix round; measured runs have put it at half to over nine tenths of total spend. The panel is what makes the loop worth running, so the strong model goes there. Offer the strongest builder explicitly for tasks that warrant it — a large refactor, an unfamiliar codebase, anything where a weak first draft would just generate findings — and say that is what you are doing. Never quietly upgrade it.

**Question 2 — Reviewers.** Do not make the user choose blind. Run:

```bash
python3 <skill-dir>/scripts/review_loop.py suggest --task "<the task>"
```

It reads the task wording and the repository's tracked files and returns a recommended panel with a reason for each. Present those as the pre-selected options, with the rest of the library available underneath. Show the reason — "task mentions oauth, sso" tells the user more than the persona name does.

Two or three reviewers is the useful range. More than four mostly produces duplicate findings and a slower loop; say so if the user picks a large panel.

The same persona may appear twice on different models — that is a legitimate way to get two independent opinions from one lens, and the orchestrator gives each slot its own identity so they can corroborate each other.

**Question 3 — Model and effort per reviewer.** Let the user override each reviewer individually. In Claude Code, default every reviewer to the rolling Opus alias at `low` — the strong model belongs on the panel. In non-Claude hosts, default every reviewer to GPT-5.6 Luna at `xhigh` when `detect` reports it; otherwise use that harness's detected reviewer fallback.

**Question 4 — Mixing (only if `detect` found more than one CLI).** Default is no. If the user opts in, re-ask question 3 with models from every detected CLI, labelled by which one they come from. Cross-family reviewers disagree more usefully than same-family ones, which is the main reason to bother.

**Question 5 — Build agent permissions.** The build agent has to work unattended. Only `acceptEdits` and `bypassPermissions` are supported, and their exact effect is adapter-specific: Claude uses its native modes; Copilot's `acceptEdits` approves its local write permission but denies the shell and does not blanket-approve MCP tools, while its bypass maps to `--allow-all`; Codex maps them to `workspace-write` and `--dangerously-bypass-approvals-and-sandbox`. Offer:

- **Full permissions (`bypassPermissions`)** — recommended, and strongly recommended *inside a git worktree or container* so an autonomous agent cannot touch anything you care about. Suggest `git worktree add ../<name>-review -b <branch>` and running there.
- **Bounded editing (`acceptEdits`)** — safer. Copilot cannot run shell tests in this mode; Codex can run them inside its workspace sandbox; Claude follows its native permission contract.

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
  "require_clean_baseline": true,
  "implementer": { "cli": "claude", "model": "opus", "effort": "high" },
  "reviewers": [
    { "persona": "principal-engineer", "label": "Principal Engineer",
      "cli": "claude", "model": "opus", "effort": "low" },
    { "persona": "adversarial-qa", "label": "Adversarial QA",
      "cli": "codex", "model": "gpt-5.6-luna", "effort": "xhigh" }
  ]
}
```

`start` returns a run directory and exits immediately. State and the live lock are stored outside the worktree: under Git's per-worktree administrative directory for Git runs, or under the private keyed `~/.review-loop/state/non-git/` root for explicit non-Git runs. This means repository files, symlinks, and `git clean` cannot overwrite history or remove the active lock.

Safety escape hatches are off by default. Clean-baseline enforcement is on by default. Only use an override because the user chose it, and say which you set:

| Key | Effect |
|---|---|
| `allow_dirty` | Start on a working tree with uncommitted changes. |
| `allow_non_git` | Start somewhere that is not a git working tree. Nothing the build agent does will be reviewable or reversible. |
| `allow_missing_validation` | Allow approval with no validation commands. The outcome becomes `approved_unverified`. |
| `require_clean_baseline: false` | Continue after a failing baseline only when repairing it is explicitly within task scope. |
| `max_untracked_files` | How many new files are inlined into the diff (default 60). The rest are listed by path so reviewers know to read them. |

Two further keys shape *what* gets reviewed and *how many times*. Neither is a safety override:

| Key | Effect |
|---|---|
| `base_ref` | The commit to diff from. Defaults to HEAD at launch, which is right when the build agent is about to make the changes. Set it to review work that is **already committed** — `"base_ref": "004de6f0"` reviews everything since that commit. It must resolve to a commit and be an ancestor of HEAD; the run refuses to start otherwise. |
| `min_iterations` | Review rounds to run even when a round comes back clean (default 1). `max_iterations` is a cap, not a target: without this, a clean first round ends the run. Set it to 2 when you want a second independent panel over the same code. Rounds are the most expensive unit in the system — raise it deliberately. |
| `large_diff_warning_tokens` | When a round's diff exceeds this many approximate tokens, the loop emits a `large_diff` event with the panel multiplication already done (default 25,000). It does not change behaviour — it is there so the cost of an oversized task is visible while there is still a decision to make. |
| `rate_limit_wait_seconds` | How long the loop may sleep waiting for a provider quota to reset (default 0, meaning do not wait). When the limit message names a reset time the loop waits for it, otherwise it backs off within this budget, and it retries the cut-off agent once. Useful for a detached overnight run; leave it at 0 when you want to know immediately. |
| `review_only` | Skip the initial implementation pass and go straight to review. Use it to audit work that is already written; pointing a build agent at a finished branch invites it to re-implement what is already there. The implementer still runs for fix rounds, once a reviewer raises something concrete. Requires `base_ref` or `allow_dirty` — something has to be in the diff. |

### Reviewing work that is already finished

This is a first-class use of the loop, not a workaround. To audit commits `004de6f0..f36c92f3`:

```json
{
  "base_ref": "004de6f0",
  "review_only": true,
  "min_iterations": 2
}
```

`base_ref` is the commit **before** the work, and it is excluded from the diff — the same meaning as `git diff <base>..HEAD`. When someone describes the work as "commits A..B", confirm which they mean before launching:

- `"base_ref": "A"` reviews everything *after* A.
- `"base_ref": "A~1"` reviews everything *including* A.

The two can differ by a great deal. Check with `git diff --stat <base>..HEAD` and say which you used.

Do not instead write a task prompt that asks the build agent to do nothing, and do not set `allow_dirty` to manufacture a diff out of committed work. Both leave the run one mistake away from reviewing an empty diff.

Before the build agent runs, the loop records a **baseline** validation result in `validation-00.json`. A failure ends as `baseline_failed` before the agent runs. With the explicit repair-task override, the failure is instead reported to the implementer, reviewers, and `final.md`.

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
- **`rate_limited`** — the account ran out of quota mid-run. The event carries the provider's message and, where it can be read, the seconds until reset. Say so at once, with the reset time; the run is over and anything already spent on that round bought nothing.
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
| `rate_limited` | 2 | The account hit its usage or session limit mid-run. Not an approval, and not an agent failure. Wait for the reset, or set `rate_limit_wait_seconds`. |
| `empty_diff` | 2 | There was nothing to review. Either the build agent changed nothing, or `base_ref` needs to name the commit the work started from. Never an approval. |
| `validation_not_configured` | 2 | Nothing blocking is open, but there was no gate and no explicit opt-in. |
| `baseline_failed` | 1 | Validation was failing before the task started; clean baseline is the default. |
| `max_iterations_reached` | 2 | Hit the cap with blocking findings open. |
| `no_progress` | 2 | A fix round changed nothing the reviewers cared about. |
| `implementer_failed` | 1 | The build agent could not complete a round. |
| `stopped_by_user` | 2 | Stopped on request, including by `stop --kill`. The working tree holds whatever the last completed step produced. Not a failure, and not a review. |
| `run_failed` | 1 | A safety-critical Git operation or unexpected orchestration step failed; terminal state and report were still written. |

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
<git-admin-dir>/review-loop/       # ~/.review-loop/state/non-git/<key>/ without Git
  .owner               validated state ownership marker
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
    logs/                bounded transcript per agent invocation
```

## Stop conditions

Stop and hand back to the user when:

- the repository is not a git repository, or the tree is dirty and they have not chosen how to proceed
- another run is already active in this worktree
- `detect` finds no usable CLI
- the project has no validation commands and the user has not accepted an unverified run
- the implementer fails twice in a row
- a reviewer returns unparseable output twice for the same round — the run ends as `review_incomplete`, which is not an approval
- the account hits a provider usage limit — report the reset time and stop; re-running before then buys nothing
- the diff is empty, so no reviewer saw any code — check `base_ref` before re-running
- the iteration cap is reached with blocking findings outstanding
- validation was already failing before the loop started — stop on `baseline_failed`; only configure `require_clean_baseline: false` for a task whose stated purpose includes fixing it
