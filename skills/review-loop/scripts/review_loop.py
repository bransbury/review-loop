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
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import threading
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


def installed_version() -> str:
    """Read the installed skill version without maintaining a third copy."""
    try:
        text = (SKILL_DIR / "SKILL.md").read_text()
    except OSError:
        return "unknown"
    frontmatter_end = text.find("\n---\n", 4)
    if frontmatter_end == -1:
        return "unknown"
    match = re.search(r"(?m)^version:\s*([^\s]+)\s*$", text[:frontmatter_end])
    return match.group(1) if match else "unknown"


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
        # `write` deliberately does not cover the shell tool — Copilot's own
        # permissions help says so — and denials override --allow-all-tools.
        # Denying specific commands is therefore useless: `sed -i`, `rm` and a
        # shell redirection all still mutate the tree. Deny the shell outright
        # and leave the reviewer its read-only file tools.
        argv += ["--deny-tool", "write", "--deny-tool", "shell"]
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


def cmd_suggest(args: argparse.Namespace) -> int:
    """Recommend a review panel from the task text and the repository.

    Picking reviewers blind is the hardest part of configuring a run, and a
    panel that misses the security reviewer on an auth change is worse than
    useless. This is advisory only — the wizard still asks.
    """
    repo = Path(args.repo or os.getcwd()).resolve()
    task = (args.task or "").lower()

    paths = [p.lower() for p in git(repo, "ls-files").splitlines()][:5000]

    suggested, considered = [], []
    for path in sorted(PERSONA_DIR.glob("*.md")):
        meta = _persona_meta(path)
        reasons = []
        is_default = str(meta.get("default", "")).lower() == "true"
        if is_default:
            reasons.append("always recommended")

        hits = [k for k in _csv(meta.get("keywords")) if _keyword_hit(k, task)]
        if hits:
            reasons.append("task mentions " + ", ".join(sorted({h.rstrip("*") for h in hits})[:4]))

        sig = [s for s in _csv(meta.get("signals")) if _signal_hit(s, paths)]
        if sig:
            reasons.append("repository contains " + ", ".join(sorted(set(sig))[:4]))

        entry = {"id": path.stem, "name": meta.get("name", path.stem),
                 "description": meta.get("description", ""), "reasons": reasons,
                 # Ranking only; stripped before output.
                 "_rank": (0 if is_default else 1, -len(hits), -len(sig))}
        (suggested if reasons else considered).append(entry)

    # More than four reviewers mostly produces duplicate findings and a slower
    # loop. Defaults first, then whatever the task itself argued for.
    suggested.sort(key=lambda e: e["_rank"])
    if len(suggested) > 4:
        suggested, trimmed = suggested[:4], suggested[4:]
        considered = trimmed + considered
    for e in suggested + considered:
        e.pop("_rank", None)

    print(json.dumps({"repo": str(repo), "suggested": suggested,
                      "other_personas": considered,
                      "note": "Advisory only. Two or three reviewers is the useful range."},
                     indent=2))
    return 0


def _csv(value: Any) -> list[str]:
    return [v.strip().lower() for v in str(value or "").split(",") if v.strip()]


def _keyword_hit(keyword: str, text: str) -> bool:
    """Match a persona keyword against the task description.

    Whole words by default, so "log" does not fire on "login". A trailing `*`
    asks for prefix matching, for stems like `optimi*` that need to cover both
    spellings.
    """
    if not keyword:
        return False
    if keyword.endswith("*"):
        return re.search(rf"\b{re.escape(keyword[:-1])}\w*", text) is not None
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def _signal_hit(signal: str, paths: list[str]) -> bool:
    """Match a repository signal against tracked file paths.

    Extensions (".tsx") match a suffix; everything else must match a whole
    path segment, so a `security` signal fires on `src/security/...` but not
    on an unrelated file that merely has the word in its name.
    """
    if not signal:
        return False
    if signal.startswith("."):
        return any(p.endswith(signal) for p in paths)
    return any(signal in p.strip("/").split("/") for p in paths)


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
# Configuration
# ---------------------------------------------------------------------------

