# review-loop

[![CI](https://github.com/bransbury/review-loop/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/bransbury/review-loop/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bransbury/review-loop)](https://github.com/bransbury/review-loop/releases)
[![License](https://img.shields.io/github/license/bransbury/review-loop)](LICENSE)

A configurable adversarial review panel for AI-assisted engineering work.

One agent implements the task. A panel of reviewer personas — each with a
deliberately different job — reviews the result in parallel, read-only, without
seeing each other's findings or the implementer's reasoning. The findings are
merged, deduplicated and sent back. Then the whole thing is reviewed again from
scratch, until the tests pass and nothing blocking remains.

It is not three agents talking. It is a deterministic workflow where models are
replaceable workers and a small program decides who acts, when, on what
information, and when the job is allowed to finish.

```text
                    ┌──────────────────────┐
                    │     ORCHESTRATOR     │
                    └──────────┬───────────┘
                               │  "implement this"
                               ▼
                    ┌──────────────────────┐
                    │    BUILD AGENT       │  writes code + tests
                    └──────────┬───────────┘
                               │  git diff
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
      ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
      │  Principal   │ │ Adversarial  │ │   Security   │   read-only,
      │   Engineer   │ │      QA      │ │   Engineer   │   in parallel
      └───────┬──────┘ └───────┬──────┘ └───────┬──────┘
              └────────────────┼────────────────┘
                               │  structured findings
                               ▼
                    ┌──────────────────────┐
                    │  merge + deduplicate │
                    │  + severity gate     │
                    └──────────┬───────────┘
                               │
                    blockers? ─┴─ no ──▶ tests pass? ──▶ DONE
                        │                    │
                        yes                  no
                        │                    │
                        └──▶ fix ──▶ review again (from scratch)
```

## Install

Works with **Claude Code**, **GitHub Copilot CLI** and **Codex** — all three
read `SKILL.md` from a per-user skills directory.

**One line.** Installs into every CLI found on your machine:

```bash
curl -fsSL https://raw.githubusercontent.com/bransbury/review-loop/main/install.sh | bash
```

Or from a clone, if you'd rather read it first:

```bash
git clone https://github.com/bransbury/review-loop.git
cd review-loop && ./install.sh
```

Symlinks by default, so `git pull` updates every harness at once. Use
`--copy` for independent copies, `--uninstall` to remove. Re-running the
installer safely updates installations it recognizes. If a destination named
`review-loop` already exists but is not a review-loop installation, the
installer stops without deleting or replacing it.

## Update skill

Use the update method that matches how you installed review-loop.

| Installation method | Update command |
|---|---|
| One-line installer | Re-run the one-line installation command in [Install](#install) |
| Cloned with the default symlink | Run `git pull --ff-only` in the review-loop clone |
| Cloned with `--copy` | Pull the clone, then run `./install.sh --copy` again |
| Claude Code plugin | Run `claude plugin update review-loop@review-loop` |
| Copilot CLI plugin | Run `copilot plugin update review-loop` |

Default installer and clone-based installations use symlinks, so pulling the
source checkout updates every linked harness immediately. Copy-based installs
need `./install.sh --copy` after every pull. If the checkout has local changes,
commit or stash them before pulling so Git does not overwrite your work.

After updating, restart the CLI or start a new Codex task so the refreshed
skill instructions are loaded. Claude Code can instead run `/reload-plugins`.
Third-party Claude marketplaces do not enable automatic updates by default;
enable auto-update for the marketplace in `/plugin` if you want updates at
startup.

Confirm the installed orchestrator version with:

```bash
python3 <skill-dir>/scripts/review_loop.py --version
```

review-loop uses Semantic Versioning while its interfaces settle. Release
notes and upgrade guidance are published on the
[GitHub Releases page](https://github.com/bransbury/review-loop/releases), and
all notable changes are recorded in [CHANGELOG.md](CHANGELOG.md).

**Claude Code plugin:**

```
/plugin marketplace add bransbury/review-loop
/plugin install review-loop
```

**Copilot CLI plugin:**

```bash
copilot plugin install bransbury/review-loop
```

Then restart your CLI and run `/review-loop`.

Requires Python 3.9+, which ships with macOS and every mainstream Linux
distribution. There is nothing to `pip install`.

## Use

```
/review-loop  Implement configurable rate limiting for the public API.
              Use the existing Redis infrastructure. Include tests and docs.
```

You will be asked five things:

1. **Build agent** — which model and effort implements the task. Non-Claude
   hosts default to GPT-5.6 Sol at medium when available.
2. **Reviewers** — pre-selected for you. The tool reads your task wording and
   the repository's files and recommends a panel, with a reason for each:

   ```
   $ review_loop.py suggest --task "Add SSO login with OAuth token refresh"
     principal-engineer   <- always recommended
     adversarial-qa       <- always recommended
     security             <- task mentions login, oauth, sso
   ```
3. **Model and effort per reviewer** — each one independently. Claude hosts
   default reviewers to the latest Opus model at low effort; non-Claude hosts
   default reviewers to GPT-5.6 Luna at xhigh when available. The same persona
   may appear twice on different models; each slot keeps its own identity and
   they can corroborate each other.
4. **Mixing** — whether reviewers may use a different CLI to the one you invoked
   from. Off by default.
5. **Build agent permissions** — it needs to run your tests unattended.

Then it runs detached and reports progress:

```text
● Implementer — Claude Opus @ high
  ✓ implementation complete
  ✓ 47 tests passed

● Review round 1
  ├─ Principal Engineer — Opus @ low
  │  1 high, 2 medium
  └─ Adversarial QA — GPT-5.6-Luna @ xhigh
     1 blocker, 1 medium

● Sending 5 findings to implementer
  ✓ 4 fixed · 1 rejected with evidence
  ✓ 51 tests passed

● Review round 2
  ✓ Principal Engineer approved
  ✓ Adversarial QA approved

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓ COMPLETE · 2 rounds · 5 found · 5 resolved · 51 passing
```

## Reviewer personas

| Persona | Lens |
|---|---|
| `principal-engineer` | Architecture, correctness, maintainability, unnecessary complexity |
| `adversarial-qa` | Edge cases, malformed input, partial failure, resource leaks, races |
| `security` | Authorization, injection, secrets, untrusted input, data exposure |
| `ux-accessibility` | Missing states, keyboard and focus, semantics, contrast |
| `performance` | N+1s, unbounded work, accidental quadratics, bundle weight |
| `test-engineer` | Tests that pass while the behaviour is wrong; untested branches |
| `api-contract` | Silent breaking changes, contract drift, versioning, migration paths |
| `data-integrity` | Transactions, migrations, destructive operations, constraint gaps |
| `observability` | Silent failures, lost causes, undiagnosable incidents |
| `typescript-types` | `any` leaks, unsound assertions, states that should be unrepresentable |

Personas are plain markdown in `skills/review-loop/personas/`. Add your own by
dropping a file in — it appears in the picker automatically. The frontmatter
controls routing:

```markdown
---
name: Security Engineer
description: Authorization, injection, secrets, untrusted input.
default: false                        # true = always recommended
keywords: auth*,login,oauth,token*    # matched against the task wording
signals: auth,session,middleware      # matched against tracked file paths
---
```

Keywords match whole words, so `log` will not fire on `login`. A trailing `*`
makes it a stem, so `optimi*` covers both spellings. Signals starting with `.`
match file extensions; the rest must match a whole path segment, so `security`
fires on `src/security/guard.ts` but not on `notes-about-security.md`.

## Why it is built this way

**Reviewers get deliberately different jobs.** Ask two strong models to "review
this PR" and you get two overlapping lists. The value is in the differentiation.

**Every round reviews from scratch.** Asking "did you fix ARCH-001?" anchors the
reviewer on the previous round and hides regressions introduced by the fixes.

**Tests are an independent gate.** Agreement between language models is not a
definition of correctness. Validation commands run every round, and failing
tests keep the loop alive even when every reviewer approves.

**Reviewers are read-only, enforced by flags and trusted configuration.** Claude
uses plan mode with project/local setting sources disabled, Copilot explicitly
allows its read capability while denying write, shell, and MCP tools, and Codex
uses `-s read-only`. Permission-affecting Copilot environment approvals are
removed before launch. This is not merely a prompt instruction.

**A missing answer is not a clean one.** A review counts only if it parses
*and* holds a real review: an explicit verdict, a `findings` list, objects
inside it, and at least one finding when it asks for changes. A reviewer that
fails, that returns something malformed twice, or that objects without naming
anything makes the round incomplete, and the run ends `review_incomplete` —
never `approved`. Every permissive reading of a malformed review normalises to
zero findings, which is indistinguishable from a clean one. Silence from a
reviewer that never answered is not evidence of correctness, and it does not
resolve findings in the ledger either.

**A gate that never ran did not pass.** Validation is `passed`, `failed` or
`not_configured` — never a boolean that reads "true" when nothing executed. A
config with no validation commands is rejected at launch; approving without one
requires `allow_missing_validation` and reports as `approved_unverified`. A
baseline run before the build agent starts must pass by default, so pre-existing
failures cannot silently expand the task. A task whose stated purpose includes
repairing that baseline can explicitly set `require_clean_baseline` to `false`;
the original failure is then carried into prompts and the final report.

**One run per worktree, and never on a tree it cannot reason about.** The
orchestrator refuses to start on a non-git or dirty working tree unless
explicitly allowed. It resolves any configured subdirectory through Git to the
canonical worktree root, then uses that root for agent cwd, validation, diffs,
state and an atomic lock. Two subdirectory configs therefore cannot edit the
same worktree concurrently or race on `current-run`. State and the lock live
outside the worktree in Git's per-worktree administrative directory, where a
tracked path, planted symlink, reviewer, or `git clean` cannot remove them.

**Timeouts stop process trees.** Agent and validation commands run in their own
POSIX process groups while a kernel-identity tracker records descendants that
escape into a new session or clear their environment. On supported macOS and
Linux hosts, a timeout terminates the complete recorded tree and preserves
bounded partial stdout/stderr in logs and validation artifacts. Excessive output
terminates the command rather than being buffered without limit.

**Approval is tied to one filesystem snapshot.** After validation, the loop
fingerprints HEAD, the index, tracked content, and untracked paths/content. It
checks that identity again after reviewers finish. A reviewer hook, background
process, or user edit during review invalidates the gates and fails the run
closed instead of approving code that was never validated or reviewed.

**The loop is capped.** Five rounds by default. Past that it stops and asks for
a human, rather than negotiating with itself indefinitely.

**Severity has a threshold.** `blocker` and `high` must be fixed, `medium`
normally is, `low` is recorded, `nit` is ignored. Nobody should re-run a build
agent because a reviewer wanted a variable renamed.

**Findings raised independently by two reviewers are marked
`corroborated_by`** and treated as high-confidence.

**Every finding is tracked across rounds, not just the last one.** A ledger
records each defect once — matching it across rounds even when a reviewer
rewords it — with a state of `open`, `resolved` or `reappeared`. That is what
lets the final report say what was raised, what was fixed, and which fix did
not hold, instead of showing whatever the final panel happened to repeat.

## Token efficiency

A panel multiplies cost: the diff goes to every reviewer, every round. Three
reviewers over five rounds is fifteen copies. The design takes that seriously.

**Noise never reaches a reviewer.** Lockfiles, `dist/`, minified bundles,
snapshots, binary assets and generated code are excluded by default. On a
change that also touched `package-lock.json`, this took the review payload from
~10,000 tokens to ~570 — a 94% cut, with the actual code change untouched.
Reviewers are told what was withheld so they never review a partial picture
believing it complete.

**Stable content comes first.** The persona, instructions, schema and task form
a cacheable prefix; only the diff and validation results vary. In a measured
run, 545,248 of 545,361 input tokens were cache reads.

**The panel is capped and the loop exits early.** `suggest` recommends at most
four reviewers. The loop stops when a fix round changes nothing, and when the
same blocking findings survive a round — spending another full panel to receive
the same answer is the most expensive way to learn nothing.

**Reviewers get the verdict, not the stack trace.** Failing test output goes to
the build agent, which has to fix it, and not to every reviewer, which cannot.

**Spend is reported, not guessed.** Where the CLI returns real usage, `render`
shows it:

```
spend: 545,361 in (545,248 cached) · 14,637 out · $0.52
```

Worth knowing: most of that is per-invocation harness overhead, not your diff.
The strongest lever is therefore fewer invocations — a smaller panel and fewer
rounds — not a smaller prompt.

Tunable in the run config:

| Key | Default | Effect |
|---|---|---|
| `exclude_noise` | `true` | Drop lockfiles, build output, binaries, generated code |
| `exclude_paths` | `[]` | Extra glob pathspecs to exclude |
| `max_file_chars` | `20000` | Per-file cap before truncation |
| `max_diff_chars` | `120000` | Whole-payload ceiling |

Every numeric limit and timeout is validated as an in-range integer before a
run detaches. Copilot-backed panels cap `max_diff_chars` at 400,000 because its
non-interactive prompt travels in argv on macOS. Runtime booleans, exclusions,
severities, permission modes and
agent slot fields are likewise shape-checked before launch.

## Configuration

Save defaults to `~/.review-loop/defaults.json` so you are not re-answering the
wizard every time:

```json
{
  "global_default": { "cli": "codex", "model": "gpt-5.6-luna", "effort": "xhigh" },
  "implementer": { "cli": "codex", "model": "gpt-5.6-sol", "effort": "medium" },
  "reviewers": ["principal-engineer", "adversarial-qa"],
  "max_iterations": 5,
  "blocking_severities": ["blocker", "high", "medium"]
}
```

Per-run configuration lives in
`<git-admin-dir>/review-loop/history/run-NN/config.json`. Explicit non-Git runs
use a path-keyed directory below `~/.review-loop/state/non-git/` (or
`REVIEW_LOOP_STATE_ROOT` when configured).

### Build-agent permissions

Only `acceptEdits` and `bypassPermissions` are supported. The adapters map
those portable names to their actual CLI contracts:

| CLI | `acceptEdits` | `bypassPermissions` |
|---|---|---|
| Claude Code | native `--permission-mode acceptEdits` | native `--permission-mode bypassPermissions` |
| Copilot CLI | file writes approved; shell denied; no blanket MCP approval | `--allow-all` (tools, paths and URLs) |
| Codex | `workspace-write` sandbox | `--dangerously-bypass-approvals-and-sandbox` |

The modes are not identical across products; the table is the guarantee.
Reviewer isolation always overrides this setting.

### Multiple accounts

Any agent slot accepts `config_dir`, which sets `CLAUDE_CONFIG_DIR` or
`CODEX_HOME` for that agent only. If you have a second authenticated account,
pointing reviewers at it spreads a long loop across two rate-limit pools:

```json
{ "persona": "security", "cli": "claude", "model": "opus",
  "effort": "max", "config_dir": "~/.claude-guest" }
```

Off by default.

## Working files

Everything lands outside the target worktree. For a normal Git repository the
layout is below Git's per-worktree administrative directory:

```text
<git-admin-dir>/review-loop/
  .owner                      validated state ownership marker
  task.md
  final.md                    ← read this
  lock                        one active run per worktree
  history/run-NN/
    config.json               resolved configuration
    progress.jsonl            event stream
    findings-NN.json          merged and deduplicated, this round only
    ledger.json               every finding across rounds, with its state
    review-NN-<persona>.json  each reviewer's raw verdict
    validation-00.json        baseline, before the build agent ran
    validation-NN.json        test/lint/typecheck results
    logs/                     bounded transcript per invocation
```

## Direct CLI use

The orchestrator runs standalone, without an agent host — useful in CI:

```bash
review_loop.py detect                      # installed CLIs, their models and efforts
review_loop.py suggest --task "..."        # recommend a reviewer panel
review_loop.py start   --config run.json   # launch detached
review_loop.py render                      # the progress tree, for humans
review_loop.py status  --tail 20           # raw events, for scripts
review_loop.py stop [--kill]
```

On a successful launch, `start` returns immediately with the run directory and
the run continues in the background. Invalid or unsafe launches exit nonzero
before detaching. For CI, use `run`, which executes in the foreground and
exits with the result:

```bash
python3 skills/review-loop/scripts/review_loop.py run --config run.json
```

| Exit | Meaning |
|---|---|
| `0` | Approved — full panel reported, no blocking findings, validation passing (or `approved_unverified` when `allow_missing_validation` is set) |
| `1` | Could not run — invalid config, unsafe repository, another run already active, build agent failed, Git safety/diff failure, or unexpected orchestration failure |
| `2` | Finished with blocking findings outstanding, hit the iteration cap, or could not approve because the panel was incomplete or ungated |

For CI, treat only exit `0` as a pass, and check `outcome` in `final.md` if you
want to distinguish `approved` from `approved_unverified`.

Unexpected exceptions fail closed with `run_failed` and `run_complete` events,
a final report, exit `1`, and lock release.

`status` and `stop` operate on the newest run in the current worktree; pass
`--repo <path>` or `--run <dir>` to target another.

## Known limits

- **Codex and current Claude Code versions can constrain reviewer output to a
  JSON Schema.** Codex receives `--output-schema`; Claude receives inline
  `--json-schema` only when its installed CLI advertises the flag. Older Claude
  versions and Copilot safely fall back to prompt shaping. Every adapter's
  output is still validated in code, with one repair retry; malformed output is
  never guessed at or read as approval.
- **Copilot model availability is governed by your GitHub organisation's
  policy.** The picker enumerates at runtime and falls back to `auto`.
- **The build agent needs non-interactive write permission.** The default is
  `acceptEdits`, with the adapter-specific behavior documented above. Use
  `bypassPermissions` only when the task requires its broader capabilities,
  preferably inside a disposable worktree or container.
- **Reviewers read the diff plus the repository, not a running system.** They
  will not catch what only an integration environment reveals.

## Licence

MIT
