---
name: review-loop
version: 0.1.0
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
- **Reviewers are read-only, enforced by CLI flags** (`--permission-mode plan`, `--deny-tool write`, `-s read-only`), not by asking politely in a prompt.
- **Reviewers never see the implementer's reasoning or self-assessment.** They get the task, the repository, and the diff. Nothing else.
- **Every review round starts from scratch.** Never ask "did the implementer fix ARCH-001?". Anchoring on prior findings hides regressions introduced by the fixes.
- **Tests are an independent gate.** Agreement between models is not a definition of correctness. If validation fails, the loop keeps going even when every reviewer approves.
- **One harness by default.** Every slot uses the CLI you were invoked from unless the user explicitly opts into mixing.
- **Never run this on a dirty working tree** without telling the user. Uncommitted work will be mixed into the diff under review and may be modified by the implementer.

## Token discipline

Be concise. Do not paste the diff, the prompts, or the raw JSON into the conversation. Report progress as the compact tree shown below. Summarise findings; the full detail is on disk.

## 1. Preflight

Run the detector:

```bash
python3 <skill-dir>/scripts/review_loop.py detect
```

It returns installed CLIs, their available models and effort ladders, the persona library, and a guess at which harness invoked you. Model availability varies by plan and by org policy — never hardcode a model list, always use what `detect` reports.

Then check the repository state:

- Confirm you are in a git repository. If not, stop and say so.
- Check `git status`. If the tree is dirty, tell the user what is uncommitted and ask whether to continue, commit first, or stop.
- Identify validation commands from the project itself — `package.json` scripts, `Makefile`, `pyproject.toml`, CI config. Propose what you find; do not invent commands that do not exist.

If no task was supplied with the invocation, ask for one before anything else.

## 2. Configure

Ask these questions using your host's interactive question mechanism. In Claude Code that is `AskUserQuestion`; in Copilot CLI it is the `ask_user` tool; elsewhere ask in plain text with numbered options. Group them so the user answers in as few steps as possible.

Load `~/.review-loop/defaults.json` if it exists and use it to pre-fill every answer. Offer to save the answers back there at the end of a successful configuration.

**Question 1 — Build agent.** Which model and effort implements the task. Default to the strongest model the invoking CLI offers, at `high`.

**Question 2 — Reviewers.** Multi-select from the persona library returned by `detect`. Recommend a panel based on what the task actually touches rather than offering all nine every time:

| The task involves | Suggest |
|---|---|
| Anything at all | Principal Engineer, Adversarial QA |
| Auth, user input, external requests | Security Engineer |
| A user-visible interface | UI/UX & Accessibility |
| A public or shared interface | API & Contract |
| Schema changes, migrations, bulk writes | Data & Migrations |
| Hot paths, large collections, bundle size | Performance Engineer |
| A TypeScript codebase | TypeScript & Type Design |
| A new dependency or failure mode | Observability & Operations |
| Thin or suspect test coverage | Test Engineer |

Two or three reviewers is the useful range. More than four mostly produces duplicate findings and a slower loop; say so if the user picks a large panel.

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
  "implementer": { "cli": "claude", "model": "opus", "effort": "high" },
  "reviewers": [
    { "persona": "principal-engineer", "label": "Principal Engineer",
      "cli": "claude", "model": "opus", "effort": "max" },
    { "persona": "adversarial-qa", "label": "Adversarial QA",
      "cli": "codex", "model": "gpt-5.6-luna", "effort": "max" }
  ]
}
```

`start` returns a run directory and exits immediately. It also adds `.review-loop/` to the repository's `.gitignore`.

Each reviewer slot accepts an optional `config_dir`, which sets `CLAUDE_CONFIG_DIR` or `CODEX_HOME` for that agent. Use it only if the user has a second authenticated account and asks for it — it spreads rate limits across subscriptions. It is off by default.

## 4. Report progress

Poll `progress.jsonl` in the run directory and render the tree. Do not poll faster than every 30 seconds, and do not echo raw events.

```text
● Implementer — Claude Opus @ high
  ✓ implementation complete
  ✓ 47 tests passed

● Review round 1
  ├─ Principal Engineer — Opus @ max
  │  1 high, 2 medium
  └─ Adversarial QA — GPT-5.6-Luna @ max
     1 blocker, 1 medium

● Sending 5 findings to implementer
  ▸ running (04:12)
```

Between polls, tell the user they can keep working and that `status` and `stop` are available.

Two events need surfacing immediately rather than at the end:

- **`permission_denied`** — the build agent was blocked from running commands, so its "success" is unverified. Tell the user at once and offer to stop and restart with full permissions.
- **`review_unparsed`** — a reviewer failed to return usable JSON twice. Its findings are missing from the round, so the panel is smaller than the user thinks. Say so rather than reporting a clean review.

## 5. Finish

When `run_complete` appears, read `final.md` from the run directory and report:

- the outcome — approved, max iterations reached, stopped, or failed
- rounds run, findings raised, findings resolved
- validation status
- any finding the implementer rejected, with its stated evidence
- anything still outstanding

Then show the user the diff summary and hand off. Do not commit or push unless asked.

**If the outcome is `max_iterations_reached`, say so plainly and stop.** That means the panel and the implementer did not converge, and it needs a human. Do not raise the cap and try again without being asked.

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
  task.md
  final.md
  current-run
  history/run-NN/
    config.json          resolved configuration
    progress.jsonl       event stream — poll this
    findings-NN.json     merged, deduplicated
    review-NN-<persona>.json
    validation-NN.json
    implementer-NN.md
    final.md
    logs/                full transcript per agent invocation
```

## Stop conditions

Stop and hand back to the user when:

- the repository is not a git repository, or the tree is dirty and they have not chosen how to proceed
- `detect` finds no usable CLI
- the implementer fails twice in a row
- a reviewer returns unparseable output twice for the same round
- the iteration cap is reached with blocking findings outstanding
- validation was already failing before the loop started — fix that first, or the gate is meaningless
