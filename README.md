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
`--copy` for independent copies, `--uninstall` to remove.

## Update

How to update depends on how review-loop was installed:

| Installation | Update command |
|---|---|
| One-line installer | Re-run the `curl` installation command above. |
| Cloned, default symlink | Run `git pull --ff-only` in the clone. |
| Cloned with `--copy` | Pull the clone, then run `./install.sh --copy` again. |
| Claude Code plugin | `claude plugin update review-loop@review-loop` |
| Copilot CLI plugin | `copilot plugin update review-loop` |

Restart the CLI after updating. Claude Code can instead run
`/reload-plugins`. Third-party Claude marketplaces do not enable automatic
updates by default; enable auto-update for the marketplace in `/plugin` if you
want updates at startup.

To see the installed orchestrator version:

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

1. **Build agent** — which model and effort implements the task.
2. **Reviewers** — pre-selected for you. The tool reads your task wording and
   the repository's files and recommends a panel, with a reason for each:

   ```
   $ review_loop.py suggest --task "Add SSO login with OAuth token refresh"
     principal-engineer   <- always recommended
     adversarial-qa       <- always recommended
     security             <- task mentions login, oauth, sso
   ```
3. **Model and effort per reviewer** — each one independently. The same persona
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
  ├─ Principal Engineer — Opus @ max
  │  1 high, 2 medium
  └─ Adversarial QA — GPT-5.6-Luna @ max
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

**Reviewers are read-only, enforced by flags.** `--permission-mode plan`,
`--deny-tool write --deny-tool shell`, `-s read-only` — not a polite request in
a prompt. Copilot's `write` permission covers file-writing tools but explicitly
not shell invocations, so its shell is denied outright; denying `git commit` and
`git push` by name would still have left `sed -i`, `rm` and redirection.

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
baseline run before the build agent starts records whether the suite was
already failing, so a later failure is never misattributed to the change.

**One run per worktree, and never on a tree it cannot reason about.** The
orchestrator refuses to start on a non-git or dirty working tree unless
explicitly allowed, and holds an atomic lock so two runs cannot edit the same
files and race on `current-run`. It keeps its own state out of git with a
`.gitignore` inside `.review-loop/`, so it never modifies a tracked file.

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

## Configuration

Save defaults to `~/.review-loop/defaults.json` so you are not re-answering the
wizard every time:

```json
{
  "global_default": { "cli": "codex", "model": "gpt-5.6-luna", "effort": "max" },
  "implementer": { "cli": "claude", "model": "opus", "effort": "high" },
  "reviewers": ["principal-engineer", "adversarial-qa"],
  "max_iterations": 5,
  "blocking_severities": ["blocker", "high", "medium"]
}
```

Per-run configuration lives in `.review-loop/history/run-NN/config.json`.

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

Everything lands in `.review-loop/` in the target repository, which the tool
adds to `.gitignore` on first run.

```text
.review-loop/
  .gitignore                  keeps this directory out of git
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
    logs/                     full transcript per invocation
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

`start` returns immediately and always exits `0`; the run continues in the
background. For CI, use `run` instead, which executes in the foreground and
exits with the result:

```bash
python3 skills/review-loop/scripts/review_loop.py run --config run.json
```

| Exit | Meaning |
|---|---|
| `0` | Approved — full panel reported, no blocking findings, validation passing (or `approved_unverified` when `allow_missing_validation` is set) |
| `1` | Could not run — invalid config, unsafe repository, another run already active, build agent failed, no usable CLI |
| `2` | Finished with blocking findings outstanding, hit the iteration cap, or could not approve because the panel was incomplete or ungated |

For CI, treat only exit `0` as a pass, and check `outcome` in `final.md` if you
want to distinguish `approved` from `approved_unverified`.

`status` and `stop` operate on the newest run in the current directory; pass
`--repo <path>` or `--run <dir>` to target another.

## Known limits

- **Only Codex can hard-constrain reviewer output to a JSON Schema**
  (`--output-schema`). Every adapter's output is therefore validated in code
  for shape, not just parsed — an explicit verdict, a `findings` list, and
  objects inside it — with one repair retry. Anything that fails is recorded as
  a reviewer that did not report, never guessed at and never read as approval.
- **Copilot model availability is governed by your GitHub organisation's
  policy.** The picker enumerates at runtime and falls back to `auto`.
- **The build agent needs non-interactive write permission.** The default is
  `acceptEdits`; if runs stall waiting for approval, set `permission_mode` to
  `bypassPermissions` in the config — and prefer running in a worktree or
  container when you do.
- **Reviewers read the diff plus the repository, not a running system.** They
  will not catch what only an integration environment reveals.

## Licence

MIT
