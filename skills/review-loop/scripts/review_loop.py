#!/usr/bin/env python3
"""review-loop: deterministic implement -> review -> fix orchestrator.

Stdlib only. No pip install. Works with any combination of the
`claude`, `copilot` and `codex` CLIs that are present on PATH.

Subcommands
-----------
  detect                      Probe installed CLIs; emit JSON for the wizard.
  start   --config FILE       Launch a run detached; print the run directory.
  run     --config FILE       Execute the loop in the foreground (used by start).
  status  [--run DIR]         Human/JSON summary of the newest or given run.
  stop    [--run DIR]         Ask a running loop to halt after the current step.

The host agent (Claude Code / Copilot CLI / Codex) writes the config from its
interactive wizard, calls `start`, then tails progress.jsonl to render the UI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
PERSONA_DIR = SKILL_DIR / "personas"
SCHEMA_PATH = SKILL_DIR / "schemas" / "review.json"

SEVERITIES = ["blocker", "high", "medium", "low", "nit"]
STATE_DIRNAME = ".review-loop"


# ---------------------------------------------------------------------------
# CLI adapters
# ---------------------------------------------------------------------------
# Each adapter knows five things: how to invoke non-interactively, how to set
# the model, how to set reasoning effort, how to enforce read-only, and how to
# get the final assistant message back out. That is the entire surface needed
# to add a new harness.

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
COPILOT_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]

# Fallbacks only. `detect` prefers live enumeration where the CLI allows it,
# because model availability varies by plan and by org policy.
CLAUDE_MODELS = ["opus", "fable", "sonnet", "haiku"]
CODEX_MODELS_FALLBACK = ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5", "gpt-5.4"]
COPILOT_MODELS_FALLBACK = ["auto"]


def _claude_argv(prompt: str, model: str, effort: str, readonly: bool,
                 permission_mode: str, out_file: Path) -> tuple[list[str], str | None]:
    # Prompt goes on stdin: a large diff would otherwise risk ARG_MAX.
    argv = ["claude", "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if readonly:
        # Belt and braces: plan mode cannot write, and the tools are denied too.
        argv += ["--permission-mode", "plan",
                 "--disallowed-tools", "Edit", "Write", "NotebookEdit"]
    else:
        argv += ["--permission-mode", permission_mode]
    return argv, prompt


def _copilot_argv(prompt: str, model: str, effort: str, readonly: bool,
                  permission_mode: str, out_file: Path) -> tuple[list[str], str | None]:
    # Copilot has no stdin prompt mode, so the text goes in argv. ARG_MAX is
    # 1MB on macOS and 2MB on Linux; the diff cap keeps us well clear.
    argv = ["copilot", "--prompt", prompt,
            "--allow-all-tools", "--no-ask-user", "--silent", "--no-color"]
    if model and model != "auto":
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if readonly:
        argv += ["--deny-tool", "write", "--deny-tool", "shell(git commit)",
                 "--deny-tool", "shell(git push)"]
    return argv, None


def _codex_argv(prompt: str, model: str, effort: str, readonly: bool,
                permission_mode: str, out_file: Path) -> tuple[list[str], str | None]:
    # `-` makes codex exec read the prompt from stdin.
    argv = ["codex", "exec", "--skip-git-repo-check", "-o", str(out_file)]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    if readonly:
        argv += ["-s", "read-only"]
        # Codex is the only harness that can hard-constrain the response shape.
        if SCHEMA_PATH.exists():
            argv += ["--output-schema", str(SCHEMA_PATH)]
    else:
        argv += ["-s", "workspace-write"]
    argv += ["-"]
    return argv, prompt


ADAPTERS: dict[str, dict[str, Any]] = {
    "claude": {
        "bin": "claude",
        "argv": _claude_argv,
        "efforts": CLAUDE_EFFORTS,
        "default_effort": "high",
        "default_model": "opus",
        "reads_out_file": False,
        "config_dir_env": "CLAUDE_CONFIG_DIR",
    },
    "copilot": {
        "bin": "copilot",
        "argv": _copilot_argv,
        "efforts": COPILOT_EFFORTS,
        "default_effort": "high",
        "default_model": "auto",
        "reads_out_file": False,
        "config_dir_env": None,
    },
    "codex": {
        "bin": "codex",
        "argv": _codex_argv,
        "efforts": CODEX_EFFORTS,
        "default_effort": "high",
        "default_model": "gpt-5.6-sol",
        "reads_out_file": True,
        "config_dir_env": "CODEX_HOME",
    },
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _codex_models() -> list[str]:
    cache = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
    try:
        data = json.loads(cache.read_text())
        slugs = [m["slug"] for m in data.get("models", []) if m.get("slug")]
        # codex-auto-review is a routing target, not a general-purpose model.
        return [s for s in slugs if not s.startswith("codex-auto")] or CODEX_MODELS_FALLBACK
    except Exception:
        return CODEX_MODELS_FALLBACK


def _copilot_models() -> list[str]:
    # Copilot exposes no `list models` command, and availability is governed by
    # org policy. Ask the CLI and fall back to `auto` if it refuses.
    cfg = Path.home() / ".copilot" / "settings.json"
    try:
        data = json.loads(re.sub(r"^\s*//.*$", "", cfg.read_text(), flags=re.M))
        known = data.get("knownModels") or data.get("models")
        if isinstance(known, list) and known:
            return ["auto"] + [str(m) for m in known]
    except Exception:
        pass
    return COPILOT_MODELS_FALLBACK


def cmd_detect(_args: argparse.Namespace) -> int:
    out: dict[str, Any] = {"agents": {}, "personas": [], "invoked_from": _invoking_harness()}
    for name, ad in ADAPTERS.items():
        path = shutil.which(ad["bin"])
        if not path:
            continue
        entry = {
            "path": path,
            "efforts": ad["efforts"],
            "default_model": ad["default_model"],
            "default_effort": ad["default_effort"],
        }
        if name == "claude":
            entry["models"] = CLAUDE_MODELS
        elif name == "codex":
            entry["models"] = _codex_models()
        else:
            entry["models"] = _copilot_models()
            entry["note"] = "Model availability is governed by your GitHub org's Copilot policy."
        out["agents"][name] = entry

    for p in sorted(PERSONA_DIR.glob("*.md")):
        meta = _persona_meta(p)
        out["personas"].append({"id": p.stem, **meta})
    print(json.dumps(out, indent=2))
    return 0


def _invoking_harness() -> str | None:
    """Best-effort guess at which harness is running us, for wizard defaults."""
    if os.environ.get("CLAUDE_CODE") or os.environ.get("CLAUDECODE"):
        return "claude"
    if os.environ.get("COPILOT_ALLOW_ALL") or os.environ.get("COPILOT_AGENT"):
        return "copilot"
    if os.environ.get("CODEX_HOME") and not os.environ.get("CLAUDE_CONFIG_DIR"):
        return "codex"
    return None


def _persona_meta(path: Path) -> dict[str, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    meta: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
    meta.setdefault("name", path.stem)
    meta.setdefault("description", "")
    return meta


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, repo: Path, run_dir: Path, config: dict[str, Any]):
        self.repo = repo
        self.dir = run_dir
        self.config = config
        self.progress = run_dir / "progress.jsonl"
        self.stop_file = run_dir / "STOP"

    def emit(self, event: str, **fields: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self.progress.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        # Also echo for foreground/log tailing.
        print(json.dumps(rec), flush=True)

    def should_stop(self) -> bool:
        return self.stop_file.exists()


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

def invoke_agent(run: Run, slot: dict[str, Any], prompt: str, readonly: bool,
                 label: str, log_name: str, attempts: int = 1) -> tuple[bool, str]:
    """Run one agent to completion. Retries only on transient process failure."""
    last = "no attempt made"
    for attempt in range(1, max(1, attempts) + 1):
        suffix = "" if attempt == 1 else f"-retry{attempt - 1}"
        ok, text = _invoke_once(run, slot, prompt, readonly,
                                label if attempt == 1 else f"{label} (retry {attempt - 1})",
                                log_name + suffix)
        if ok:
            return True, text
        last = text
        if attempt < attempts:
            run.emit("agent_retry", label=label, attempt=attempt, error=text[:300])
            time.sleep(5)
    return False, last


def _invoke_once(run: Run, slot: dict[str, Any], prompt: str, readonly: bool,
                 label: str, log_name: str) -> tuple[bool, str]:
    cli = slot["cli"]
    ad = ADAPTERS.get(cli)
    if ad is None:
        return False, f"unknown cli '{cli}'"
    if not shutil.which(ad["bin"]):
        return False, f"{ad['bin']} is not on PATH"

    work = run.dir / "work"
    work.mkdir(exist_ok=True)
    prompt_file = work / f"{log_name}.prompt.md"
    prompt_file.write_text(prompt)
    out_file = work / f"{log_name}.out.txt"
    log_file = run.dir / "logs" / f"{log_name}.log"
    log_file.parent.mkdir(exist_ok=True)

    argv, stdin_text = ad["argv"](
        prompt,
        slot.get("model") or ad["default_model"],
        slot.get("effort") or ad["default_effort"],
        readonly,
        run.config.get("permission_mode", "acceptEdits"),
        out_file,
    )

    env = os.environ.copy()
    env_key = ad["config_dir_env"]
    override = slot.get("config_dir")
    if env_key and override:
        env[env_key] = os.path.expanduser(override)

    started = time.time()
    run.emit("agent_start", label=label, cli=cli,
             model=slot.get("model"), effort=slot.get("effort"), readonly=readonly)

    timeout = int(run.config.get("agent_timeout_seconds", 3600))
    try:
        proc = subprocess.run(
            argv, cwd=str(run.repo), env=env, timeout=timeout,
            input=stdin_text, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        run.emit("agent_error", label=label, error=f"timed out after {timeout}s")
        return False, f"{label} timed out after {timeout}s"

    log_file.write_text(
        f"$ {' '.join(argv[:6])} ...\n\n--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}"
    )

    if ad["reads_out_file"] and out_file.exists():
        text = out_file.read_text()
    else:
        text = _extract_final_text(cli, proc.stdout)

    # A write-capable agent that was blocked from running tests will look like
    # it succeeded while having verified nothing. Surface that loudly.
    if not readonly:
        denials = _permission_denials(cli, proc.stdout)
        if denials:
            run.emit("permission_denied", label=label, count=len(denials),
                     tools=sorted({str(d)[:60] for d in denials})[:5],
                     hint="Set \"permission_mode\": \"bypassPermissions\" in the config "
                          "(ideally inside a git worktree) so the build agent can run tests.")

    elapsed = round(time.time() - started, 1)
    if proc.returncode != 0 and not text.strip():
        run.emit("agent_error", label=label, rc=proc.returncode,
                 error=(proc.stderr or "").strip()[:600], seconds=elapsed)
        return False, (proc.stderr or f"exit {proc.returncode}")[:600]
    if not text.strip():
        run.emit("agent_error", label=label, rc=proc.returncode,
                 error="agent produced no output", seconds=elapsed)
        return False, "agent produced no output"

    run.emit("agent_done", label=label, seconds=elapsed, chars=len(text))
    return True, text


def _permission_denials(cli: str, stdout: str) -> list[Any]:
    if cli != "claude":
        return []
    try:
        env = json.loads(stdout)
        denials = env.get("permission_denials")
        return denials if isinstance(denials, list) else []
    except Exception:
        return []


def _extract_final_text(cli: str, stdout: str) -> str:
    """Normalise each CLI's stdout down to the final assistant message."""
    if cli == "claude":
        try:
            env = json.loads(stdout)
            if isinstance(env, dict):
                return str(env.get("result") or env.get("content") or stdout)
        except Exception:
            pass
    if cli == "copilot":
        # We invoke with --silent, so stdout is normally the plain final message.
        # Only treat it as a JSONL event stream when *every* line parses as a
        # JSON object; a prose answer that happens to end in `}` must not be
        # mistaken for one.
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        events = []
        for ln in lines:
            try:
                ev = json.loads(ln)
            except Exception:
                events = []
                break
            if not isinstance(ev, dict):
                events = []
                break
            events.append(ev)
        if events:
            texts = [str(ev.get("text") or ev.get("content") or "")
                     for ev in events
                     if ev.get("type") in ("assistant", "message", "response")]
            joined = "\n".join(t for t in texts if t)
            if joined.strip():
                return joined
    return stdout