def validate_config(config: dict[str, Any]) -> list[str]:
    """Check a run config and assign a unique slot id to every reviewer.

    Mutates `config` in place. Returns human-readable errors; an empty list
    means the run is safe to start. Catching these before launch matters: a
    config with no reviewers would otherwise "approve" instantly and look
    like a clean review.
    """
    errors: list[str] = []

    if not str(config.get("task", "")).strip():
        errors.append("`task` is required and cannot be empty.")

    impl = config.get("implementer")
    if not isinstance(impl, dict):
        errors.append("`implementer` is required.")
    elif impl.get("cli") not in ADAPTERS:
        errors.append(f"implementer.cli must be one of {sorted(ADAPTERS)}, "
                      f"got {impl.get('cli')!r}.")

    reviewers = config.get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        errors.append("At least one reviewer is required.")
        reviewers = []

    seen: dict[str, int] = {}
    for i, rv in enumerate(reviewers):
        if not isinstance(rv, dict):
            errors.append(f"reviewers[{i}] must be an object.")
            continue
        persona = rv.get("persona")
        if not persona:
            errors.append(f"reviewers[{i}].persona is required.")
            continue
        if rv.get("cli") not in ADAPTERS:
            errors.append(f"reviewers[{i}].cli must be one of {sorted(ADAPTERS)}, "
                          f"got {rv.get('cli')!r}.")
        # The same persona may legitimately appear twice on different models.
        # Give each slot a unique id so their outputs, log files and finding
        # ids stay distinct — and so they can corroborate each other.
        seen[persona] = seen.get(persona, 0) + 1
        rv["slot_id"] = persona if seen[persona] == 1 else f"{persona}-{seen[persona]}"
        # Derived fields are rebuilt from `label_base`, never from the last
        # result. `start` validates a config and then the detached `run`
        # validates the file it wrote, so anything appended in place would be
        # appended twice — "Security Engineer #2 #2".
        if not rv.get("label_base"):
            # Prefer the persona's own name ("Adversarial QA") over title-casing
            # the slug, which mangles acronyms into "Adversarial Qa".
            meta = _persona_meta(PERSONA_DIR / f"{persona}.md") \
                if (PERSONA_DIR / f"{persona}.md").exists() else {}
            rv["label_base"] = (rv.get("label")
                                or meta.get("name")
                                or persona.replace("-", " ").title())
        rv["label"] = rv["label_base"] if seen[persona] == 1 \
            else f"{rv['label_base']} #{seen[persona]}"

    for slot in ([impl] if isinstance(impl, dict) else []) + \
                [r for r in reviewers if isinstance(r, dict)]:
        cli = slot.get("cli")
        ad = ADAPTERS.get(cli)
        if ad and not shutil.which(ad["bin"]):
            errors.append(f"`{ad['bin']}` is configured but not on PATH.")
        effort = slot.get("effort")
        if ad and effort and effort not in ad["efforts"]:
            errors.append(f"effort {effort!r} is not supported by {cli}; "
                          f"choose from {ad['efforts']}.")

    # The independent gate is the reason this tool is worth running. Validate
    # its shape before treating it as present: a string would otherwise be run
    # one character at a time, while an empty string is a successful shell
    # no-op and could make "nothing ran" read as "validation passed".
    validation = config.get("validation")
    if validation is None:
        validation = {}
    commands: list[Any] = []
    validation_shape_ok = True
    if not isinstance(validation, dict):
        errors.append("`validation` must be an object with a `commands` list.")
        validation_shape_ok = False
    else:
        raw_commands = validation.get("commands", [])
        if not isinstance(raw_commands, list):
            errors.append("`validation.commands` must be a list of shell command strings.")
            validation_shape_ok = False
        else:
            commands = raw_commands
            bad_commands = [i for i, cmd in enumerate(commands)
                            if not isinstance(cmd, str) or not cmd.strip()]
            if bad_commands:
                errors.append("`validation.commands` contains empty or non-string entries "
                              f"at indexes {bad_commands}.")
                validation_shape_ok = False

    if validation_shape_ok and not commands \
            and not config.get("allow_missing_validation"):
        errors.append("`validation.commands` is empty: there would be no independent "
                      "gate, and the loop could only approve on model consensus. "
                      "Add commands, or set `allow_missing_validation: true` to "
                      "accept a run whose outcome is unverified.")

    bad = set(config.get("blocking_severities") or []) - set(SEVERITIES)
    if bad:
        errors.append(f"unknown blocking severities: {sorted(bad)}")

    try:
        if int(config.get("max_iterations", 5)) < 1:
            errors.append("`max_iterations` must be at least 1.")
    except (TypeError, ValueError):
        errors.append("`max_iterations` must be a number.")

    return errors


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
        # Reviewers run concurrently and all emit here; without this the
        # progress stream can interleave into unparseable lines.
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        line = json.dumps(rec)
        with self._lock:
            with self.progress.open("a") as fh:
                fh.write(line + "\n")
            # Also echo for foreground/log tailing.
            print(line, flush=True)

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

    # Transcripts are for debugging a run, not archiving it. Keep the head and
    # tail of each stream: the middle of a long agent transcript is where the
    # least useful bytes live.
    log_cap = int(run.config.get("max_log_chars", 100_000))
    log_file.write_text(
        f"$ {' '.join(argv[:6])} ...\n\n"
        f"--- STDOUT ---\n{_head_tail(proc.stdout, log_cap)}\n"
        f"--- STDERR ---\n{_head_tail(proc.stderr, log_cap // 4)}"
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

    run.emit("agent_done", label=label, seconds=elapsed, chars=len(text),
             prompt_chars=len(prompt), **_usage(cli, proc.stdout))
    return True, text


def _usage(cli: str, stdout: str) -> dict[str, Any]:
    """Pull real token and cost figures out of the CLI's response envelope.

    Only Claude reports these today. Where a CLI does not, spend stays
    unreported rather than being guessed at from character counts.
    """
    if cli != "claude":
        return {}
    try:
        env = json.loads(stdout)
        u = env.get("usage") or {}
        out: dict[str, Any] = {}
        for key, name in (("input_tokens", "in"), ("output_tokens", "out"),
                          ("cache_read_input_tokens", "cache_read"),
                          ("cache_creation_input_tokens", "cache_write")):
            if isinstance(u.get(key), int):
                out[f"tok_{name}"] = u[key]
        if isinstance(env.get("total_cost_usd"), (int, float)):
            out["cost_usd"] = round(env["total_cost_usd"], 4)
        return out
    except Exception:
        return {}


def _head_tail(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text or ""
    half = limit // 2
    cut = len(text) - limit
    return f"{text[:half]}\n\n… [{cut:,} chars elided] …\n\n{text[-half:]}"


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


def _verdict(obj: dict[str, Any]) -> str:
    """The reviewer's own verdict, normalised.

    Returns "" when the reviewer did not state one. Anything stated but
    unrecognised counts as `changes_requested`: approval has to be explicit.
    """
    raw = obj.get("verdict")
    if raw is None or not str(raw).strip():
        return ""
    return "approved" if str(raw).strip().lower() == "approved" else "changes_requested"


def review_problem(obj: dict[str, Any] | None) -> str | None:
    """Why this review object cannot be trusted, or None if it is well formed.

    `extract_json` only asks whether *something* JSON-shaped came back, and
    only Codex can be schema-constrained, so the shape has to be checked here
    for every adapter. Anything that fails this is a reviewer that did not
    report — never a reviewer that approved. The permissive readings are the
    dangerous ones: a missing verdict and a `findings` value that is not a list
    both normalise to zero findings, which reads exactly like a clean review.
    """
    if not isinstance(obj, dict):
        return "review was not a JSON object"
    if "verdict" not in obj:
        return "no verdict field"
    verdict = _verdict(obj)
    if not verdict:
        return "empty verdict field"
    findings = obj.get("findings")
    if not isinstance(findings, list):
        return f"`findings` was {type(findings).__name__}, not a list"
    if any(not isinstance(f, dict) for f in findings):
        return "`findings` contained entries that were not objects"
    if verdict == "changes_requested" and not findings:
        return "requested changes but listed no findings"
    return None


def normalise_findings(reviewer_id: str, obj: dict[str, Any] | None,
                       max_findings: int = 10) -> list[dict[str, Any]]:
    """Parse and bound one reviewer's output.

    Enforces in code what the prompt asks for: no nits, a finding cap, and
    fields short enough that a panel's worth of them stays readable. A
    reviewer that ignores the instruction cannot flood the loop.
    """
    if not obj:
        return []
    raw = obj.get("findings")
    out = []
    for i, f in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "medium")).strip().lower()
        if sev not in SEVERITIES:
            sev = "medium"
        if sev == "nit":
            # Policy discards these, so carrying them costs tokens for nothing.
            continue
        out.append({
            "id": f.get("id") or f"{reviewer_id.upper()}-{i + 1:03d}",
            "reviewer": reviewer_id,
            "severity": sev,
            "file": f.get("file") or "",
            "line": f.get("line"),
            "category": f.get("category") or "",
            "problem": _clip(f.get("problem")),
            "impact": _clip(f.get("impact")),
            "recommended_fix": _clip(f.get("recommended_fix")),
        })
    # Most severe first, then bound. A reviewer that returns forty findings is
    # padding, and the tail would be paid for by the implementer next round.
    out.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return out[:max_findings]


