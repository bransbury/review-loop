# review-loop

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

**Universal (recommended).** Installs into every CLI found on your machine:

```bash
git clone https://github.com/bransbury/review-loop.git
cd review-loop
./install.sh
```

Symlinks by default, so `git pull` updates every harness at once. Use
`--copy` if you would rather have independent copies, `--uninstall` to remove.

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

You will be asked four things:

1. **Build agent** — which model and effort implements the task.
2. **Reviewers** — pick from the persona library. Two or three is the useful range.
3. **Model and effort per reviewer** — each one independently.
4. **Mixing** — whether reviewers may use a different CLI to the one you invoked from. Off by default.

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
dropping a file in — it appears in the picker automatically.

## Why it is built this way

**Reviewers get deliberately different jobs.** Ask two strong models to "review
this PR" and you get two overlapping lists. The value is in the differentiation.

**Every round reviews from scratch.** Asking "did you fix ARCH-001?" anchors the
reviewer on the previous round and hides regressions introduced by the fixes.

**Tests are an independent gate.** Agreement between language models is not a
definition of correctness. Validation commands run every round, and failing
tests keep the loop alive even when every reviewer approves.

**Reviewers are read-only, enforced by flags.** `--permission-mode plan`,
`--deny-tool write`, `-s read-only` — not a polite request in a prompt.

**The loop is capped.** Five rounds by default. Past that it stops and asks for
a human, rather than negotiating with itself indefinitely.

**Severity has a threshold.** `blocker` and `high` must be fixed, `medium`
normally is, `low` is recorded, `nit` is ignored. Nobody should re-run a build
agent because a reviewer wanted a variable renamed.

**Findings raised independently by two reviewers are marked
`corroborated_by`** and treated as high-confidence.

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
  task.md
  final.md                    ← read this
  history/run-NN/
    config.json               resolved configuration
    progress.jsonl            event stream
    findings-NN.json          merged, deduplicated
    review-NN-<persona>.json  each reviewer's raw verdict
    validation-NN.json        test/lint/typecheck results
    logs/                     full transcript per invocation
```

## Direct CLI use

The orchestrator runs standalone, without an agent host — useful in CI:

```bash
python3 skills/review-loop/scripts/review_loop.py detect
python3 skills/review-loop/scripts/review_loop.py start  --config run.json   # detached
python3 skills/review-loop/scripts/review_loop.py status --tail 20
python3 skills/review-loop/scripts/review_loop.py stop
```

`start` returns immediately and always exits `0`; the run continues in the
background. For CI, use `run` instead, which executes in the foreground and
exits with the result:

```bash
python3 skills/review-loop/scripts/review_loop.py run --config run.json
```

| Exit | Meaning |
|---|---|
| `0` | Approved — no blocking findings, validation passing |
| `1` | Could not run (build agent failed, no usable CLI) |
| `2` | Finished with blocking findings outstanding, or hit the iteration cap |

`status` and `stop` operate on the newest run in the current directory; pass
`--repo <path>` or `--run <dir>` to target another.

## Known limits

- **Only Codex can hard-constrain reviewer output to a JSON Schema**
  (`--output-schema`). Claude and Copilot are asked for JSON and parsed
  defensively, with one repair retry. Unparseable output is recorded, not
  guessed at.
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