# ---------------------------------------------------------------------------
# Review parsing
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict[str, Any] | None:
    """Pull a review object out of model output.

    Only Codex can be schema-constrained, so this has to cope with prose
    wrappers, fenced blocks and trailing commentary from the other harnesses.
    """
    if not text:
        return None
    candidates: list[str] = []
    candidates += re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    # Last resort: widest brace span.
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])

    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and ("findings" in obj or "verdict" in obj):
            return obj
    return None


def normalise_findings(reviewer_id: str, obj: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not obj:
        return []
    out = []
    for i, f in enumerate(obj.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "medium")).strip().lower()
        if sev not in SEVERITIES:
            sev = "medium"
        out.append({
            "id": f.get("id") or f"{reviewer_id.upper()}-{i + 1:03d}",
            "reviewer": reviewer_id,
            "severity": sev,
            "file": f.get("file") or "",
            "line": f.get("line"),
            "category": f.get("category") or "",
            "problem": (f.get("problem") or "").strip(),
            "impact": (f.get("impact") or "").strip(),
            "recommended_fix": (f.get("recommended_fix") or "").strip(),
        })
    return out


def dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse findings that two reviewers raised about the same thing.

    Same file + overlapping line region + meaningful token overlap in the
    problem statement. Keeps the highest severity and records corroboration,
    because two independent reviewers agreeing is a useful signal.
    """
    def tokens(f: dict[str, Any]) -> set[str]:
        words = re.findall(r"[a-z_]{4,}", (f["problem"] + " " + f["category"]).lower())
        return set(words)

    kept: list[dict[str, Any]] = []
    for f in sorted(findings, key=lambda x: SEVERITIES.index(x["severity"])):
        merged = False
        for k in kept:
            if k["file"] and k["file"] == f["file"]:
                close = True
                if isinstance(k.get("line"), int) and isinstance(f.get("line"), int):
                    close = abs(k["line"] - f["line"]) <= 15
                a, b = tokens(k), tokens(f)
                overlap = len(a & b) / max(1, min(len(a), len(b)))
                if close and overlap >= 0.5:
                    k.setdefault("corroborated_by", [])
                    if f["reviewer"] not in k["corroborated_by"] and f["reviewer"] != k["reviewer"]:
                        k["corroborated_by"].append(f["reviewer"])
                    merged = True
                    break
        if not merged:
            kept.append(f)
    return kept


# ---------------------------------------------------------------------------
# Git + validation
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    return r.stdout.strip()


def base_commit(repo: Path) -> str | None:
    """The SHA the loop started from, or None in a repository with no commits."""
    r = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                       cwd=str(repo), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def current_diff(repo: Path, base_sha: str | None) -> str:
    """Everything the implementer changed: committed, staged, unstaged, new."""
    parts = []
    if base_sha:
        # Committed since the loop started, plus anything not yet committed.
        for rng in ((f"{base_sha}..HEAD",), ("HEAD",)):
            d = git(repo, "diff", *rng)
            if d.strip():
                parts.append(d)
    else:
        d = git(repo, "diff")
        if d.strip():
            parts.append(d)

    untracked = [f for f in git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
                 if f.strip() and not f.startswith(STATE_DIRNAME)]
    for path in untracked[:50]:
        full = repo / path
        try:
            if full.stat().st_size > 200_000:
                parts.append(f"--- new file (too large to inline): {path} ---")
                continue
            body = full.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            parts.append(f"--- new binary file: {path} ---")
            continue
        parts.append(f"--- new file: {path} ---\n{body}")
    return "\n".join(parts)


def run_validation(run: Run) -> tuple[bool, list[dict[str, Any]]]:
    cmds = (run.config.get("validation") or {}).get("commands") or []
    timeout = int(run.config.get("validation_timeout_seconds", 1800))
    results = []
    ok = True
    for cmd in cmds:
        run.emit("validation_start", command=cmd)
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(run.repo),
                               capture_output=True, text=True, timeout=timeout)
            passed = r.returncode == 0
            tail = (r.stdout + r.stderr)[-4000:]
        except subprocess.TimeoutExpired:
            passed = False
            tail = f"command timed out after {timeout}s"
        except Exception as exc:  # a broken command must not kill the run
            passed = False
            tail = f"could not execute: {exc}"
        ok = ok and passed
        results.append({"command": cmd, "passed": passed, "output_tail": tail})
        run.emit("validation_done", command=cmd, passed=passed)
    return ok, results


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

IMPLEMENTER_PROMPT = """\
You are the implementation engineer.