def _clip(value: Any, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


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


def _same_defect(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two findings, possibly from different rounds, describe one defect.

    Same rule as `dedupe`, applied across time instead of across reviewers:
    reviewers reword a finding between rounds, so an exact-string key would
    report every survivor as a brand new defect.
    """
    if not a.get("file") or a.get("file") != b.get("file"):
        return False
    if isinstance(a.get("line"), int) and isinstance(b.get("line"), int) \
            and abs(a["line"] - b["line"]) > 15:
        return False

    def tokens(f: dict[str, Any]) -> set[str]:
        text = (str(f.get("problem", "")) + " " + str(f.get("category", ""))).lower()
        return set(re.findall(r"[a-z_]{4,}", text))

    x, y = tokens(a), tokens(b)
    return len(x & y) / max(1, min(len(x), len(y))) >= 0.5


class Ledger:
    """Every finding the panel has raised, and what became of it.

    The loop re-reviews from scratch each round, so the last round's merged
    list is a snapshot, not a history: it cannot say what was raised and
    fixed, and a low-severity finding that a later panel did not repeat would
    simply vanish from the report. This keeps them all, with a state.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record_round(self, iteration: int, findings: list[dict[str, Any]],
                     panel_complete: bool = True) -> None:
        matched: list[dict[str, Any]] = []
        for f in findings:
            hit = next((e for e in self.entries if _same_defect(e, f)), None)
            if hit is None:
                entry = dict(f)
                entry.update({"state": "open", "first_seen": iteration,
                              "last_seen": iteration, "rounds_seen": [iteration]})
                self.entries.append(entry)
                matched.append(entry)
                continue
            # A defect that came back after a round without it is worth calling
            # out separately: the fix for it did not hold.
            if hit["state"] == "resolved":
                hit["state"] = "reappeared"
                hit.pop("resolved_in", None)
            else:
                hit["state"] = "open"
            # Keep the most severe wording the panel has used for it.
            if SEVERITIES.index(f["severity"]) < SEVERITIES.index(hit["severity"]):
                hit.update({k: f[k] for k in
                            ("severity", "problem", "impact", "recommended_fix")})
            hit["last_seen"] = iteration
            hit["rounds_seen"].append(iteration)
            for who in [f["reviewer"]] + list(f.get("corroborated_by") or []):
                if who != hit["reviewer"] and who not in hit.setdefault("corroborated_by", []):
                    hit["corroborated_by"].append(who)
            matched.append(hit)

        # A finding that this round did not repeat is only evidence of a fix if
        # the whole panel actually reported. If a reviewer dropped out, silence
        # about its findings means nothing.
        if not panel_complete:
            return
        seen = {id(e) for e in matched}
        for e in self.entries:
            if id(e) not in seen and e["state"] in ("open", "reappeared"):
                e["state"] = "resolved"
                e["resolved_in"] = iteration

    def open_findings(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["state"] in ("open", "reappeared")]

    def resolved(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["state"] == "resolved"]

    def to_json(self) -> list[dict[str, Any]]:
        return self.entries


# ---------------------------------------------------------------------------
# Git + validation
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    return r.stdout.strip()


def _porcelain_entries(raw: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain -z` into (status, path) pairs.

    `-z` because the default format C-quotes any path with a space or a
    non-ASCII byte, which slicing would then mangle. Rename and copy entries
    carry a second, trailing field holding the source path; it has to be
    consumed or it reads as an entry of its own with a garbled status.
    """
    fields = raw.split("\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        status, path = rec[:2], rec[3:]
        if "R" in status or "C" in status:
            i += 1   # skip the source path that follows
        out.append((status, path))
    return out


def _is_state_path(path: str) -> bool:
    """Whether a repository path is the orchestrator's own state directory.

    Exact match or a directory prefix — a `startswith` on the bare name also
    swallows sibling files like `.review-loop-config`, which are the user's.
    """
    p = path.rstrip("/")
    return p == STATE_DIRNAME or p.startswith(STATE_DIRNAME + "/")


def preflight_repo(repo: Path, config: dict[str, Any]) -> list[str]:
    """Refuse to run somewhere the loop could do damage it cannot undo.

    The skill asks its host to check this, but the script is a CLI and is
    routinely driven directly, so the guarantee has to live here. Both gates
    are overridable — deliberately, and only in the config.
    """
    problems: list[str] = []
    if not repo.is_dir():
        return [f"repo path does not exist: {repo}"]

    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=str(repo), capture_output=True, text=True)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        if not config.get("allow_non_git"):
            problems.append(f"{repo} is not a git working tree. The build agent edits "
                            "files in place and there would be no way to see or undo "
                            "what it changed. Set `allow_non_git: true` to override.")
        return problems

    # --porcelain omits ignored files, so `.review-loop/` normally never trips
    # this — but an older run, or a user who deleted its .gitignore, still can,
    # and orchestration state is not the user's work.
    raw = subprocess.run(["git", "status", "--porcelain", "-z"],
                         cwd=str(repo), capture_output=True, text=True).stdout
    dirty = [path for _, path in _porcelain_entries(raw) if not _is_state_path(path)]
    if dirty and not config.get("allow_dirty"):
        names = dirty[:10]
        more = f" (+{len(dirty) - 10} more)" if len(dirty) > 10 else ""
        problems.append("the working tree is dirty, so uncommitted work would be mixed "
                        "into the diff under review and may be modified by the build "
                        f"agent: {', '.join(names)}{more}. Commit or stash first, or set "
                        "`allow_dirty: true`.")
    return problems


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except Exception:
        return False
    return True


def new_lock_token() -> str:
    return secrets.token_hex(16)


def acquire_lock(state: Path, token: str, pid: int,
                 run_dir: Path | None = None,
                 adopt_only: bool = False) -> str | None:
    """Claim the one-run-per-worktree lock. Returns an error string, or None.

    Two concurrent runs would edit the same files, review each other's
    half-finished changes and race on `current-run`. The create is atomic, so
    the loser finds out rather than silently proceeding.

    `token` is how a run proves the lock is its own. `start` mints one, takes
    the lock, and passes the token to the detached `run` it spawns, which
    presents it to adopt the lock under its own pid. It must be an unguessable
    secret rather than something derivable — two `start` calls racing pick the
    same next run number, so identifying a run by its directory would let each
    mistake the other for its own child. Adoption rotates the token, so a
    handoff can only be used once. A detached child uses `adopt_only`: if the
    parent-held lock is gone, it must fail rather than recreate the lock and
    replay an old resolved config over an existing run directory.
    """
    lock = state / "lock"

    def payload(tok: str) -> str:
        return json.dumps({"pid": pid, "token": tok,
                           "run_dir": str(run_dir) if run_dir else None,
                           "started": datetime.now(timezone.utc).isoformat()})

    for _ in range(100):
        if not adopt_only:
            # Write the content first and link it into place: creating the lock
            # empty and filling it a moment later leaves a window where a racing
            # caller reads nothing, concludes the lock is corrupt, and steals it.
            # `os.link` fails if the target exists, so the file is never partial.
            tmp = state / f"lock.{os.getpid()}.{secrets.token_hex(4)}"
            try:
                tmp.write_text(payload(token))
                try:
                    os.link(str(tmp), str(lock))
                    return None
                except FileExistsError:
                    pass
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass

        try:
            held = json.loads(lock.read_text())
            if not isinstance(held, dict):
                raise ValueError
        except FileNotFoundError:
            if adopt_only:
                return ("could not adopt the review-loop lock: the parent-held lock "
                        "is missing or has already been consumed. Start a new run "
                        "instead of replaying a resolved config.")
            continue
        except Exception:
            # Unreadable. Give whoever wrote it time to be recognisable rather
            # than assuming the worktree is free.
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                if adopt_only:
                    return ("could not adopt the review-loop lock: the parent-held lock "
                            "is missing or has already been consumed. Start a new run "
                            "instead of replaying a resolved config.")
                continue
            if age < 60:
                return ("another review-loop run is holding this worktree "
                        "(its lock file is unreadable). Retry, or remove "
                        f"{lock} if you are certain nothing is running.")
            held = {}

        if held.get("token") and held.get("token") == token:
            # Our own handoff. Rotate the token so it cannot be replayed.
            lock.write_text(payload(new_lock_token()))
            return None
        other_pid = held.get("pid")
        if isinstance(other_pid, int) and other_pid > 0 and _pid_alive(other_pid):
            return (f"another review-loop run is already active in this worktree "
                    f"(pid {other_pid}, {held.get('run_dir') or 'starting up'}). "
                    f"Stop it first: review_loop.py stop --kill")
        # Stale lock from a crashed or killed run. A fresh caller may reclaim
        # it, but a detached child must only ever consume its exact handoff.
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        if adopt_only:
            return ("could not adopt the review-loop lock: its handoff token no longer "
                    "matches. Start a new run instead of replaying a resolved config.")
    return "could not acquire the review-loop lock; retry"


def handoff_lock(state: Path, token: str, pid: int, run_dir: Path) -> None:
    """Point the lock at the detached child while the handoff is still pending.

    Without this the lock briefly names a `start` process that has already
    exited, which the next caller would read as stale. Matching on the token
    makes it a no-op once the child has adopted the lock — or released it.
    """
    lock = state / "lock"
    try:
        held = json.loads(lock.read_text())
    except Exception:
        return
    if held.get("token") == token:
        held.update({"pid": pid, "run_dir": str(run_dir)})
        lock.write_text(json.dumps(held))


def release_lock(state: Path, run_dir: Path) -> None:
    lock = state / "lock"
    try:
        held = json.loads(lock.read_text())
    except Exception:
        return
    if held.get("run_dir") == str(run_dir):
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _state_for_run(run_dir: Path) -> Path | None:
    """Resolve the state directory from a standard history/run-NN path."""
    resolved = run_dir.resolve()
    if resolved.parent.name != "history":
        return None
    state = resolved.parent.parent
    return state if state.name == STATE_DIRNAME else None


def _locked_run_pid(run_dir: Path) -> int | None:
    """Return the pid only when the live lock names this exact run.

    A run's pid file is historical evidence, not authority to signal forever:
    after completion that pid may be reused by an unrelated process. The
    worktree lock is the active-run record and must agree with both paths.
    """
    state = _state_for_run(run_dir)
    if state is None:
        return None
    try:
        held = json.loads((state / "lock").read_text())
        pid = int(held.get("pid"))
        locked_dir = Path(str(held.get("run_dir"))).resolve()
        recorded_pid = int((run_dir / "pid").read_text().strip())
    except Exception:
        return None
    if locked_dir != run_dir.resolve() or recorded_pid != pid or pid <= 0:
        return None
    return pid


def _pid_is_review_loop(pid: int, run_dir: Path) -> bool:
    """Guard against a stale lock whose pid has been reused by another process."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    command = result.stdout.strip()
    return (result.returncode == 0
            and str(Path(__file__).resolve()) in command
            and str((run_dir / "config.json").resolve()) in command)


def base_commit(repo: Path) -> str | None:
    """The SHA the loop started from, or None in a repository with no commits."""
    r = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                       cwd=str(repo), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


# Files whose diffs cost a great deal of context and tell a reviewer nothing
# they can act on. A single lockfile change can be tens of thousands of tokens
# of pure noise, multiplied by every reviewer on every round.
NOISE_PATHSPECS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Cargo.lock", "poetry.lock", "Gemfile.lock", "composer.lock", "go.sum",
    "*.min.js", "*.min.css", "*.map", "*.snap",
    "dist/*", "build/*", "out/*", "vendor/*", "node_modules/*", ".next/*",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.woff", "*.woff2",
    "*.mp4", "*.zip", "*.parquet",
    "*_pb2.py", "*.pb.go", "*.generated.*", "__snapshots__/*",
]


def _exclude_args(config: dict[str, Any]) -> list[str]:
    specs = NOISE_PATHSPECS if config.get("exclude_noise", True) else []
    specs = list(specs) + list(config.get("exclude_paths") or [])
    return [f":(exclude,glob){s}" for s in specs] + [f":(exclude){STATE_DIRNAME}/*"]


def _split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, hunk-text) pairs."""
    out: list[tuple[str, str]] = []
    current: list[str] = []
    path = ""
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                out.append((path, "".join(current)))
            current = [line]
            bits = line.split(" b/", 1)
            path = bits[1].strip() if len(bits) > 1 else "?"
        else:
            current.append(line)
    if current:
        out.append((path, "".join(current)))
    return out


def collect_diff(repo: Path, base_sha: str | None,
                 config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the review payload and report what it cost.

    Returns the diff text plus a manifest describing anything excluded or
    truncated, so a reviewer is never silently shown a partial picture.
    """
    excl = _exclude_args(config)
    parts: list[str] = []
    ranges = [(f"{base_sha}..HEAD",), ("HEAD",)] if base_sha else [()]
    for rng in ranges:
        d = git(repo, "diff", *rng, "--", ".", *excl)
        if d.strip():
            parts.append(d)

    untracked = [f for f in git(repo, "ls-files", "--others", "--exclude-standard",
                                "--", ".", *excl).splitlines()
                 if f.strip() and not f.startswith(STATE_DIRNAME)]

    per_file = int(config.get("max_file_chars", 20000))
    max_untracked = int(config.get("max_untracked_files", 60))
    manifest: dict[str, Any] = {"truncated_files": [], "skipped_files": [],
                                "omitted_files": untracked[max_untracked:],
                                "untracked_files": len(untracked),
                                "untracked_included": min(len(untracked), max_untracked)}

    files = _split_by_file("".join(parts))
    kept: list[str] = []
    for path, body in files:
        if len(body) > per_file:
            manifest["truncated_files"].append({"path": path, "chars": len(body)})
            body = body[:per_file] + f"\n... [{path} truncated at {per_file} chars]"
        # A diff whose final line has no newline would otherwise run into
        # whatever section follows it.
        kept.append(body if body.endswith("\n") else body + "\n")

    for path in untracked[:max_untracked]:
        full = repo / path
        try:
            info = full.lstat()
            if stat.S_ISLNK(info.st_mode):
                # Never follow an untracked symlink. Otherwise a repository can
                # point at ~/.ssh, a credential file or anything else readable
                # by the host and have its contents copied into every reviewer
                # prompt and transcript.
                target = os.readlink(full)
                kept.append(f"--- new symlink: {path} -> {target} ---\n")
                continue
            if not stat.S_ISREG(info.st_mode):
                # FIFOs can block forever on read; devices and sockets have no
                # meaningful text payload to inline. Name them without opening.
                manifest["skipped_files"].append({"path": path, "chars": 0})
                kept.append(f"--- new special file, not opened: {path} ---\n")
                continue
            size = info.st_size
            if size > per_file:
                manifest["skipped_files"].append({"path": path, "chars": size})
                kept.append(f"--- new file, too large to inline: {path} ({size} bytes) ---\n")
                continue
            body = full.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            manifest["skipped_files"].append({"path": path, "chars": 0})
            kept.append(f"--- new binary file: {path} ---\n")
            continue
        kept.append(f"--- new file: {path} ---\n{body}\n")

    # Anything past the cap must be named, not silently dropped: a reviewer that
    # is not told about a new file cannot know to go and read it.
    if manifest["omitted_files"]:
        listing = "\n".join(f"  {p}" for p in manifest["omitted_files"][:200])
        kept.append(f"--- {len(manifest['omitted_files'])} further new files were not "
                    f"inlined (cap: {max_untracked}); read them from the repository ---\n"
                    f"{listing}\n")

    diff = "".join(kept)

    # Whole-payload ceiling, applied last so per-file trimming does the work.
    max_total = int(config.get("max_diff_chars", 120000))
    if len(diff) > max_total:
        diff = diff[:max_total] + f"\n\n[diff truncated at {max_total} chars]\n"
        manifest["payload_truncated"] = True

    manifest["chars"] = len(diff)
    manifest["approx_tokens"] = len(diff) // 4
    manifest["files"] = len(files) + len(untracked)
    return diff, manifest


def diff_note(manifest: dict[str, Any], config: dict[str, Any]) -> str:
    """Tell the reviewer exactly what it is not being shown."""
    lines = []
    if config.get("exclude_noise", True):
        lines.append("Lockfiles, build output, binary assets and generated code are "
                     "excluded from this diff. Do not report on them.")
    for item in manifest.get("truncated_files", [])[:10]:
        lines.append(f"NOTE: {item['path']} was truncated; inspect it directly if relevant.")
    for item in manifest.get("skipped_files", [])[:10]:
        lines.append(f"NOTE: {item['path']} was not inlined; read it from the repository.")
    omitted = manifest.get("omitted_files") or []
    if omitted:
        lines.append(f"NOTE: {len(omitted)} new files exceeded the untracked-file cap and "
                     f"were listed by path only. Read them from the repository before "
                     f"concluding the change is complete.")
    if manifest.get("payload_truncated"):
        lines.append("NOTE: the diff exceeded the payload budget and was cut. "
                     "Use git to inspect anything missing.")
    return "\n".join(lines)


VALIDATION_PASSED = "passed"
VALIDATION_FAILED = "failed"
VALIDATION_NOT_CONFIGURED = "not_configured"


def run_validation(run: Run, label: str = "") -> tuple[str, list[dict[str, Any]]]:
    """Run the configured validation commands.

    Returns a tri-state, not a boolean. "No commands configured" is not the
    same fact as "the suite passed", and collapsing the two into True makes
    the independent gate approve a run that was never checked.
    """
    cmds = (run.config.get("validation") or {}).get("commands") or []
    if not cmds:
        return VALIDATION_NOT_CONFIGURED, []
    timeout = int(run.config.get("validation_timeout_seconds", 1800))
    results = []
    ok = True
    for cmd in cmds:
        run.emit("validation_start", command=cmd, stage=label or "round")
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
        run.emit("validation_done", command=cmd, passed=passed, stage=label or "round")
    return (VALIDATION_PASSED if ok else VALIDATION_FAILED), results


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

# Ordered stable-content-first so that the persona, instructions, schema and
# task form a cacheable prefix that does not change between rounds. Only the
# diff and validation results vary, and they come last.
REVIEW_PROMPT = """\
{persona}

Do NOT modify the repository. You have read-only access. Review only.

Review the implementation from scratch. Do not assume previous review rounds
were correct or complete, and look for regressions introduced by recent fixes.

Inspect the surrounding code in the repository rather than reviewing the diff
in isolation, but do not re-read files the diff already shows you in full.

{shape}

<task>
{task}
</task>

{diff_note}
{validation_note}

<diff>
{diff}
</diff>
"""

# Shared by the review prompt and the repair prompt. The repair runs as a fresh
# session with no memory of the original instructions, so it must carry the
# full shape with it rather than referring back to it.
OUTPUT_SHAPE = """\
Return ONLY a JSON object matching this shape, with no prose before or after it:

{
  "verdict": "approved" | "changes_requested",
  "findings": [
    {
      "id": "SHORT-001",
      "severity": "blocker" | "high" | "medium" | "low" | "nit",
      "file": "path/to/file.ts",
      "line": 74,
      "category": "correctness",
      "problem": "What is wrong.",
      "impact": "Why it matters.",
      "recommended_fix": "What to do instead."
    }
  ]
}

Rules:
- Return only actionable findings. Do not compliment the implementation.
- Do not raise optional stylistic preferences.
- Do not report `nit` findings. They are discarded unread.
- Report at most 10 findings, most severe first. If you have more, you are
  padding: keep the ones that would change what a reviewer does.
- One or two sentences per field. No preamble, no restating the code, no
  markdown inside the strings. `problem` states the defect, `impact` states
  the consequence, `recommended_fix` states the action.
- If you find nothing actionable, return "approved" with an empty findings
  list. A clean review is a real outcome.
- `line` may be null when a finding is not anchored to one line."""

REPAIR_PROMPT = """\
Your previous response could not be parsed as JSON.

Below is what you produced. Convert it faithfully into the required JSON
object. Do not re-review anything, do not add findings that are not already
present, and do not drop any.

{shape}

<previous_response>
{previous}
</previous_response>
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
    problems = validate_config(config)
    repo = Path(config.get("repo") or os.getcwd()).resolve()
    problems += preflight_repo(repo, config)
    if problems:
        print(json.dumps({"error": "invalid config", "problems": problems}, indent=2),
              file=sys.stderr)
        return 1
    state = repo / STATE_DIRNAME
    _ensure_state_ignored(state)

    # A `run_dir` in the config means `start` already reserved one and is
    # handing this process its lock; `lock_token` is the proof. Run directly
    # and we mint our own token, which no live lock can match.
    handed_over = bool(config.get("run_dir"))
    token = config.get("lock_token") or new_lock_token()

    held = acquire_lock(state, token, os.getpid(),
                        Path(config["run_dir"]) if handed_over else None,
                        adopt_only=handed_over)
    if held:
        print(json.dumps({"error": "run already active", "problems": [held]}, indent=2),
              file=sys.stderr)
        return 1

    run_dir = Path(config["run_dir"]) if handed_over else _new_run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    # The lock is keyed on the run directory from here on, so release can tell
    # our lock from a later run's.
    handoff_lock(state, token, os.getpid(), run_dir)
    try:
        return _run_loop(config, repo, state, run_dir)
    finally:
        release_lock(state, run_dir)


def _run_loop(config: dict[str, Any], repo: Path, state: Path, run_dir: Path) -> int:
    run = Run(repo, run_dir, config)

    (run_dir / "config.json").write_text(json.dumps(config, indent=2))
    (state / "task.md").write_text(config["task"])
    # `start` writes this too, but a foreground `run` is a first-class entry
    # point and `render`/`status` resolve through it — without this they would
    # report on whichever run was started last.
    (state / "current-run").write_text(str(run_dir))
    (run_dir / "pid").write_text(str(os.getpid()))

    task = config["task"]
    blocking = set(config.get("blocking_severities") or ["blocker", "high", "medium"])
    max_iter = int(config.get("max_iterations", 5))

    base_sha = base_commit(repo)
    if base_sha is None:
        run.emit("warning", message="Repository has no commits; reviewing the whole working tree.")
    run.emit("run_start", repo=str(repo), base_sha=base_sha,
             reviewers=[r["slot_id"] for r in config["reviewers"]],
             max_iterations=max_iter)

    # --- baseline: was validation already failing before we touched it? ----
    # Without this the loop cannot tell "the change broke the build" from "the
    # build was broken when we arrived", and every later result is ambiguous.
    baseline_status, baseline_results = run_validation(run, label="baseline")
    (run_dir / "validation-00.json").write_text(json.dumps(
        {"stage": "baseline", "status": baseline_status, "results": baseline_results}, indent=2))
    run.emit("baseline_validation", status=baseline_status,
             commands=len(baseline_results))
    if baseline_status == VALIDATION_FAILED:
        run.emit("warning", message="Validation was already failing before the task "
                                    "started; the independent gate cannot attribute a "
                                    "later failure to this change.")
        if config.get("require_clean_baseline"):
            final = _write_final(run, base_sha, "baseline_failed", Ledger(), 0,
                                 VALIDATION_FAILED, blocking, baseline_status)
            run.emit("run_complete", outcome="baseline_failed", rounds=0, findings=0,
                     blocking_open=0, validation_status=VALIDATION_FAILED,
                     final=str(final))
            return exit_code("baseline_failed")

    # --- initial implementation -------------------------------------------
    cmds = (config.get("validation") or {}).get("commands") or []
    gate_note = ("This change will be judged by these commands, which are run "
                 "independently of your own testing:\n"
                 + "\n".join(f"  {c}" for c in cmds)) if cmds else \
                ("No validation commands are configured, so run whatever tests "
                 "this project already has.")
    if baseline_status == VALIDATION_FAILED:
        gate_note += ("\n\nThese commands were ALREADY FAILING before you started. "
                      "Fix only what your task requires; say clearly in your summary "
                      "which failures pre-existed.\n"
                      + _validation_note(baseline_status, baseline_results))

    ok, text = invoke_agent(run, config["implementer"],
                            IMPLEMENTER_PROMPT.format(task=task, validation_note=gate_note),
                            readonly=False, label="Implementer",
                            log_name="iter00-implementer", attempts=2)
    if not ok:
        # Still write the report and emit run_complete: whoever is polling the
        # progress stream must never be left waiting for an event that is not
        # coming.
        run.emit("run_failed", stage="implement", error=text)
        final = _write_final(run, base_sha, "implementer_failed", Ledger(), 0,
                             VALIDATION_NOT_CONFIGURED, blocking, baseline_status)
        run.emit("run_complete", outcome="implementer_failed", rounds=0, findings=0,
                 blocking_open=0, validation_status=VALIDATION_NOT_CONFIGURED,
                 final=str(final), error=text[:400])
        return exit_code("implementer_failed")
    (run_dir / "implementer-00.md").write_text(text)

    outcome = "max_iterations_reached"
    ledger = Ledger()
    rounds_run = 0
    val_status = VALIDATION_NOT_CONFIGURED
    missing_reviewers: list[str] = []
    previous_diff = ""
    previous_signature: set[str] = set()

    for iteration in range(1, max_iter + 1):
        if run.should_stop():
            outcome = "stopped_by_user"
            break
        rounds_run = iteration

        run.emit("iteration_start", iteration=iteration)

        # --- independent gate: tests decide, not model consensus -----------
        val_status, val_results = run_validation(run)
        val_ok = val_status == VALIDATION_PASSED
        (run_dir / f"validation-{iteration:02d}.json").write_text(json.dumps(val_results, indent=2))

        diff, manifest = collect_diff(repo, base_sha, config)
        if not diff.strip():
            run.emit("warning", message="Diff is empty; implementer may not have changed anything.")
        run.emit("diff_ready", iteration=iteration, **{
            k: v for k, v in manifest.items() if k in ("chars", "approx_tokens", "files")})

        # The same payload goes to every reviewer, so its size is multiplied by
        # the size of the panel. Report it once so the cost is visible.
        if manifest.get("truncated_files") or manifest.get("skipped_files"):
            run.emit("diff_trimmed", iteration=iteration,
                     truncated=[t["path"] for t in manifest["truncated_files"]][:10],
                     skipped=[t["path"] for t in manifest["skipped_files"]][:10])

        # If a fix round changed nothing, re-reviewing an identical payload
        # would cost a full panel and return the same answer.
        if iteration > 1 and diff == previous_diff:
            run.emit("warning", message="Fix round produced no change to the diff; "
                                        "stopping rather than re-reviewing identical code.")
            outcome = "no_progress"
            break
        previous_diff = diff

        # Reviewers get the verdict; the implementer gets the stack traces.
        validation_note = _validation_note(val_status, val_results, brief=True,
                                           baseline=baseline_status)
        implementer_validation_note = _validation_note(val_status, val_results,
                                                       baseline=baseline_status)

        # --- reviewers, in parallel, read-only, no shared context ----------
        def do_review(rv: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
            prompt = REVIEW_PROMPT.format(
                persona=load_persona(rv["persona"]),
                task=task, diff=diff, validation_note=validation_note,
                diff_note=diff_note(manifest, config), shape=OUTPUT_SHAPE,
            )
            ok_, text_ = invoke_agent(
                run, rv, prompt, readonly=True,
                label=rv.get("label") or rv["slot_id"],
                log_name=f"iter{iteration:02d}-{rv['slot_id']}",
            )
            return rv, ok_, text_

        reviewers = config["reviewers"]
        workers = len(reviewers) if config.get("parallel", True) else 1
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(do_review, reviewers))

        round_findings: list[dict[str, Any]] = []
        missing_reviewers = []
        for rv, ok_, text_ in results:
            slot = rv["slot_id"]
            obj = extract_json(text_) if ok_ else None
            problem = ("agent failed" if not ok_ else
                       "unparseable output" if obj is None else review_problem(obj))
            if ok_ and problem:
                # One repair attempt, for a malformed shape as much as for
                # unparseable text. This is a fresh session, so the required
                # shape has to travel with the request.
                ok2, text2 = invoke_agent(
                    run, rv,
                    REPAIR_PROMPT.format(shape=OUTPUT_SHAPE, previous=text_[:8000]),
                    readonly=True, label=f"{rv.get('label') or slot} (repair)",
                    log_name=f"iter{iteration:02d}-{slot}-repair")
                repaired = extract_json(text2) if ok2 else None
                if repaired is not None and review_problem(repaired) is None:
                    obj, problem = repaired, None
            if problem:
                # This reviewer did not report, so the panel is smaller than it
                # looks. Never let that pass silently — and never as approval.
                missing_reviewers.append(slot)
                run.emit("review_unparsed", reviewer=slot,
                         label=rv.get("label") or slot, reason=problem)
            # Findings from a malformed review are still kept: they can only
            # make the gate stricter, and the slot is already counted missing.
            found = normalise_findings(slot, obj)
            (run_dir / f"review-{iteration:02d}-{slot}.json").write_text(
                json.dumps(obj if isinstance(obj, dict) else
                           {"verdict": "unparsed", "findings": []}, indent=2))
            counts = {s: sum(1 for x in found if x["severity"] == s) for s in SEVERITIES}
            run.emit("review_done", reviewer=slot,
                     label=rv.get("label") or slot,
                     verdict=(_verdict(obj) if isinstance(obj, dict) else "") or "unparsed",
                     usable=not problem,
                     counts={k: v for k, v in counts.items() if v})
            round_findings += found

        panel_complete = not missing_reviewers
        merged = dedupe(round_findings)
        (run_dir / f"findings-{iteration:02d}.json").write_text(json.dumps(merged, indent=2))
        ledger.record_round(iteration, merged, panel_complete=panel_complete)
        (run_dir / "ledger.json").write_text(json.dumps(ledger.to_json(), indent=2))

        blockers = [f for f in merged if f["severity"] in blocking]
        run.emit("round_summary", iteration=iteration, total=len(merged),
                 blocking=len(blockers), validation_status=val_status,
                 validation_passed=val_ok, panel_complete=panel_complete,
                 missing_reviewers=sorted(set(missing_reviewers)))

        # Approval needs all three: every reviewer reported, nothing blocking
        # is open, and the independent gate actually ran and passed. A missing
        # answer is not a clean one, and neither is a gate that never ran.
        if not blockers:
            if not panel_complete:
                run.emit("warning", message="No blocking findings, but "
                         f"{len(set(missing_reviewers))} reviewer(s) contributed nothing "
                         "this round. The panel is incomplete, so this is not an approval.")
                outcome = "review_incomplete"
                break
            if val_status == VALIDATION_NOT_CONFIGURED:
                if config.get("allow_missing_validation"):
                    run.emit("warning", message="Approving without an independent gate: "
                             "no validation commands were configured and "
                             "`allow_missing_validation` is set.")
                    outcome = "approved_unverified"
                else:
                    outcome = "validation_not_configured"
                break
            if val_ok:
                outcome = "approved"
                break
            run.emit("warning", message="Reviewers approved but validation is failing; "
                                        "sending validation failures back to the implementer.")

        if iteration == max_iter:
            outcome = "max_iterations_reached"
            break

        # If the panel returns the same blocking findings it returned last
        # round, the implementer is not converging. Another round costs a full
        # panel plus a fix and will almost certainly return the same answer.
        signature = {f"{f['file']}:{f['severity']}:{f['problem'][:80]}" for f in blockers}
        if signature and signature == previous_signature:
            run.emit("warning", message="The same blocking findings survived a fix round; "
                                        "stopping rather than looping on them.")
            outcome = "no_progress"
            break
        previous_signature = signature

        payload = json.dumps(blockers or merged, indent=2)
        if not val_ok:
            payload += "\n\nVALIDATION FAILURES:\n" + implementer_validation_note
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

    open_now = ledger.open_findings()
    final = _write_final(run, base_sha, outcome, ledger, rounds_run,
                         val_status, blocking, baseline_status,
                         sorted(set(missing_reviewers)))
    run.emit("run_complete", outcome=outcome, rounds=rounds_run,
             findings=len(ledger.entries), resolved=len(ledger.resolved()),
             blocking_open=sum(1 for f in open_now if f["severity"] in blocking),
             validation_status=val_status,
             validation_passed=val_status == VALIDATION_PASSED,
             missing_reviewers=sorted(set(missing_reviewers)),
             final=str(final))
    return exit_code(outcome)




def _validation_note(status: str, results: list[dict[str, Any]],
                     brief: bool = False, baseline: str | None = None) -> str:
    """Summarise validation.

    `brief` is for reviewers, who need to know whether the suite passes but
    cannot act on a stack trace — and who each pay for it separately, every
    round. The implementer gets the full output because it has to fix it.
    """
    if status == VALIDATION_NOT_CONFIGURED or not results:
        return ("No validation commands were configured for this run, so nothing "
                "was independently verified. Treat every claim about behaviour as "
                "unproven.")
    lines = ["Validation results (these are ground truth, not opinion):"]
    for r in results:
        lines.append(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['command']}")
        if not r["passed"] and not brief:
            lines.append(textwrap.indent(r["output_tail"][-1500:], "      "))
    if brief and status != VALIDATION_PASSED:
        lines.append("  (failure output withheld; the build agent has it.)")
    if baseline == VALIDATION_FAILED:
        lines.append("  NOTE: validation was ALREADY FAILING before this task started. "
                     "A failure here is not necessarily caused by the change under review.")
    return "\n".join(lines)


def _new_run_dir(state: Path) -> Path:
    """Reserve the next run directory by creating it.

    Returning a name without claiming it lets two callers pick the same one and
    then write over each other's config, progress stream and findings.
    """
    history = state / "history"
    history.mkdir(parents=True, exist_ok=True)
    n = len([d for d in history.iterdir() if d.is_dir()]) + 1
    while True:
        candidate = history / f"run-{n:02d}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            n += 1


# The exit code is the whole interface in CI, so it is a table rather than a
# condition at each return site — that is how a fix-round failure came to exit
# 2 while the identical failure before the first round exited 1.
#   0  approved
#   1  could not run or could not complete
#   2  ran to a conclusion that is not an approval
EXIT_CODES = {
    "approved": 0,
    "approved_unverified": 0,
    "implementer_failed": 1,
    "baseline_failed": 1,
}


def exit_code(outcome: str) -> int:
    return EXIT_CODES.get(outcome, 2)


OUTCOME_NOTE = {
    "approved": "Every reviewer reported, no blocking findings remain, and validation passed.",
    "approved_unverified": "No blocking findings remain, but no validation commands were "
                           "configured, so nothing was independently verified. This is a "
                           "model-consensus result only — `allow_missing_validation` was set.",
    "review_incomplete": "One or more reviewers contributed nothing, so the panel that "
                         "reported was smaller than the one that was configured. The "
                         "absence of findings from a reviewer that never answered is not "
                         "evidence of correctness. Re-run, or review those areas by hand.",
    "validation_not_configured": "No blocking findings remain, but no validation commands "
                                 "were configured, so there was no independent gate and the "
                                 "loop will not call this approved. Add validation commands "
                                 "and re-run.",
    "baseline_failed": "Validation was already failing before the task started, and "
                       "`require_clean_baseline` was set. Fix the build first, or the "
                       "gate cannot attribute anything to this change.",
    "max_iterations_reached": "The loop hit its iteration cap with blocking findings "
                              "still open. This needs a human — do not simply raise the cap.",
    "stopped_by_user": "Stopped on request. The working tree holds whatever the last "
                       "completed step produced.",
    "implementer_failed": "The build agent could not complete a fix round. See logs/.",
    "no_progress": "A fix round changed nothing the reviewers cared about, so the loop "
                   "stopped rather than spending another panel on the same answer. "
                   "The findings below need a human.",
}


VALIDATION_LABEL = {
    VALIDATION_PASSED: "passing",
    VALIDATION_FAILED: "FAILING",
    VALIDATION_NOT_CONFIGURED: "NOT CONFIGURED — nothing was independently verified",
}


def _write_final(run: Run, base_sha: str | None, outcome: str,
                 ledger: Ledger, rounds: int, validation_status: str,
                 blocking: set[str], baseline_status: str = VALIDATION_NOT_CONFIGURED,
                 missing_reviewers: list[str] | None = None) -> Path:
    open_findings = ledger.open_findings()
    resolved = ledger.resolved()
    blockers = [f for f in open_findings if f["severity"] in blocking]
    advisory = [f for f in open_findings if f["severity"] not in blocking]
    reappeared = [f for f in open_findings if f["state"] == "reappeared"]
    missing_reviewers = missing_reviewers or []

    lines = [
        "# Review loop result", "",
        f"**Outcome: {outcome}** — {OUTCOME_NOTE.get(outcome, '')}", "",
        f"- Review rounds: {rounds}",
        f"- Base SHA: `{base_sha or '(no commits)'}`",
        f"- Reviewers: {', '.join(r.get('label') or r.get('slot_id') or r.get('persona', '?') for r in run.config.get('reviewers', []))}",
        f"- Validation: {VALIDATION_LABEL.get(validation_status, validation_status)}",
        f"- Validation before the task started: "
        f"{VALIDATION_LABEL.get(baseline_status, baseline_status)}",
        f"- Findings raised across all rounds: {len(ledger.entries)}",
        f"- Findings resolved: {len(resolved)}",
        f"- Blocking findings open: {len(blockers)}",
        f"- Advisory findings open: {len(advisory)}", "",
    ]
    if missing_reviewers:
        lines += [f"> **Panel incomplete.** These reviewers contributed nothing in the "
                  f"final round: {', '.join(missing_reviewers)}. Their areas of the "
                  f"change were not reviewed.", ""]
    if reappeared:
        lines += [f"> **{len(reappeared)} finding(s) came back after being fixed.** "
                  f"A fix for them did not hold; they are marked below.", ""]
    if rounds > 1:
        lines += ["The implementer's response to each round — what it fixed and what it "
                  "rejected, with its stated evidence — is in `implementer-NN.md` in this "
                  "run directory.", ""]

    def section(title: str, items: list[dict[str, Any]], note: str) -> None:
        # Every mutation here must be a method call: an augmented assignment
        # (`lines += ...`) would rebind `lines` as a local and break the closure.
        if not items:
            return
        lines.extend([f"## {title}", "", note, ""])
        for f in items:
            corr = f.get("corroborated_by")
            tag = (f" · independently raised by {', '.join(corr)}") if corr else ""
            if f.get("state") == "reappeared":
                tag += " · CAME BACK after being fixed"
            elif f.get("state") == "resolved":
                tag += f" · resolved in round {f.get('resolved_in', '?')}"
            elif f.get("first_seen") and f["first_seen"] != f.get("last_seen"):
                tag += f" · open since round {f['first_seen']}"
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
    section("Findings raised and resolved", resolved,
            "Raised by the panel in an earlier round and no longer reported by a "
            "complete panel. Kept for the record — a later round can bring one back.")

    if not ledger.entries:
        lines += ["## Findings", "", "None raised in any round.", ""]
    elif not open_findings:
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
    # Validate before detaching: a config error must surface here, where the
    # caller can still see it, not in a log file nobody is watching.
    problems = validate_config(config)
    repo = Path(config.get("repo") or os.getcwd()).resolve()
    problems += preflight_repo(repo, config)
    if problems:
        print(json.dumps({"error": "invalid config", "problems": problems}, indent=2),
              file=sys.stderr)
        return 1
    state = repo / STATE_DIRNAME
    _ensure_state_ignored(state)

    # Claim the worktree before reserving a run directory or repointing
    # `current-run`, so a second `start` cannot detach a run that would fight
    # the first one. Our own pid holds the lock until the child adopts it.
    token = new_lock_token()
    held = acquire_lock(state, token, os.getpid())
    if held:
        print(json.dumps({"error": "run already active", "problems": [held]}, indent=2),
              file=sys.stderr)
        return 1

    run_dir = _new_run_dir(state)
    config["run_dir"] = str(run_dir)
    config["lock_token"] = token
    resolved = run_dir / "config.json"
    resolved.write_text(json.dumps(config, indent=2))

    log = (run_dir / "run.log").open("w")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--config", str(resolved)],
            cwd=str(repo), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        handoff_lock(state, token, os.getpid(), run_dir)
        release_lock(state, run_dir)
        raise
    # Name the child on the lock so the window between this process exiting and
    # the child adopting does not look like a crashed run to the next caller.
    handoff_lock(state, token, proc.pid, run_dir)
    (run_dir / "pid").write_text(str(proc.pid))
    (state / "current-run").write_text(str(run_dir))
    print(json.dumps({"run_dir": str(run_dir), "pid": proc.pid,
                      "progress": str(run_dir / "progress.jsonl")}, indent=2))
    return 0


def _ensure_state_ignored(state: Path) -> None:
    """Make the state directory invisible to git without touching the repo.

    Appending to the project's own `.gitignore` would leave an uncommitted
    change behind — which the next run's dirty-tree check would then refuse to
    start on, and which the user never asked for. A `.gitignore` holding `*`
    inside the directory ignores the directory's contents and itself, so
    `git status` stays clean and nothing outside `.review-loop/` is modified.
    """
    try:
        state.mkdir(exist_ok=True)
        marker = state / ".gitignore"
        if not marker.exists():
            marker.write_text("# review-loop working state; not part of the project\n*\n")
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


SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}


def _spend(events: list[dict[str, Any]]) -> str:
    """Total reported token use and cost across every agent invocation."""
    cost = sum(e.get("cost_usd", 0) or 0 for e in events if e.get("event") == "agent_done")
    fresh = sum(e.get("tok_in", 0) or 0 for e in events if e.get("event") == "agent_done")
    cached = sum(e.get("tok_cache_read", 0) or 0 for e in events if e.get("event") == "agent_done")
    outp = sum(e.get("tok_out", 0) or 0 for e in events if e.get("event") == "agent_done")
    if not (cost or fresh or cached or outp):
        return ""
    bits = []
    if fresh or cached:
        bits.append(f"{fresh + cached:,} in ({cached:,} cached)")
    if outp:
        bits.append(f"{outp:,} out")
    if cost:
        bits.append(f"${cost:.2f}")
    return "spend: " + " · ".join(bits)


def cmd_render(args: argparse.Namespace) -> int:
    """Turn the event stream into the progress tree.

    The script owns this so every host renders it identically and nobody has
    to spend tokens reformatting JSON.
    """
    run_dir = _resolve_run(args)
    if not run_dir or not (run_dir / "progress.jsonl").exists():
        print("No review-loop run found here.")
        return 1

    events = []
    for line in (run_dir / "progress.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except Exception:
            continue

    out: list[str] = []
    agents: dict[str, dict[str, Any]] = {}
    round_open = False
    panel_size = 1

    def plural(n: Any, word: str) -> str:
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    def sev_summary(counts: dict[str, int]) -> str:
        parts = [f"{n} {s}" for s, n in
                 sorted(counts.items(), key=lambda kv: SEV_ORDER.get(kv[0], 9)) if n]
        return ", ".join(parts) if parts else "approved — no findings"

    for e in events:
        ev = e.get("event")
        if ev == "run_start":
            panel_size = max(1, len(e.get("reviewers") or []))
            revs = ", ".join(e.get("reviewers") or [])
            out.append(f"review-loop · {plural(len(e.get('reviewers') or []), 'reviewer')} "
                       f"· max {plural(e.get('max_iterations'), 'round')}")
            out.append(f"  panel: {revs}")
            out.append("")
        elif ev == "agent_start":
            agents[e["label"]] = e
            if not e.get("readonly"):
                model = " ".join(x for x in (e.get("cli"), e.get("model")) if x)
                eff = f" @ {e['effort']}" if e.get("effort") else ""
                out.append(f"● {e['label']} — {model}{eff}")
                out.append("  ▸ running…")
        elif ev == "agent_done":
            if not agents.get(e["label"], {}).get("readonly"):
                if out and out[-1].strip() == "▸ running…":
                    out.pop()
                out.append(f"  ✓ complete ({e.get('seconds')}s)")
        elif ev == "agent_error":
            out.append(f"  ✗ {e['label']} failed: {str(e.get('error'))[:120]}")
        elif ev == "permission_denied":
            out.append(f"  ⚠ {e['label']} was blocked from running commands "
                       f"({e.get('count')} denials) — its result is unverified")
        elif ev == "validation_done":
            stage = " (baseline)" if e.get("stage") == "baseline" else ""
            out.append(f"  {'✓' if e.get('passed') else '✗'} validation{stage}: "
                       f"{e.get('command')}")
        elif ev == "baseline_validation":
            if e.get("status") == VALIDATION_NOT_CONFIGURED:
                out.append("  ⚠ no validation commands configured — "
                           "there is no independent gate on this run")
            elif e.get("status") == VALIDATION_FAILED:
                out.append("  ⚠ validation was already failing before the task started")
        elif ev == "iteration_start":
            out.append("")
            out.append(f"● Review round {e['iteration']}")
            round_open = True
        elif ev == "review_done":
            counts = e.get("counts") or {}
            out.append(f"  ├─ {e.get('label')}")
            out.append(f"  │  {sev_summary(counts)}")
        elif ev == "review_unparsed":
            out.append(f"  ├─ {e.get('label')}")
            out.append(f"  │  ⚠ no usable output ({e.get('reason')}) — "
                       f"this reviewer contributed nothing")
        elif ev == "round_summary":
            if round_open:
                # Turn the last branch into the closing one.
                for i in range(len(out) - 1, -1, -1):
                    if out[i].startswith("  ├─"):
                        out[i] = "  └─" + out[i][4:]
                        break
                for i in range(len(out) - 1, -1, -1):
                    if out[i].startswith("  │"):
                        out[i] = "     " + out[i][5:]
                        break
            line = (f"  → {plural(e.get('total'), 'finding')} after merge · "
                    f"{e.get('blocking')} blocking · validation "
                    f"{VALIDATION_LABEL.get(e.get('validation_status'), 'FAILING' if not e.get('validation_passed') else 'passing')}")
            if e.get("panel_complete") is False:
                line += f" · PANEL INCOMPLETE ({', '.join(e.get('missing_reviewers') or [])})"
            out.append(line)
            round_open = False
        elif ev == "warning":
            out.append(f"  ⚠ {e.get('message')}")
        elif ev == "diff_ready":
            tok = e.get("approx_tokens") or 0
            line = f"  · diff: {e.get('files')} files, ~{tok:,} tokens"
            if panel_size > 1:
                # The payload is sent to every reviewer, so this is the number
                # that actually determines the round's cost.
                line += f" × {panel_size} reviewers ≈ {tok * panel_size:,}/round"
            out.append(line)
        elif ev == "diff_trimmed":
            for p in (e.get("truncated") or [])[:3]:
                out.append(f"  · trimmed {p}")
            for p in (e.get("skipped") or [])[:3]:
                out.append(f"  · not inlined {p}")
        elif ev == "run_complete":
            out.append("")
            out.append("━" * 52)
            mark = "✓" if e.get("outcome") == "approved" else "■"
            out.append(f"{mark} {str(e.get('outcome', '')).replace('_', ' ').upper()}")
            out.append(f"  {plural(e.get('rounds'), 'round')} · "
                       f"{plural(e.get('findings'), 'finding')} raised · "
                       f"{e.get('resolved', 0)} resolved · "
                       f"{e.get('blocking_open')} blocking open · "
                       f"validation "
                       f"{VALIDATION_LABEL.get(e.get('validation_status'), 'FAILING' if not e.get('validation_passed') else 'passing')}")
            if e.get("missing_reviewers"):
                out.append(f"  ⚠ panel incomplete: "
                           f"{', '.join(e['missing_reviewers'])} contributed nothing")
            spend = _spend(events)
            if spend:
                out.append(f"  {spend}")
            out.append(f"  report: {e.get('final')}")
            out.append("━" * 52)

    if not any(e.get("event") == "run_complete" for e in events):
        out.append("")
        out.append("  ▸ still running — re-run `render` for an update")

    print("\n".join(out))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    run_dir = _resolve_run(args)
    if not run_dir:
        print(json.dumps({"error": "no run found"}))
        return 1
    (run_dir / "STOP").write_text("stop")
    if args.kill:
        pid = _locked_run_pid(run_dir)
        if pid is None or not _pid_is_review_loop(pid, run_dir):
            print(json.dumps({
                "error": "refusing to signal an unverified process",
                "run_dir": str(run_dir),
                "hint": "The run is not actively locked by a matching review-loop process."
            }), file=sys.stderr)
            return 1
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception as exc:
            print(json.dumps({"error": "could not stop run", "detail": str(exc)}),
                  file=sys.stderr)
            return 1
        # Do not release here: SIGTERM is asynchronous and can fail. Keeping the
        # lock until the pid is actually dead prevents a quick restart from
        # overlapping the process being killed. The next acquisition safely
        # reclaims locks whose recorded pid no longer exists.
    print(json.dumps({"stopping": str(run_dir)}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="review_loop")
    p.add_argument("--version", action="version", version=f"%(prog)s {installed_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect").set_defaults(func=cmd_detect)

    sg = sub.add_parser("suggest", help="recommend a reviewer panel for a task")
    sg.add_argument("--task", default=""); sg.add_argument("--repo")
    sg.set_defaults(func=cmd_suggest)

    s = sub.add_parser("start"); s.add_argument("--config", required=True)
    s.set_defaults(func=cmd_start)

    r = sub.add_parser("run"); r.add_argument("--config", required=True)
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status")
    st.add_argument("--run"); st.add_argument("--repo")
    st.add_argument("--tail", type=int, default=40)
    st.set_defaults(func=cmd_status)

    rn = sub.add_parser("render", help="print the progress tree for a run")
    rn.add_argument("--run"); rn.add_argument("--repo")
    rn.set_defaults(func=cmd_render)

    sp = sub.add_parser("stop")
    sp.add_argument("--run"); sp.add_argument("--repo")
    sp.add_argument("--kill", action="store_true")
    sp.set_defaults(func=cmd_stop)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