Complete the task described below. You have write access to this repository.

<task>
{task}
</task>

You may inspect the entire repository, modify source code, add tests, run
tests/build/lint, and refactor where necessary.

Do not merely describe changes. Implement them.

Follow the conventions already present in this repository rather than importing
your own. Match the surrounding code's structure, naming and test style.

{validation_note}

Before finishing:
1. Run the commands above and make them pass.
2. Inspect `git diff`.
3. Verify the requested behaviour actually works, not just that tests pass.
4. Leave the repository in a working state.

Do not modify anything inside the `.review-loop/` directory; it is orchestration
state, not part of the project.

Finish with a short summary of what you changed and why.
"""

FIX_PROMPT = """\
You are continuing your implementation.

<task>
{task}
</task>

{count} independent reviewer(s) examined your changes. Address every valid
finding below.

For EACH finding:
- Investigate it independently against the actual code.
- Fix it if valid.
- Add or change tests where appropriate.
- If the finding is demonstrably incorrect, explain why with concrete evidence.

Do not simply satisfy the wording of the review. Fix the underlying problem.

Findings marked `corroborated_by` were raised independently by more than one
reviewer; treat those as high-confidence.

After your changes:
- Run the relevant tests.
- Run lint/typecheck/build as appropriate.
- Inspect the resulting diff.

<reviews>
{reviews}
</reviews>

Finish with a short summary: which findings you fixed, and which you rejected
and why.
"""

REVIEW_PROMPT = """\
{persona}

Do NOT modify the repository. You have read-only access. Review only.

<task>
{task}
</task>

The implementation below is the change under review. Inspect the surrounding
code in the repository rather than reviewing this diff in isolation.

<diff>
{diff}
</diff>

{validation_note}

Review the implementation from scratch. Do not assume previous review rounds
were correct or complete, and look for regressions introduced by recent fixes.

Return ONLY a JSON object matching this shape, with no prose before or after it:

{{
  "verdict": "approved" | "changes_requested",
  "findings": [
    {{
      "id": "SHORT-001",
      "severity": "blocker" | "high" | "medium" | "low" | "nit",
      "file": "path/to/file.ts",
      "line": 74,
      "category": "correctness",
      "problem": "What is wrong.",
      "impact": "Why it matters.",
      "recommended_fix": "What to do instead."
    }}
  ]
}}

Rules:
- Return only actionable findings. Do not compliment the implementation.
- Do not raise optional stylistic preferences.
- If you find nothing actionable, return "approved" with an empty findings list.
"""


def load_persona(persona_id: str) -> str:
    path = PERSONA_DIR / f"{persona_id}.md"
    if not path.exists():
        return f"You are a {persona_id.replace('-', ' ')} conducting an adversarial code review."
    text = path.read_text()
    return re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S).strip()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text())
    repo = Path(config.get("repo") or os.getcwd()).resolve()
    state = repo / STATE_DIRNAME
    state.mkdir(exist_ok=True)

    run_dir = Path(config["run_dir"]) if config.get("run_dir") else _new_run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(repo, run_dir, config)

    (run_dir / "config.json").write_text(json.dumps(config, indent=2))
    (state / "task.md").write_text(config["task"])

    base_sha = base_commit(repo)
    if base_sha is None:
        run.emit("warning", message="Repository has no commits; reviewing the whole working tree.")
    run.emit("run_start", repo=str(repo), base_sha=base_sha,
             reviewers=[r["persona"] for r in config["reviewers"]],
             max_iterations=config.get("max_iterations", 5))

    task = config["task"]
    blocking = set(config.get("blocking_severities") or ["blocker", "high", "medium"])
    max_iter = int(config.get("max_iterations", 5))

    # --- initial implementation -------------------------------------------
    cmds = (config.get("validation") or {}).get("commands") or []
    gate_note = ("This change will be judged by these commands, which are run "
                 "independently of your own testing:\n"
                 + "\n".join(f"  {c}" for c in cmds)) if cmds else \
                ("No validation commands are configured, so run whatever tests "
                 "this project already has.")

    ok, text = invoke_agent(run, config["implementer"],
                            IMPLEMENTER_PROMPT.format(task=task, validation_note=gate_note),
                            readonly=False, label="Implementer",
                            log_name="iter00-implementer", attempts=2)
    if not ok:
        run.emit("run_failed", stage="implement", error=text)
        return 1
    (run_dir / "implementer-00.md").write_text(text)

    outcome = "max_iterations_reached"
    all_findings: list[dict[str, Any]] = []
    rounds_run = 0
    last_val_ok = False

    for iteration in range(1, max_iter + 1):
        if run.should_stop():
            outcome = "stopped_by_user"
            break
        rounds_run = iteration

        run.emit("iteration_start", iteration=iteration)

        # --- independent gate: tests decide, not model consensus -----------
        val_ok, val_results = run_validation(run)
        last_val_ok = val_ok
        (run_dir / f"validation-{iteration:02d}.json").write_text(json.dumps(val_results, indent=2))

        diff = current_diff(repo, base_sha)
        if not diff.strip():
            run.emit("warning", message="Diff is empty; implementer may not have changed anything.")

        max_diff = int(config.get("max_diff_chars", 180000))
        if len(diff) > max_diff:
            diff = diff[:max_diff] + f"\n\n[diff truncated at {max_diff} chars]"

        validation_note = _validation_note(val_ok, val_results)

        # --- reviewers, in parallel, read-only, no shared context ----------
        def do_review(rv: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
            prompt = REVIEW_PROMPT.format(
                persona=load_persona(rv["persona"]),
                task=task, diff=diff, validation_note=validation_note,
            )
            ok_, text_ = invoke_agent(
                run, rv, prompt, readonly=True,
                label=rv.get("label") or rv["persona"],
                log_name=f"iter{iteration:02d}-{rv['persona']}",
            )
            return rv, ok_, text_

        reviewers = config["reviewers"]
        workers = len(reviewers) if config.get("parallel", True) else 1
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(do_review, reviewers))

        round_findings: list[dict[str, Any]] = []
        for rv, ok_, text_ in results:
            obj = extract_json(text_) if ok_ else None
            if ok_ and obj is None:
                # One repair attempt: hand the output back and demand JSON only.
                repair = ("Your previous response was not valid JSON. Return ONLY the JSON "
                          "object described earlier, with no prose.\n\n<previous>\n"
                          + text_[:8000] + "\n</previous>")
                ok2, text2 = invoke_agent(run, rv, repair, readonly=True,
                                          label=f"{rv['persona']} (repair)",
                                          log_name=f"iter{iteration:02d}-{rv['persona']}-repair")
                obj = extract_json(text2) if ok2 else None
                if obj is None:
                    run.emit("review_unparsed", reviewer=rv["persona"])
            found = normalise_findings(rv["persona"], obj)
            (run_dir / f"review-{iteration:02d}-{rv['persona']}.json").write_text(
                json.dumps(obj or {"verdict": "unparsed", "findings": []}, indent=2))
            counts = {s: sum(1 for x in found if x["severity"] == s) for s in SEVERITIES}
            run.emit("review_done", reviewer=rv["persona"],
                     label=rv.get("label") or rv["persona"],
                     verdict=(obj or {}).get("verdict", "unparsed"),
                     counts={k: v for k, v in counts.items() if v})
            round_findings += found

        merged = dedupe(round_findings)
        (run_dir / f"findings-{iteration:02d}.json").write_text(json.dumps(merged, indent=2))
        all_findings = merged

        blockers = [f for f in merged if f["severity"] in blocking]
        run.emit("round_summary", iteration=iteration, total=len(merged),
                 blocking=len(blockers), validation_passed=val_ok)

        if not blockers and val_ok:
            outcome = "approved"
            break
        if not blockers and not val_ok:
            run.emit("warning", message="Reviewers approved but validation is failing; "
                                        "sending validation failures back to the implementer.")

        if iteration == max_iter:
            outcome = "max_iterations_reached"
            break

        payload = json.dumps(blockers or merged, indent=2)
        if not val_ok:
            payload += "\n\nVALIDATION FAILURES:\n" + validation_note
        ok, text = invoke_agent(run, config["implementer"],
                                FIX_PROMPT.format(task=task, count=len(reviewers),
                                                  reviews=payload),
                                readonly=False, label="Implementer (fixes)",
                                log_name=f"iter{iteration:02d}-fix", attempts=2)
        if not ok:
            run.emit("run_failed", stage="fix", error=text)
            outcome = "implementer_failed"
            break
        (run_dir / f"implementer-{iteration:02d}.md").write_text(text)

    final = _write_final(run, base_sha, outcome, all_findings,
                         rounds_run, last_val_ok, blocking)
    run.emit("run_complete", outcome=outcome, rounds=rounds_run,
             findings=len(all_findings),
             blocking_open=sum(1 for f in all_findings if f["severity"] in blocking),
             validation_passed=last_val_ok, final=str(final))
    return 0 if outcome == "approved" else 2


def _validation_note(ok: bool, results: list[dict[str, Any]]) -> str:
    if not results:
        return "No validation commands were configured for this run."
    lines = ["Validation results (these are ground truth, not opinion):"]
    for r in results:
        lines.append(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['command']}")
        if not r["passed"]:
            lines.append(textwrap.indent(r["output_tail"][-1500:], "      "))
    return "\n".join(lines)


def _new_run_dir(state: Path) -> Path:
    history = state / "history"
    history.mkdir(parents=True, exist_ok=True)
    n = len([d for d in history.iterdir() if d.is_dir()]) + 1
    return history / f"run-{n:02d}"


OUTCOME_NOTE = {
    "approved": "No blocking findings remain and validation passed.",
    "max_iterations_reached": "The loop hit its iteration cap with blocking findings "
                              "still open. This needs a human — do not simply raise the cap.",
    "stopped_by_user": "Stopped on request. The working tree holds whatever the last "
                       "completed step produced.",
    "implementer_failed": "The build agent could not complete a fix round. See logs/.",
}


def _write_final(run: Run, base_sha: str | None, outcome: str,
                 findings: list[dict[str, Any]], rounds: int,
                 validation_passed: bool, blocking: set[str]) -> Path:
    blockers = [f for f in findings if f["severity"] in blocking]
    advisory = [f for f in findings if f["severity"] not in blocking]

    lines = [
        "# Review loop result", "",
        f"**Outcome: {outcome}** — {OUTCOME_NOTE.get(outcome, '')}", "",
        f"- Review rounds: {rounds}",
        f"- Base SHA: `{base_sha or '(no commits)'}`",
        f"- Reviewers: {', '.join(r.get('label') or r['persona'] for r in run.config['reviewers'])}",
        f"- Validation: {'passing' if validation_passed else 'FAILING'}",
        f"- Blocking findings open: {len(blockers)}",
        f"- Advisory findings recorded: {len(advisory)}", "",
    ]

    def section(title: str, items: list[dict[str, Any]], note: str) -> None:
        # Every mutation here must be a method call: an augmented assignment
        # (`lines += ...`) would rebind `lines` as a local and break the closure.
        if not items:
            return
        lines.extend([f"## {title}", "", note, ""])
        for f in items:
            corr = f.get("corroborated_by")
            tag = (f" · independently raised by {', '.join(corr)}") if corr else ""
            loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
            lines.extend([f"### [{f['severity'].upper()}] {f['id']} — {loc}{tag}", "",
                          f"**Problem.** {f['problem']}", "",
                          f"**Impact.** {f['impact']}", "",
                          f"**Fix.** {f['recommended_fix']}", ""])

    section("Blocking findings still open", blockers,
            "These met the blocking threshold and were not resolved.")
    section("Advisory findings", advisory,
            "Below the blocking threshold. Recorded for your judgement; "
            "the loop did not iterate on them.")

    if not findings:
        lines += ["## Findings", "", "None outstanding.", ""]

    body = "\n".join(lines)
    path = run.dir / "final.md"
    path.write_text(body)
    (run.repo / STATE_DIRNAME / "final.md").write_text(body)
    return path


# ---------------------------------------------------------------------------
# start / status / stop
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text())
    repo = Path(config.get("repo") or os.getcwd()).resolve()
    state = repo / STATE_DIRNAME
    state.mkdir(exist_ok=True)
    run_dir = _new_run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    config["run_dir"] = str(run_dir)
    resolved = run_dir / "config.json"
    resolved.write_text(json.dumps(config, indent=2))

    _ensure_gitignore(repo)

    log = (run_dir / "run.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "run", "--config", str(resolved)],
        cwd=str(repo), stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (run_dir / "pid").write_text(str(proc.pid))
    (state / "current-run").write_text(str(run_dir))
    print(json.dumps({"run_dir": str(run_dir), "pid": proc.pid,
                      "progress": str(run_dir / "progress.jsonl")}, indent=2))
    return 0


def _ensure_gitignore(repo: Path) -> None:
    gi = repo / ".gitignore"
    entry = f"{STATE_DIRNAME}/"
    try:
        existing = gi.read_text() if gi.exists() else ""
        if entry not in existing:
            with gi.open("a") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(f"\n# review-loop working state\n{entry}\n")
    except Exception:
        pass


def _resolve_run(args: argparse.Namespace) -> Path | None:
    if getattr(args, "run", None):
        return Path(args.run)
    repo = Path(getattr(args, "repo", None) or os.getcwd())
    state = repo / STATE_DIRNAME
    pointer = state / "current-run"
    if pointer.exists():
        return Path(pointer.read_text().strip())
    history = state / "history"
    if history.exists():
        dirs = sorted([d for d in history.iterdir() if d.is_dir()])
        if dirs:
            return dirs[-1]
    return None


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = _resolve_run(args)
    if not run_dir or not run_dir.exists():
        print(json.dumps({"error": "no run found"}))
        return 1
    events = []
    pf = run_dir / "progress.jsonl"
    if pf.exists():
        for line in pf.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    running = False
    pid_file = run_dir / "pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            running = True
        except Exception:
            running = False
    print(json.dumps({"run_dir": str(run_dir), "running": running,
                      "events": events[-args.tail:]}, indent=2))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    run_dir = _resolve_run(args)
    if not run_dir:
        print(json.dumps({"error": "no run found"}))
        return 1
    (run_dir / "STOP").write_text("stop")
    pid_file = run_dir / "pid"
    if args.kill and pid_file.exists():
        try:
            os.killpg(os.getpgid(int(pid_file.read_text().strip())), signal.SIGTERM)
        except Exception:
            pass
    print(json.dumps({"stopping": str(run_dir)}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="review_loop")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect").set_defaults(func=cmd_detect)

    s = sub.add_parser("start"); s.add_argument("--config", required=True)
    s.set_defaults(func=cmd_start)

    r = sub.add_parser("run"); r.add_argument("--config", required=True)
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status")
    st.add_argument("--run"); st.add_argument("--repo")
    st.add_argument("--tail", type=int, default=40)
    st.set_defaults(func=cmd_status)

    sp = sub.add_parser("stop")
    sp.add_argument("--run"); sp.add_argument("--repo")
    sp.add_argument("--kill", action="store_true")
    sp.set_defaults(func=cmd_stop)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
