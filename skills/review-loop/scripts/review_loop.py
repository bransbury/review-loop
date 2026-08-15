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
import ctypes
import fcntl
import hashlib
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
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
PERSONA_DIR = SKILL_DIR / "personas"
SCHEMA_PATH = SKILL_DIR / "schemas" / "review.json"

SEVERITIES = ["blocker", "high", "medium", "low", "nit"]
STATE_DIRNAME = ".review-loop"
PERMISSION_MODES = ["acceptEdits", "bypassPermissions"]
MAX_REVIEWERS = 8
MAX_VALIDATION_COMMANDS = 32
MAX_AGENT_WORKERS = 4
MAX_TASK_CHARS = 100_000
MAX_COMMAND_CHARS = 16_384
MAX_GIT_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_FINGERPRINT_FILES = 100_000
MAX_FINGERPRINT_FILE_BYTES = 512 * 1024 * 1024
STATE_OWNER = "review-loop managed state v1"


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
COPILOT_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]

# Fallbacks only. `detect` prefers live enumeration where the CLI allows it,
# because model availability varies by plan and by org policy.
CLAUDE_MODELS = ["opus", "fable", "sonnet", "haiku"]
CODEX_MODELS_FALLBACK = ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5", "gpt-5.4"]
COPILOT_MODELS_FALLBACK = ["auto"]


@lru_cache(maxsize=8)
def _claude_supports_json_schema(executable: str) -> bool:
    """Probe the installed CLI instead of assuming every version has the flag."""
    try:
        result = subprocess.run([executable, "--help"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "--json-schema" in result.stdout


@lru_cache(maxsize=8)
def _claude_supports_safe_mode(executable: str) -> bool:
    try:
        result = subprocess.run([executable, "--help"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "--safe-mode" in result.stdout


def _claude_argv(prompt: str, model: str, effort: str, readonly: bool,
                 permission_mode: str, out_file: Path) -> tuple[list[str], Optional[str]]:
    # Prompt goes on stdin: a large diff would otherwise risk ARG_MAX.
    argv = ["claude", "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if readonly:
        # Belt and braces: plan mode cannot write, and the tools are denied too.
        executable = shutil.which("claude") or "claude"
        argv += ["--permission-mode", "plan",
                 "--disallowed-tools", "Edit", "Write", "NotebookEdit"]
        if _claude_supports_safe_mode(executable):
            argv += ["--safe-mode"]
        else:
            # Safe fallback used by older releases that predate --safe-mode.
            argv += ["--setting-sources", ""]
        if SCHEMA_PATH.exists() and _claude_supports_json_schema(executable):
            # Claude accepts the schema inline, unlike Codex's file-path flag.
            # Older versions safely fall back to prompt shaping plus parser
            # validation because capability detection simply omits this flag.
            schema = json.dumps(json.loads(SCHEMA_PATH.read_text()), separators=(",", ":"))
            argv += ["--json-schema", schema]
    else:
        argv += ["--permission-mode", permission_mode]
    return argv, prompt


def _copilot_argv(prompt: str, model: str, effort: str, readonly: bool,
                  permission_mode: str, out_file: Path) -> tuple[list[str], Optional[str]]:
    # Copilot has no stdin prompt mode, so the text goes in argv. ARG_MAX is
    # 1MB on macOS and 2MB on Linux; the diff cap keeps us well clear.
    argv = ["copilot", "--prompt", prompt,
            "--no-ask-user", "--silent", "--no-color"]
    if model and model != "auto":
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if readonly:
        # `write` deliberately does not cover the shell tool — Copilot's own
        # permissions help says so — and denials override --allow-all-tools.
        # Denying specific commands is therefore useless: `sed -i`, `rm` and a
        # shell redirection all still mutate the tree. Deny the shell outright
        # and leave the reviewer its read-only tools. Do not blanket-approve
        # every tool: that would also approve side-effecting MCP calls.
        argv += ["--available-tools=view,grep,glob", "--disable-builtin-mcps",
                 "--no-custom-instructions", "--deny-tool", "write",
                 "--deny-tool", "shell"]
    elif permission_mode == "acceptEdits":
        # Copilot has no acceptEdits mode. Its closest safe equivalent is to
        # approve its local write permission while denying the shell outright,
        # without blanket-approving MCP tools. The shell denial
        # matters because Copilot's `write` permission does not cover shell
        # redirections or commands such as `sed -i`.
        argv += ["--allow-tool", "write", "--deny-tool", "shell"]
    elif permission_mode == "bypassPermissions":
        # `--allow-all` includes tools, paths and URLs. This is deliberately
        # broader than --allow-all-tools and matches the advertised bypass.
        argv += ["--allow-all"]
    return argv, None


def _codex_argv(prompt: str, model: str, effort: str, readonly: bool,
                permission_mode: str, out_file: Path) -> tuple[list[str], Optional[str]]:
    # `-` makes codex exec read the prompt from stdin.
    argv = ["codex", "exec", "--skip-git-repo-check", "-o", str(out_file)]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    if readonly:
        argv += ["-s", "read-only"]
        # Codex accepts the schema by path; current Claude versions take it
        # inline, while older Claude and Copilot use validated parser fallback.
        if SCHEMA_PATH.exists():
            argv += ["--output-schema", str(SCHEMA_PATH)]
    elif permission_mode == "acceptEdits":
        # Codex has sandbox/approval policies rather than Claude-style modes.
        # workspace-write is its bounded, unattended editing contract.
        argv += ["-s", "workspace-write"]
    elif permission_mode == "bypassPermissions":
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    argv += ["-"]
    return argv, prompt


ADAPTERS: dict[str, dict[str, Any]] = {
    "claude": {
        "bin": "claude",
        "argv": _claude_argv,
        "efforts": CLAUDE_EFFORTS,
        "default_effort": "high",
        "default_model": "opus",
        "reviewer_default_effort": "low",
        "reviewer_default_model": "opus",
        "reads_out_file": False,
        "config_dir_env": "CLAUDE_CONFIG_DIR",
    },
    "copilot": {
        "bin": "copilot",
        "argv": _copilot_argv,
        "efforts": COPILOT_EFFORTS,
        "default_effort": "medium",
        "default_model": "gpt-5.6-sol",
        "reviewer_default_effort": "xhigh",
        "reviewer_default_model": "gpt-5.6-luna",
        "unavailable_model_fallback": "auto",
        "reads_out_file": False,
        "config_dir_env": None,
    },
    "codex": {
        "bin": "codex",
        "argv": _codex_argv,
        "efforts": CODEX_EFFORTS,
        "default_effort": "medium",
        "default_model": "gpt-5.6-sol",
        "reviewer_default_effort": "xhigh",
        "reviewer_default_model": "gpt-5.6-luna",
        "reads_out_file": True,
        "config_dir_env": "CODEX_HOME",
    },
}


def _available_models(cli: str) -> list[str]:
    if cli == "claude":
        return CLAUDE_MODELS
    if cli == "codex":
        return _codex_models()
    if cli == "copilot":
        return _copilot_models()
    return []


def _role_defaults(cli: str, readonly: bool) -> tuple[str, str]:
    """Resolve role-specific defaults without selecting an unavailable model."""
    ad = ADAPTERS[cli]
    model_key = "reviewer_default_model" if readonly else "default_model"
    effort_key = "reviewer_default_effort" if readonly else "default_effort"
    preferred_model = ad[model_key]
    models = _available_models(cli)
    if preferred_model not in models:
        preferred_model = ad.get("unavailable_model_fallback") or (models[0] if models else preferred_model)
    return preferred_model, ad[effort_key]


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
        implementer_model, implementer_effort = _role_defaults(name, False)
        reviewer_model, reviewer_effort = _role_defaults(name, True)
        entry = {
            "path": path,
            "efforts": ad["efforts"],
            # Keep the original fields as implementer-default aliases for
            # hosts that have not yet learned the role-specific shape.
            "default_model": implementer_model,
            "default_effort": implementer_effort,
            "implementer_default": {
                "model": implementer_model,
                "effort": implementer_effort,
            },
            "reviewer_default": {
                "model": reviewer_model,
                "effort": reviewer_effort,
            },
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
    configured = Path(args.repo or os.getcwd())
    try:
        repo = canonical_worktree_root(configured)
        paths = [p.lower() for p in git(repo, "ls-files").splitlines()][:5000]
    except RuntimeError:
        # Suggestion is read-only and precedes the wizard's allow_non_git
        # choice. A directory with no Git metadata has no repository signals,
        # but task-keyword routing remains useful. Existing broken Git metadata
        # is still an error rather than silently treated as non-Git.
        resolved = configured.expanduser().resolve()
        if _has_git_marker(resolved):
            raise
        repo = resolved
        paths = []
    task = (args.task or "").lower()

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


def _invoking_harness() -> Optional[str]:
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
    if not isinstance(config, dict):
        return ["run config must be a JSON object."]
    errors: list[str] = []

    numeric_fields = {
        "max_iterations": (1, 20, 5),
        "agent_timeout_seconds": (1, 86_400, 3600),
        "validation_timeout_seconds": (1, 86_400, 1800),
        "max_log_chars": (4, 2_000_000, 100_000),
        "max_file_chars": (1, 1_000_000, 20_000),
        "max_untracked_files": (0, 1_000, 60),
        "max_diff_chars": (1, 2_000_000, 120_000),
    }
    for field, (minimum, maximum, default) in numeric_fields.items():
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"`{field}` must be an integer.")
        elif value < minimum:
            errors.append(f"`{field}` must be at least {minimum}.")
        elif value > maximum:
            errors.append(f"`{field}` must be at most {maximum}.")

    for field in ("allow_dirty", "allow_non_git", "allow_missing_validation",
                  "require_clean_baseline", "parallel", "exclude_noise"):
        if field in config and not isinstance(config[field], bool):
            errors.append(f"`{field}` must be true or false.")

    if "repo" in config and (not isinstance(config["repo"], str)
                             or not config["repo"].strip()):
        errors.append("`repo` must be a non-empty path string.")
    exclude_paths = config.get("exclude_paths", [])
    if not isinstance(exclude_paths, list) or any(
            not isinstance(path, str) or not path.strip() for path in exclude_paths):
        errors.append("`exclude_paths` must be a list of non-empty path strings.")

    permission_mode = config.get("permission_mode", "acceptEdits")
    if permission_mode not in PERMISSION_MODES:
        errors.append(f"permission_mode must be one of {PERMISSION_MODES}, "
                      f"got {permission_mode!r}.")

    if not isinstance(config.get("task"), str) or not config["task"].strip():
        errors.append("`task` is required and cannot be empty.")
    elif len(config["task"]) > MAX_TASK_CHARS:
        errors.append(f"`task` must be at most {MAX_TASK_CHARS} characters.")

    impl = config.get("implementer")
    if not isinstance(impl, dict):
        errors.append("`implementer` is required.")
    elif not isinstance(impl.get("cli"), str) or impl.get("cli") not in ADAPTERS:
        errors.append(f"implementer.cli must be one of {sorted(ADAPTERS)}, "
                      f"got {impl.get('cli')!r}.")

    reviewers = config.get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        errors.append("At least one reviewer is required.")
        reviewers = []
    elif len(reviewers) > MAX_REVIEWERS:
        errors.append(f"At most {MAX_REVIEWERS} reviewers may be configured.")

    seen: dict[str, int] = {}
    for i, rv in enumerate(reviewers):
        if not isinstance(rv, dict):
            errors.append(f"reviewers[{i}] must be an object.")
            continue
        persona = rv.get("persona")
        if not persona:
            errors.append(f"reviewers[{i}].persona is required.")
            continue
        if not isinstance(persona, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", persona):
            errors.append(f"reviewers[{i}].persona must contain only lowercase letters, "
                          "digits, and hyphens.")
            continue
        if not isinstance(rv.get("cli"), str) or rv.get("cli") not in ADAPTERS:
            errors.append(f"reviewers[{i}].cli must be one of {sorted(ADAPTERS)}, "
                          f"got {rv.get('cli')!r}.")
        for field in ("label", "label_base"):
            if field in rv and (not isinstance(rv[field], str) or not rv[field].strip()):
                errors.append(f"reviewers[{i}].{field} must be a non-empty string.")
        # The same persona may legitimately appear twice on different models.
        # Give each slot a unique id so their outputs, log files and finding
        # ids stay distinct — and so they can corroborate each other.
        seen[persona] = seen.get(persona, 0) + 1
        rv["slot_id"] = persona if seen[persona] == 1 else f"{persona}-{seen[persona]}"
        # Derived fields are rebuilt from `label_base`, never from the last
        # result. `start` validates a config and then the detached `run`
        # validates the file it wrote, so anything appended in place would be
        # appended twice — "Security Engineer #2 #2".
        if not isinstance(rv.get("label_base"), str) or not rv["label_base"].strip():
            # Prefer the persona's own name ("Adversarial QA") over title-casing
            # the slug, which mangles acronyms into "Adversarial Qa".
            meta = _persona_meta(PERSONA_DIR / f"{persona}.md") \
                if (PERSONA_DIR / f"{persona}.md").exists() else {}
            supplied_label = rv.get("label") if isinstance(rv.get("label"), str) else None
            rv["label_base"] = (supplied_label
                                or meta.get("name")
                                or persona.replace("-", " ").title())
        rv["label"] = rv["label_base"] if seen[persona] == 1 \
            else f"{rv['label_base']} #{seen[persona]}"

    for slot in ([impl] if isinstance(impl, dict) else []) + \
                [r for r in reviewers if isinstance(r, dict)]:
        cli = slot.get("cli")
        ad = ADAPTERS.get(cli) if isinstance(cli, str) else None
        if ad and not shutil.which(ad["bin"]):
            errors.append(f"`{ad['bin']}` is configured but not on PATH.")
        effort = slot.get("effort")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            errors.append(f"{cli or 'agent'}.effort must be a non-empty string.")
        if ad and effort and effort not in ad["efforts"]:
            errors.append(f"effort {effort!r} is not supported by {cli}; "
                          f"choose from {ad['efforts']}.")
        for field in ("model", "config_dir"):
            if field in slot and (not isinstance(slot[field], str)
                                  or not slot[field].strip()):
                errors.append(f"{cli or 'agent'}.{field} must be a non-empty string.")

    all_slots = ([impl] if isinstance(impl, dict) else []) + \
        [r for r in reviewers if isinstance(r, dict)]
    configured_diff_cap = config.get("max_diff_chars", 120_000)
    if any(slot.get("cli") == "copilot" for slot in all_slots) \
            and isinstance(configured_diff_cap, int) \
            and not isinstance(configured_diff_cap, bool) \
            and configured_diff_cap > 400_000:
        errors.append("`max_diff_chars` must be at most 400000 when Copilot is "
                      "configured because its prompt is passed in argv on macOS.")

    for field in ("run_dir", "lock_token"):
        if field in config and (not isinstance(config[field], str)
                                or not config[field].strip()):
            errors.append(f"`{field}` must be a non-empty string when present.")
    if ("run_dir" in config) != ("lock_token" in config):
        errors.append("`run_dir` and `lock_token` are internal handoff fields and "
                      "must either both be present or both be absent.")

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
            if len(commands) > MAX_VALIDATION_COMMANDS:
                errors.append(f"At most {MAX_VALIDATION_COMMANDS} validation commands "
                              "may be configured.")
                validation_shape_ok = False
            bad_commands = [i for i, cmd in enumerate(commands)
                            if not isinstance(cmd, str) or not cmd.strip()
                            or len(cmd) > MAX_COMMAND_CHARS]
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

    raw_blocking = config.get("blocking_severities", ["blocker", "high", "medium"])
    if not isinstance(raw_blocking, list) or any(
            not isinstance(severity, str) for severity in raw_blocking):
        errors.append("`blocking_severities` must be a list of severity strings.")
    else:
        bad = set(raw_blocking) - set(SEVERITIES)
        if bad:
            errors.append(f"unknown blocking severities: {sorted(bad)}")
        accepted = [s for s in SEVERITIES[:-1] if s in raw_blocking]
        if "nit" in raw_blocking or not accepted:
            errors.append("`blocking_severities` must contain `blocker` and cannot "
                          "contain the discarded `nit` severity.")
        elif accepted != SEVERITIES[:len(accepted)]:
            errors.append("`blocking_severities` must be an upward-closed threshold "
                          "starting with `blocker` (for example blocker/high/medium).")

    return errors


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

def _safe_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically write a regular file without following a destination symlink."""
    parent_mode = path.parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise RuntimeError(f"unsafe state parent: {path.parent}")
    if os.path.lexists(str(path)):
        current = path.lstat().st_mode
        if stat.S_ISLNK(current) or not stat.S_ISREG(current):
            raise RuntimeError(f"refusing unsafe state file: {path}")
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(temp), flags, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

class Run:
    def __init__(self, repo: Path, run_dir: Path, config: dict[str, Any]):
        self.repo = repo
        self.dir = run_dir
        self.config = config
        self.progress = run_dir / "progress.jsonl"
        self.stop_file = run_dir / "STOP"
        self.snapshot: dict[str, Any] = {
            "base_sha": None, "rounds": 0,
            "validation_status": VALIDATION_NOT_CONFIGURED,
            "baseline_status": VALIDATION_NOT_CONFIGURED,
            "ledger": Ledger(), "stage": "launch",
            "missing_reviewers": [],
        }
        # Reviewers run concurrently and all emit here; without this the
        # progress stream can interleave into unparseable lines.
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        line = json.dumps(rec)
        with self._lock:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(self.progress), flags, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            # Also echo for foreground/log tailing.
            try:
                print(line, flush=True)
            except (BrokenPipeError, OSError):
                # progress.jsonl is the durable contract. A closed foreground
                # pipe must not prevent terminal reporting into that file.
                pass

    def should_stop(self) -> bool:
        return self.stop_file.exists()


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

def _marked_processes(marker: str,
                      variable: str = "REVIEW_LOOP_PROCESS_TREE") -> set[int]:
    """Find descendants by an inherited marker, even after setsid/reparenting."""
    needle = f"{variable}={marker}".encode()
    found: set[int] = set()
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").glob("[0-9]*/environ"):
            try:
                if needle + b"\0" in entry.read_bytes():
                    found.add(int(entry.parent.name))
            except (OSError, ValueError):
                continue
        return found
    if sys.platform == "darwin":
        # `ps` can be unavailable in a sandbox even for our own children.
        # libproc enumerates pids and KERN_PROCARGS2 exposes the environment of
        # same-user processes without relying on GNU-only /proc.
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            count = max(1, int(libproc.proc_listallpids(None, 0)))
            pids = (ctypes.c_int * (count * 2))()
            size = int(libproc.proc_listallpids(pids, ctypes.sizeof(pids)))
            libc = ctypes.CDLL(None, use_errno=True)
            for pid in pids[:size]:
                if pid <= 0:
                    continue
                mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN,KERN_PROCARGS2
                length = ctypes.c_size_t(0)
                if libc.sysctl(mib, 3, None, ctypes.byref(length), None, 0) != 0 \
                        or length.value == 0:
                    continue
                data = ctypes.create_string_buffer(length.value)
                if libc.sysctl(mib, 3, data, ctypes.byref(length), None, 0) == 0 \
                        and needle + b"\0" in data.raw[:length.value]:
                    found.add(pid)
        except (AttributeError, OSError, ValueError):
            pass
    return found


def _signal_process_tree(group_pid: int, sig: int, marker: str) -> None:
    """Signal the original group plus descendants that escaped that group."""
    targets = _marked_processes(marker)
    try:
        os.killpg(group_pid, sig)
    except OSError:
        pass
    # The environment marker is checked in the live process immediately before
    # this snapshot. Do not retain bare historical PIDs: a short-lived helper's
    # PID can be reused by an unrelated process during an hour-long agent run.
    for pid in sorted(targets, reverse=True):
        if pid in (os.getpid(), group_pid):
            continue
        if pid not in _marked_processes(marker):
            continue
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _process_snapshot() -> dict[int, tuple[int, str]]:
    """Return pid -> (ppid, stable start identity) without child cooperation."""
    out: dict[int, tuple[int, str]] = {}
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").glob("[0-9]*/stat"):
            try:
                raw = entry.read_text()
                close = raw.rfind(")")
                fields = raw[close + 2:].split()
                out[int(entry.parent.name)] = (int(fields[1]), fields[19])
            except (OSError, ValueError, IndexError):
                continue
        return out
    if sys.platform == "darwin":
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            count = max(1, int(libproc.proc_listallpids(None, 0)))
            pids = (ctypes.c_int * (count * 2))()
            size = int(libproc.proc_listallpids(pids, ctypes.sizeof(pids)))
            for pid in pids[:size]:
                if pid <= 0:
                    continue
                info = ProcBsdInfo()
                got = int(libproc.proc_pidinfo(
                    pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)))
                if got == ctypes.sizeof(info):
                    identity = f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
                    out[pid] = (int(info.pbi_ppid), identity)
        except (AttributeError, OSError, ValueError):
            pass
        return out
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="],
                                capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3:
                out[int(fields[0])] = (int(fields[1]), fields[2])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return out


_TOP_LEVEL_PIDS: set[int] = set()
_TOP_LEVEL_LOCK = threading.Lock()
_PROCESS_REGISTRY_LOCK = threading.Lock()


class _ProcessTracker:
    """Continuously record descendants by kernel ancestry and start identity."""

    def __init__(self, root_pid: int, registry: Optional[Path] = None):
        self.root_pid = root_pid
        self.registry = registry
        self.persisted: set[tuple[int, str]] = set()
        self.identities: dict[int, str] = {}
        self.stop_event = threading.Event()
        snap = _process_snapshot()
        if root_pid in snap:
            self.identities[root_pid] = snap[root_pid][1]
        with _TOP_LEVEL_LOCK:
            _TOP_LEVEL_PIDS.add(root_pid)
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self._persist()
        self.thread.start()

    def _persist(self) -> None:
        if self.registry is None:
            return
        pending = {(pid, identity) for pid, identity in self.identities.items()} \
            - self.persisted
        if not pending:
            return
        with _PROCESS_REGISTRY_LOCK:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(self.registry), flags, 0o600)
            with os.fdopen(fd, "a") as handle:
                for pid, identity in sorted(pending):
                    handle.write(json.dumps({"pid": pid, "identity": identity}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        self.persisted.update(pending)

    def _scan(self) -> None:
        snap = _process_snapshot()
        known = {pid for pid, identity in self.identities.items()
                 if snap.get(pid, (None, None))[1] == identity}
        changed = True
        while changed:
            changed = False
            with _TOP_LEVEL_LOCK:
                top = set(_TOP_LEVEL_PIDS)
            for pid, (ppid, identity) in snap.items():
                if pid in known or pid in top:
                    continue
                if ppid in known:
                    self.identities[pid] = identity
                    known.add(pid)
                    changed = True
        self._persist()

    def _watch(self) -> None:
        while not self.stop_event.wait(0.01):
            self._scan()

    def live(self) -> set[int]:
        self._scan()
        snap = _process_snapshot()
        return {pid for pid, identity in self.identities.items()
                if snap.get(pid, (None, None))[1] == identity}

    def signal(self, sig: int) -> None:
        self._scan()
        for pid in sorted(self.live(), reverse=True):
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        try:
            os.killpg(self.root_pid, sig)
        except OSError:
            pass

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=0.5)
        with _TOP_LEVEL_LOCK:
            _TOP_LEVEL_PIDS.discard(self.root_pid)


class _BoundedBytes:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.exceeded = threading.Event()
        self.lock = threading.Lock()

    def add(self, data: bytes) -> None:
        with self.lock:
            self.total += len(data)
            if self.total > self.limit:
                self.exceeded.set()
            half = max(1, self.limit // 2)
            if len(self.head) < half:
                take = min(half - len(self.head), len(data))
                self.head.extend(data[:take])
                data = data[take:]
            if data:
                self.tail.extend(data)
                if len(self.tail) > self.limit - len(self.head):
                    del self.tail[:len(self.tail) - (self.limit - len(self.head))]

    def text(self, errors: str = "replace") -> str:
        with self.lock:
            raw = bytes(self.head + self.tail)
            omitted = max(0, self.total - len(raw))
        text = raw.decode("utf-8", errors=errors)
        if omitted:
            split = len(self.head.decode("utf-8", errors=errors))
            text = text[:split] + f"\n… [{omitted} bytes elided] …\n" + text[split:]
        return text


def _terminate_marked_processes(run_dir: Path, grace: float = 2.0) -> set[int]:
    """Stop every live command tree belonging to a run, including new sessions."""
    marker = str(run_dir.resolve())
    variable = "REVIEW_LOOP_RUN_DIR"

    def registered_live() -> set[int]:
        identities: dict[int, str] = {}
        registry = run_dir / "processes.jsonl"
        try:
            if stat.S_ISLNK(registry.lstat().st_mode):
                return set()
            for line in registry.read_text().splitlines():
                record = json.loads(line)
                if isinstance(record.get("pid"), int) and isinstance(
                        record.get("identity"), str):
                    identities[record["pid"]] = record["identity"]
        except (OSError, ValueError, AttributeError):
            pass
        snap = _process_snapshot()
        known = {pid for pid, identity in identities.items()
                 if snap.get(pid, (None, None))[1] == identity}
        # Capture live descendants before signaling a registered parent.
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _identity) in snap.items():
                if pid not in known and ppid in known:
                    known.add(pid)
                    changed = True
        return known

    def signal_live(sig: int) -> set[int]:
        targets = (_marked_processes(marker, variable) | registered_live()) - {os.getpid()}
        for pid in sorted(targets, reverse=True):
            # Re-read the marker before each signal so PID reuse cannot turn a
            # previously valid identity into authority over an unrelated task.
            if pid not in _marked_processes(marker, variable) \
                    and pid not in registered_live():
                continue
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        return targets

    signal_live(signal.SIGTERM)
    deadline = time.monotonic() + grace
    remaining = (_marked_processes(marker, variable) | registered_live()) - {os.getpid()}
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = (_marked_processes(marker, variable) | registered_live()) - {os.getpid()}
    if remaining:
        signal_live(signal.SIGKILL)
        kill_deadline = time.monotonic() + 1.0
        while remaining and time.monotonic() < kill_deadline:
            time.sleep(0.05)
            remaining = (_marked_processes(marker, variable) | registered_live()) - {os.getpid()}
    return remaining


def _run_process_tree(args: Any, *, cwd: Path, timeout: int,
                      env: Optional[dict[str, str]] = None,
                      input_text: Optional[str] = None,
                      shell: bool = False,
                      output_limit: int = 100_000,
                      decode_errors: str = "replace") -> tuple[subprocess.CompletedProcess, bool]:
    """Run a command in its own process group and reap the group on timeout.

    macOS and Linux both provide POSIX process groups. Kernel process identities
    also track descendants that escape the group or sanitize their environment.
    Reader threads retain bounded head/tail output while the process runs and
    keep draining through TERM/KILL cleanup.
    """
    marker = secrets.token_hex(16)
    child_env = dict(os.environ if env is None else env)
    child_env["REVIEW_LOOP_PROCESS_TREE"] = marker
    gate_read, gate_write = os.pipe()
    child_env["REVIEW_LOOP_GATE_FD"] = str(gate_read)
    launcher = (
        "import os,sys; "
        "fd=int(os.environ.pop('REVIEW_LOOP_GATE_FD')); "
        "os.read(fd,1); os.close(fd); "
        "shell=sys.argv[1]=='1'; command=sys.argv[2:]; "
        "os.execv('/bin/sh',['/bin/sh','-c',command[0]]) if shell else "
        "os.execvpe(command[0],command,os.environ)"
    )
    command_args = [str(args)] if shell else [str(value) for value in args]
    try:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", launcher, "1" if shell else "0", *command_args],
                cwd=str(cwd), env=child_env, shell=False, pass_fds=(gate_read,),
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            os.close(gate_write)
            raise
    finally:
        os.close(gate_read)
    registry = None
    run_dir_value = child_env.get("REVIEW_LOOP_RUN_DIR")
    if run_dir_value:
        registry = Path(run_dir_value) / "processes.jsonl"
    try:
        tracker = _ProcessTracker(proc.pid, registry)
    except BaseException:
        os.close(gate_write)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=2)
        raise
    if proc.pid not in tracker.identities:
        # Do not launch the real command when this platform cannot provide a
        # stable kernel identity for the supervisor process.
        os.close(gate_write)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=2)
        tracker.close()
        raise RuntimeError("could not establish portable process-tree supervision")
    os.write(gate_write, b"1")
    os.close(gate_write)
    stdout_buf = _BoundedBytes(output_limit)
    stderr_buf = _BoundedBytes(max(4096, output_limit // 2))

    def drain(pipe: Any, buffer: _BoundedBytes) -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                buffer.add(chunk)
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=drain, args=(proc.stdout, stdout_buf), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, stderr_buf), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    output_exceeded = False
    try:
        if input_text is not None and proc.stdin is not None:
            proc.stdin.write(input_text.encode())
            proc.stdin.close()
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if stdout_buf.exceeded.is_set() or stderr_buf.exceeded.is_set():
                output_exceeded = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)
        if proc.poll() is not None:
            for reader in readers:
                reader.join(timeout=0.5)
        output_exceeded = (output_exceeded or stdout_buf.exceeded.is_set()
                           or stderr_buf.exceeded.is_set())
        if not timed_out and not output_exceeded:
            # A CLI may exit after launching a detached helper. The command is
            # complete only when its recorded descendants are gone too.
            remaining = ((tracker.live() | _marked_processes(marker)) - {proc.pid})
            if remaining:
                _signal_process_tree(proc.pid, signal.SIGTERM, marker)
                tracker.signal(signal.SIGTERM)
                grace = time.monotonic() + 1.0
                while (tracker.live() - {proc.pid}) and time.monotonic() < grace:
                    time.sleep(0.02)
                _signal_process_tree(proc.pid, signal.SIGKILL, marker)
                tracker.signal(signal.SIGKILL)
        if timed_out or output_exceeded:
            _signal_process_tree(proc.pid, signal.SIGTERM, marker)
            tracker.signal(signal.SIGTERM)
            grace = time.monotonic() + 1.0
            while tracker.live() and time.monotonic() < grace:
                time.sleep(0.02)
            _signal_process_tree(proc.pid, signal.SIGKILL, marker)
            tracker.signal(signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            tracker.signal(signal.SIGKILL)
            proc.wait(timeout=2)
    except BaseException:
        # No exception after Popen may leave a write-capable command alive.
        _signal_process_tree(proc.pid, signal.SIGTERM, marker)
        tracker.signal(signal.SIGTERM)
        time.sleep(0.05)
        _signal_process_tree(proc.pid, signal.SIGKILL, marker)
        tracker.signal(signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        raise
    finally:
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        for reader in readers:
            reader.join(timeout=1)
        tracker.close()
    stdout = stdout_buf.text(decode_errors)
    stderr = stderr_buf.text(decode_errors)
    rc = proc.returncode
    if output_exceeded:
        rc = rc if rc not in (None, 0) else 1
        stderr += f"\noutput exceeded the {output_limit}-byte safety limit"
    completed = subprocess.CompletedProcess(args, rc, stdout, stderr)
    return completed, timed_out

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
    _safe_write_text(prompt_file, prompt)
    out_file = work / f"{log_name}.out.txt"
    log_file = run.dir / "logs" / f"{log_name}.log"
    log_file.parent.mkdir(exist_ok=True)

    default_model, default_effort = _role_defaults(cli, readonly)
    model = slot.get("model") or default_model
    effort = slot.get("effort") or default_effort
    argv, stdin_text = ad["argv"](
        prompt,
        model,
        effort,
        readonly,
        run.config.get("permission_mode", "acceptEdits"),
        out_file,
    )

    env = os.environ.copy()
    if cli == "copilot":
        # The permission contract is expressed entirely by argv. Parent-level
        # blanket approvals must never widen acceptEdits or reviewer access.
        for key in ("COPILOT_ALLOW_ALL", "COPILOT_ALLOW_ALL_TOOLS",
                    "COPILOT_AUTO_APPROVE"):
            env.pop(key, None)
    env["REVIEW_LOOP_RUN_DIR"] = str(run.dir.resolve())
    env_key = ad["config_dir_env"]
    override = slot.get("config_dir")
    if env_key and override:
        env[env_key] = os.path.expanduser(override)

    started = time.time()
    run.emit("agent_start", label=label, cli=cli,
             model=model, effort=effort, readonly=readonly)

    timeout = int(run.config.get("agent_timeout_seconds", 3600))
    proc, timed_out = _run_process_tree(
        argv, cwd=run.repo, env=env, timeout=timeout, input_text=stdin_text,
        output_limit=int(run.config.get("max_log_chars", 100_000)))

    # Transcripts are for debugging a run, not archiving it. Keep the head and
    # tail of each stream: the middle of a long agent transcript is where the
    # least useful bytes live.
    log_cap = int(run.config.get("max_log_chars", 100_000))
    _safe_write_text(log_file,
        f"$ {' '.join(argv[:6])} ...\n\n"
        f"--- STDOUT ---\n{_head_tail(proc.stdout, log_cap)}\n"
        f"--- STDERR ---\n{_head_tail(proc.stderr, log_cap // 4)}"
    )

    if timed_out:
        run.emit("agent_error", label=label, error=f"timed out after {timeout}s")
        return False, f"{label} timed out after {timeout}s"

    if ad["reads_out_file"] and out_file.exists():
        if out_file.lstat().st_size > log_cap:
            run.emit("agent_error", label=label,
                     error=f"agent output file exceeded {log_cap} bytes")
            return False, f"{label} output exceeded the configured safety limit"
        text = out_file.read_text(errors="replace")
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
    if proc.returncode != 0:
        run.emit("agent_error", label=label, rc=proc.returncode,
                 error=(proc.stderr or "").strip()[:600], seconds=elapsed)
        detail = (proc.stderr or text or f"exit {proc.returncode}").strip()
        return False, detail[:600]
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
                structured = env.get("structured_output")
                if isinstance(structured, (dict, list)):
                    return json.dumps(structured)
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

def extract_json(text: str) -> Optional[dict[str, Any]]:
    """Pull a review object out of model output.

    Older Claude versions and Copilot cannot be schema-constrained, so this
    still copes with prose wrappers, fenced blocks and trailing commentary.
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


def review_problem(obj: Optional[dict[str, Any]]) -> Optional[str]:
    """Why this review object cannot be trusted, or None if it is well formed.

    `extract_json` only asks whether *something* JSON-shaped came back, and
    not every adapter can be schema-constrained, so the shape has to be checked here
    for every adapter. Anything that fails this is a reviewer that did not
    report — never a reviewer that approved. The permissive readings are the
    dangerous ones: a missing verdict and a `findings` value that is not a list
    both normalise to zero findings, which reads exactly like a clean review.
    """
    if not isinstance(obj, dict):
        return "review was not a JSON object"
    allowed_top = {"verdict", "findings"}
    extra_top = set(obj) - allowed_top
    if extra_top:
        return f"unexpected review fields: {sorted(extra_top)}"
    raw_verdict = obj.get("verdict")
    if not isinstance(raw_verdict, str) or raw_verdict not in (
            "approved", "changes_requested"):
        return "`verdict` must be `approved` or `changes_requested`"
    verdict = raw_verdict
    findings = obj.get("findings")
    if not isinstance(findings, list):
        return f"`findings` was {type(findings).__name__}, not a list"
    if any(not isinstance(f, dict) for f in findings):
        return "`findings` contained entries that were not objects"
    required = {"id", "severity", "file", "line", "category", "problem",
                "impact", "recommended_fix"}
    for index, finding in enumerate(findings):
        assert isinstance(finding, dict)
        if set(finding) != required:
            missing = sorted(required - set(finding))
            extra = sorted(set(finding) - required)
            return (f"findings[{index}] fields did not match the schema "
                    f"(missing={missing}, extra={extra})")
        for field in ("id", "file", "category", "problem", "impact",
                      "recommended_fix"):
            if not isinstance(finding[field], str) or not finding[field].strip():
                return f"findings[{index}].{field} must be a non-empty string"
        if finding["severity"] not in SEVERITIES:
            return f"findings[{index}].severity is not a known severity"
        line = finding["line"]
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)
                                 or line < 1):
            return f"findings[{index}].line must be a positive integer or null"
    if verdict == "changes_requested" and not findings:
        return "requested changes but listed no findings"
    if verdict == "approved" and findings:
        return "approved verdict cannot contain findings"
    return None


def normalise_findings(reviewer_id: str, obj: Optional[dict[str, Any]],
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

class GitError(RuntimeError):
    """A Git operation required for isolation or review could not complete."""


def _git_toplevel(repo: Path) -> tuple[Optional[Path], str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=str(repo),
            capture_output=True, text=True,
        )
    except OSError as exc:
        return None, str(exc)
    top = result.stdout.strip()
    if result.returncode == 0 and top:
        return Path(top).resolve(), ""
    return None, result.stderr.strip() or "not a Git working tree"


def _has_git_marker(repo: Path) -> bool:
    """Whether this path is nested under filesystem metadata for a worktree."""
    return any(os.path.lexists(str(candidate / ".git"))
               for candidate in (repo, *repo.parents))


def canonical_worktree_root(repo: Path, allow_non_git: bool = False) -> Path:
    """Return the one filesystem root that identifies this working tree.

    Git state and locking must not be scoped to the directory named in a
    config: two configs pointing at different subdirectories still operate on
    the same files. Non-Git operation remains available only through the
    explicit escape hatch and keeps the configured directory as its root.
    """
    resolved = repo.expanduser().resolve()
    if not resolved.is_dir():
        raise RuntimeError(f"repo path does not exist: {resolved}")
    top, detail = _git_toplevel(resolved)
    if top is not None:
        return top
    if allow_non_git and not _has_git_marker(resolved):
        return resolved
    if allow_non_git:
        raise RuntimeError(
            f"could not resolve the existing Git worktree at {resolved} ({detail}); "
            "`allow_non_git` cannot mask this failure"
        )
    raise RuntimeError(
        f"{resolved} is not a git working tree ({detail}). The build agent edits "
        "files in place and there would be no way to see or undo what it changed. "
        "Set `allow_non_git: true` to override."
    )


def resolve_config_repo(config: dict[str, Any]) -> tuple[Optional[Path], list[str]]:
    """Resolve and persist the actual run root before any state is touched."""
    raw = config.get("repo") or os.getcwd()
    if not isinstance(raw, (str, os.PathLike)):
        return None, ["`repo` must be a non-empty path string."]
    configured = Path(raw).expanduser().resolve()
    if not configured.is_dir():
        return None, [f"repo path does not exist: {configured}"]
    repo, detail = _git_toplevel(configured)
    if repo is None:
        if _has_git_marker(configured):
            return None, [
                f"could not resolve {configured} as a Git worktree ({detail}). The build agent "
                "edits files in place and there would be no way to see or undo what it "
                "changed. `allow_non_git` cannot override a failure inside an existing "
                "Git worktree."
            ]
        if not config.get("allow_non_git"):
            return None, [
                f"{configured} is not a git working tree ({detail}). The build agent edits "
                "files in place and there would be no way to see or undo what it changed. "
                "Set `allow_non_git: true` to override."
            ]
        repo = configured
        config["_git_worktree"] = False
    else:
        config["_git_worktree"] = True
    config["repo"] = str(repo)
    return repo, []


def state_for_repo(repo: Path, git_worktree: Optional[bool] = None) -> Path:
    """Return state outside the agent-cleanable worktree.

    Git's per-worktree administrative directory is stable, unique for linked
    worktrees, and is not touched by `git clean`. Explicit non-Git runs use a
    private per-user state root keyed by the canonical repository path.
    """
    if git_worktree is not False:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"], cwd=str(repo),
                capture_output=True, text=True,
            )
        except OSError as exc:
            raise GitError(f"could not locate Git administrative directory: {exc}") from exc
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve() / "review-loop"
        if git_worktree is True:
            detail = result.stderr.strip() or "git rev-parse failed"
            raise GitError(f"could not locate Git administrative directory: {detail}")
    root = Path(os.environ.get(
        "REVIEW_LOOP_STATE_ROOT",
        str(Path.home() / ".review-loop" / "state"),
    )).expanduser().resolve()
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]
    return root / "non-git" / key


def _validate_plain_directory(path: Path) -> None:
    """Reject symlinks and unexpected filesystem objects in state ancestors."""
    current = path
    existing: list[Path] = []
    while True:
        if os.path.lexists(str(current)):
            existing.append(current)
        if current == current.parent:
            break
        current = current.parent
    for item in reversed(existing):
        mode = item.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"unsafe review-loop state path component: {item}")


def _ensure_state_ignored(state: Path) -> None:
    """Create or validate private, installer-owned orchestration state.

    State is never placed at repository-controlled `.review-loop`. Every
    existing component and ownership marker is checked with lstat so a planted
    symlink or lookalike directory cannot redirect prompts, logs, or locks.
    """
    _validate_plain_directory(state.parent)
    if os.path.lexists(str(state)):
        mode = state.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"refusing unsafe review-loop state destination: {state}")
        marker = state / ".owner"
        try:
            marker_mode = marker.lstat().st_mode
            owner = marker.read_text()
        except OSError as exc:
            raise RuntimeError(f"unrecognized review-loop state directory: {state}") from exc
        if stat.S_ISLNK(marker_mode) or not stat.S_ISREG(marker_mode) \
                or owner.strip() != STATE_OWNER:
            raise RuntimeError(f"unrecognized review-loop state directory: {state}")
    else:
        state.mkdir(parents=True, mode=0o700)
        os.chmod(state, 0o700)
        marker = state / ".owner"
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(STATE_OWNER + "\n")

    # Critical children are never allowed to be symlinks. Run contents are
    # checked again when a run is resolved from CLI input.
    for child in ("history", "lock", "lock.guard", "current-run", "final.md"):
        candidate = state / child
        if os.path.lexists(str(candidate)):
            child_mode = candidate.lstat().st_mode
            expected = stat.S_ISDIR(child_mode) if child == "history" \
                else stat.S_ISREG(child_mode)
            if stat.S_ISLNK(child_mode) or not expected:
                raise RuntimeError(f"unsafe object in review-loop state: {candidate}")

def _git_capture(repo: Path, *args: str, limit: int = MAX_GIT_CAPTURE_BYTES) -> str:
    try:
        r, timed_out = _run_process_tree(
            ["git", *args], cwd=repo, timeout=120, output_limit=limit,
            decode_errors="surrogateescape")
    except OSError as exc:
        raise GitError(f"could not run git {' '.join(args)}: {exc}") from exc
    if timed_out:
        raise GitError(f"git {' '.join(args)} timed out")
    if r.returncode != 0:
        detail = r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}"
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return r.stdout


def git(repo: Path, *args: str) -> str:
    return _git_capture(repo, *args).strip()


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


def preflight_repo(repo: Path, config: dict[str, Any]) -> list[str]:
    """Refuse to run somewhere the loop could do damage it cannot undo.

    The skill asks its host to check this, but the script is a CLI and is
    routinely driven directly, so the guarantee has to live here. Both gates
    are overridable — deliberately, and only in the config.
    """
    problems: list[str] = []
    if not repo.is_dir():
        return [f"repo path does not exist: {repo}"]

    if config.get("_git_worktree") is False:
        return []

    try:
        inside = _git_capture(repo, "rev-parse", "--is-inside-work-tree",
                              limit=64 * 1024).strip()
    except (OSError, GitError) as exc:
        if config.get("allow_non_git") and "_git_worktree" not in config \
                and not _has_git_marker(repo):
            return []
        if not _has_git_marker(repo) and not config.get("allow_non_git"):
            return [f"{repo} is not a git working tree. The build agent edits "
                    "files in place and there would be no way to undo changes. "
                    "Set `allow_non_git: true` to override."]
        return [f"could not verify Git worktree identity: {exc}"]
    if inside != "true":
        if config.get("_git_worktree") is True:
            problems.append("could not re-verify the resolved Git worktree")
        elif config.get("allow_non_git") and _has_git_marker(repo):
            problems.append("could not verify the existing Git worktree; "
                            "`allow_non_git` cannot mask this failure")
        elif not config.get("allow_non_git"):
            problems.append(f"{repo} is not a git working tree. The build agent edits "
                            "files in place and there would be no way to see or undo "
                            "what it changed. Set `allow_non_git: true` to override.")
        return problems

    # Orchestration state is outside the worktree, so every dirty path reported
    # here belongs to the repository and must participate in the safety gate.
    try:
        raw = _git_capture(repo, "status", "--porcelain", "-z")
    except (OSError, GitError) as exc:
        return [f"could not verify working-tree cleanliness: {exc}"]
    dirty = [path for _, path in _porcelain_entries(raw)]
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


@contextmanager
def _lock_guard(state: Path):
    """Serialize lock read/compare/write transitions across processes."""
    guard = state / "lock.guard"
    with guard.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_lock(state: Path, token: str, pid: int,
                 run_dir: Optional[Path] = None,
                 adopt_only: bool = False) -> Optional[str]:
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
    with _lock_guard(state):
        return _acquire_lock_unlocked(state, token, pid, run_dir, adopt_only)


def _acquire_lock_unlocked(state: Path, token: str, pid: int,
                           run_dir: Optional[Path],
                           adopt_only: bool) -> Optional[str]:
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
                _safe_write_text(tmp, payload(token))
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
            if adopt_only and held.get("run_dir") not in (None, str(run_dir)):
                return ("could not adopt the review-loop lock: its reserved run "
                        "directory does not match this resolved config.")
            # Our own handoff. Rotate the token so it cannot be replayed.
            _safe_write_text(lock, payload(new_lock_token()))
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
    with _lock_guard(state):
        try:
            held = json.loads(lock.read_text())
        except Exception:
            return
        if held.get("token") == token:
            held.update({"pid": pid, "run_dir": str(run_dir)})
            _safe_write_text(lock, json.dumps(held))


def release_lock(state: Path, run_dir: Path) -> None:
    lock = state / "lock"
    with _lock_guard(state):
        try:
            held = json.loads(lock.read_text())
        except Exception:
            return
        if held.get("run_dir") == str(run_dir):
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def release_starting_lock(state: Path, token: str) -> None:
    """Release a lock that failed before it could be keyed to a run dir."""
    lock = state / "lock"
    with _lock_guard(state):
        try:
            held = json.loads(lock.read_text())
        except Exception:
            return
        if held.get("token") == token and not held.get("run_dir"):
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def _state_for_run(run_dir: Path) -> Optional[Path]:
    """Resolve the state directory from a standard history/run-NN path."""
    resolved = run_dir.resolve()
    if resolved.parent.name != "history":
        return None
    state = resolved.parent.parent
    try:
        _ensure_state_ignored(state)
    except RuntimeError:
        return None
    return state


def _locked_run_pid(run_dir: Path) -> Optional[int]:
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


def base_commit(repo: Path, allow_non_git: bool = False,
                git_worktree: Optional[bool] = None) -> Optional[str]:
    """The SHA the loop started from, or None in a repository with no commits."""
    if git_worktree is False:
        return None
    if allow_non_git and git_worktree is None:
        try:
            probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                   cwd=str(repo), capture_output=True, text=True)
        except OSError:
            return None
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return None
    head_error: Optional[GitError] = None
    try:
        return git(repo, "rev-parse", "--verify", "HEAD")
    except GitError as exc:
        head_error = exc
    # An unborn repository is supported. A repository with any reachable
    # commit but an unreadable HEAD is corruption, not an empty baseline.
    count = git(repo, "rev-list", "--all", "--count")
    if count == "0":
        return None
    assert head_error is not None
    raise head_error


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
    return [f":(exclude,glob){s}" for s in specs]


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


def collect_diff(repo: Path, base_sha: Optional[str],
                 config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the review payload and report what it cost.

    Returns the diff text plus a manifest describing anything excluded or
    truncated, so a reviewer is never silently shown a partial picture.
    """
    non_git = config.get("_git_worktree") is False
    if config.get("allow_non_git") and "_git_worktree" not in config:
        try:
            probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                   cwd=str(repo), capture_output=True, text=True)
            non_git = probe.returncode != 0 or probe.stdout.strip() != "true"
        except OSError:
            non_git = True
    if non_git:
        text = ("[Non-Git run: no Git diff is available. Inspect the repository "
                "directly before reaching a verdict.]\n")
        return text, {"truncated_files": [], "skipped_files": [],
                      "omitted_files": [], "untracked_files": 0,
                      "untracked_included": 0, "chars": len(text),
                      "approx_tokens": len(text) // 4, "files": 0,
                      "non_git": True}

    excl = _exclude_args(config)
    parts: list[str] = []
    # In an unborn repository there is no HEAD to compare against. The index
    # and worktree are separate diffs; omitting --cached would hide every file
    # the implementer staged before review.
    ranges = ([(f"{base_sha}..HEAD",), ("HEAD",)] if base_sha
              else [("--cached",), ()])
    for rng in ranges:
        d = git(repo, "diff", *rng, "--", ".", *excl)
        if d.strip():
            parts.append(d)

    untracked = [f for f in _git_capture(
                    repo, "ls-files", "-z", "--others", "--exclude-standard",
                    "--", ".", *excl).split("\0")
                 if f.strip()]

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
    # Python represents undecodable POSIX filename bytes with surrogate code
    # points. Escape those for JSON/prompts while retaining the original path
    # object for lstat/open above.
    diff = diff.encode("utf-8", errors="backslashreplace").decode("utf-8")

    # Whole-payload ceiling, applied last so per-file trimming does the work.
    max_total = int(config.get("max_diff_chars", 120000))
    if len(diff) > max_total:
        diff = diff[:max_total] + f"\n\n[diff truncated at {max_total} chars]\n"
        manifest["payload_truncated"] = True

    manifest["chars"] = len(diff)
    manifest["approx_tokens"] = len(diff) // 4
    manifest["files"] = len(files) + len(untracked)
    return diff, manifest


def worktree_fingerprint(repo: Path, config: dict[str, Any]) -> str:
    """Hash the exact filesystem state covered by validation and review."""
    digest = hashlib.sha256()
    non_git = config.get("_git_worktree") is False
    if non_git:
        paths: list[str] = []
        for root, dirs, files in os.walk(str(repo), followlinks=False):
            dirs.sort()
            files.sort()
            base = Path(root)
            paths.extend(str((base / name).relative_to(repo)) for name in dirs + files)
    else:
        try:
            head = git(repo, "rev-parse", "--verify", "HEAD")
        except GitError:
            if git(repo, "rev-list", "--all", "--count") != "0":
                raise
            head = "(unborn)"
        digest.update(head.encode())
        digest.update(git(repo, "write-tree").encode())
        raw = _git_capture(repo, "ls-files", "-z", "--cached", "--others",
                           "--exclude-standard")
        paths = [path for path in raw.split("\0") if path]
    if len(paths) > MAX_FINGERPRINT_FILES:
        raise GitError(f"worktree has more than {MAX_FINGERPRINT_FILES} files to fingerprint")
    for relative in sorted(set(paths)):
        full = repo / relative
        digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
        if not os.path.lexists(str(full)):
            digest.update(b"(missing)\0")
            continue
        try:
            info = full.lstat()
            digest.update(str(stat.S_IFMT(info.st_mode)).encode() + b"\0")
            if stat.S_ISLNK(info.st_mode):
                digest.update(os.readlink(full).encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_FINGERPRINT_FILE_BYTES:
                    raise GitError(f"file too large to fingerprint safely: {relative}")
                with full.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
            else:
                digest.update(f"special:{info.st_mode}".encode())
        except FileNotFoundError as exc:
            raise GitError(f"worktree changed while fingerprinting: {relative}") from exc
    return digest.hexdigest()


def diff_note(manifest: dict[str, Any], config: dict[str, Any]) -> str:
    """Tell the reviewer exactly what it is not being shown."""
    lines = []
    if config.get("exclude_noise", True):
        lines.append("Lockfiles, build output, binary assets and generated code are "
                     "excluded from this diff. Do not report on them.")
    for item in manifest.get("truncated_files", [])[:10]:
        path = str(item["path"]).encode("utf-8", "backslashreplace").decode()
        lines.append(f"NOTE: {path} was truncated; inspect it directly if relevant.")
    for item in manifest.get("skipped_files", [])[:10]:
        path = str(item["path"]).encode("utf-8", "backslashreplace").decode()
        lines.append(f"NOTE: {path} was not inlined; read it from the repository.")
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
            env = os.environ.copy()
            env["REVIEW_LOOP_RUN_DIR"] = str(run.dir.resolve())
            r, timed_out = _run_process_tree(
                cmd, shell=True, cwd=run.repo, timeout=timeout, env=env,
                output_limit=int(run.config.get("max_log_chars", 100_000)))
            passed = r.returncode == 0 and not timed_out
            output = r.stdout + r.stderr
            if timed_out:
                output += f"\ncommand timed out after {timeout}s"
            tail = output[-4000:]
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

Do not modify the directory named by `REVIEW_LOOP_RUN_DIR`; it is protected
orchestration state outside the project.

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

def _terminal_run_failure(config: dict[str, Any], repo: Path, state: Path,
                          run_dir: Path, detail: str,
                          stage: str = "orchestration",
                          existing_run: Optional[Run] = None) -> int:
    """Write the terminal contract even when normal orchestration unwinds."""
    run_dir.mkdir(parents=True, exist_ok=True)
    run = existing_run or Run(repo, run_dir, config)
    snapshot = run.snapshot
    ledger = snapshot.get("ledger")
    if not isinstance(ledger, Ledger):
        ledger = Ledger()
    stage = str(snapshot.get("stage") or stage) if existing_run else stage
    run.emit("run_failed", stage=stage, error=detail[:1000])
    try:
        final = _write_final(
            run, snapshot.get("base_sha"), "run_failed", ledger,
            int(snapshot.get("rounds") or 0),
            str(snapshot.get("validation_status") or VALIDATION_NOT_CONFIGURED),
            set(config.get("blocking_severities") or ["blocker", "high", "medium"]),
            str(snapshot.get("baseline_status") or VALIDATION_NOT_CONFIGURED),
            list(snapshot.get("missing_reviewers") or []),
            failure_stage=stage, failure_detail=detail,
        )
    except Exception as exc:
        # Reporting must not repeat the exception that brought the loop here.
        body = ("# Review loop result\n\n**Outcome: run_failed**\n\n"
                f"- Failure stage: {stage}\n"
                f"- Error: {detail}\n"
                f"- Report fallback error: {type(exc).__name__}: {exc}\n")
        final = run_dir / "final.md"
        _safe_write_text(final, body)
        try:
            _safe_write_text(state / "final.md", body)
        except OSError:
            pass
    validation_status = str(snapshot.get("validation_status") or VALIDATION_NOT_CONFIGURED)
    run.emit("run_complete", outcome="run_failed",
             rounds=int(snapshot.get("rounds") or 0), findings=len(ledger.entries),
             resolved=len(ledger.resolved()),
             blocking_open=len(ledger.open_findings()),
             validation_status=validation_status,
             validation_passed=validation_status == VALIDATION_PASSED,
             missing_reviewers=list(snapshot.get("missing_reviewers") or []),
             final=str(final), error=detail[:400])
    return exit_code("run_failed")


def _terminalize_handed_failure(config: dict[str, Any], state: Path,
                                run_dir: Path, token: str, detail: str) -> int:
    """Adopt a parent's reserved run when possible, then fail it terminally."""
    raw_repo = config.get("repo")
    if not isinstance(raw_repo, str):
        return 1
    repo = Path(raw_repo).expanduser().resolve()
    if not repo.is_dir():
        return 1
    held = acquire_lock(state, token, os.getpid(), run_dir, adopt_only=True)
    if held:
        # Failed adoption is not authority to write a caller-selected path.
        return 1
    try:
        return _terminal_run_failure(config, repo, state, run_dir, detail, "launch")
    finally:
        release_lock(state, run_dir)

def cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": "invalid config", "problems": [str(exc)]}, indent=2),
              file=sys.stderr)
        return 1
    if not isinstance(config, dict):
        print(json.dumps({"error": "invalid config",
                          "problems": ["run config must be a JSON object."]}, indent=2),
              file=sys.stderr)
        return 1
    handed_over = (isinstance(config.get("run_dir"), str)
                   and isinstance(config.get("lock_token"), str)
                   and bool(config["run_dir"]) and bool(config["lock_token"]))
    handed_run = Path(config["run_dir"]) if handed_over else None
    # Only `start` may supply the paired internal handoff fields. A foreground
    # run always mints a fresh secret; a stray/injected token can never become
    # authority to adopt another process's lock.
    token = config["lock_token"] if handed_over else new_lock_token()
    repo, repo_problems = resolve_config_repo(config)
    problems = validate_config(config) + repo_problems
    state: Optional[Path] = None
    handoff_authenticated = False
    if repo is not None:
        try:
            state = state_for_repo(repo, config.get("_git_worktree"))
            _ensure_state_ignored(state)
        except (GitError, RuntimeError) as exc:
            problems.append(str(exc))
        if handed_over and handed_run is not None and state is not None:
            expected_config = state / "history" / handed_run.name / "config.json"
            try:
                held_record = json.loads((state / "lock").read_text())
                handoff_authenticated = (
                    handed_run.absolute().parent.resolve() == (state / "history").resolve()
                    and config_path == expected_config.resolve()
                    and not stat.S_ISLNK(handed_run.lstat().st_mode)
                    and held_record.get("token") == token
                    and held_record.get("run_dir") == str(handed_run)
                )
            except (OSError, ValueError, AttributeError):
                handoff_authenticated = False
            if not handoff_authenticated:
                problems.append("internal handoff fields do not match the canonical "
                                "reserved run and live lock")
    if problems:
        print(json.dumps({"error": "invalid config", "problems": problems}, indent=2),
              file=sys.stderr)
        if handed_run is not None and handoff_authenticated and state is not None:
            return _terminalize_handed_failure(
                config, state, handed_run, token, "; ".join(problems))
        return 1
    assert repo is not None
    assert state is not None

    # A `run_dir` in the config means `start` already reserved one and is
    # handing this process its lock; `lock_token` is the proof. Run directly
    # and we mint our own token, which no live lock can match.
    if not handed_over:
        problems = preflight_repo(repo, config)
        if problems:
            print(json.dumps({"error": "invalid config", "problems": problems}, indent=2),
                  file=sys.stderr)
            return 1

    held = acquire_lock(state, token, os.getpid(),
                        handed_run,
                        adopt_only=handed_over)
    if held:
        print(json.dumps({"error": "run already active", "problems": [held]}, indent=2),
              file=sys.stderr)
        return 1

    run_dir = handed_run
    try:
        if handed_over:
            problems = preflight_repo(repo, config)
            if problems:
                print(json.dumps({"error": "invalid config", "problems": problems},
                                 indent=2), file=sys.stderr)
                assert run_dir is not None
                return _terminal_run_failure(
                    config, repo, state, run_dir, "; ".join(problems), "preflight")
        if run_dir is None:
            run_dir = _new_run_dir(state)
        run_dir.mkdir(parents=True, exist_ok=True)
        # The lock is keyed on the run directory from here on, so release can
        # tell our lock from a later run's.
        handoff_lock(state, token, os.getpid(), run_dir)
        run_obj = Run(repo, run_dir, config)
        try:
            return _run_loop(config, repo, state, run_dir, run_obj)
        except Exception as exc:
            # A detached caller only has the event stream and final report.
            # Never leave it polling forever because an unanticipated error
            # escaped the normal stage-specific failure handling.
            detail = f"{type(exc).__name__}: {exc}"
            return _terminal_run_failure(config, repo, state, run_dir, detail,
                                         existing_run=run_obj)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if run_dir is None:
            failure_name = f"run-failed-{os.getpid()}-{secrets.token_hex(4)}"
            report_errors: list[str] = []
            for candidate in (state / "history" / failure_name,
                              state / failure_name):
                try:
                    candidate.mkdir(parents=True, exist_ok=False)
                    run_dir = candidate
                    break
                except OSError as fallback_exc:
                    report_errors.append(
                        f"{type(fallback_exc).__name__}: {fallback_exc}")
            else:
                print(json.dumps({
                    "error": "run_failed",
                    "stage": "launch",
                    "detail": detail,
                    "report_errors": report_errors,
                }, indent=2), file=sys.stderr)
                return exit_code("run_failed")
            handoff_lock(state, token, os.getpid(), run_dir)
        return _terminal_run_failure(config, repo, state, run_dir, detail)
    finally:
        if run_dir is not None:
            release_lock(state, run_dir)
        # If normal and emergency run-directory creation both failed, the lock
        # never acquired a run_dir. This is harmless after a successful token
        # rotation and essential on the pre-handoff failure path.
        release_starting_lock(state, token)


def _run_loop(config: dict[str, Any], repo: Path, state: Path, run_dir: Path,
              run: Optional[Run] = None) -> int:
    run = run or Run(repo, run_dir, config)

    _safe_write_text(run_dir / "config.json", json.dumps(config, indent=2))
    _safe_write_text(state / "task.md", config["task"])
    # `start` writes this too, but a foreground `run` is a first-class entry
    # point and `render`/`status` resolve through it — without this they would
    # report on whichever run was started last.
    _safe_write_text(state / "current-run", str(run_dir))
    _safe_write_text(run_dir / "pid", str(os.getpid()))

    task = config["task"]
    blocking = set(config.get("blocking_severities") or ["blocker", "high", "medium"])
    max_iter = int(config.get("max_iterations", 5))

    base_sha = base_commit(repo, allow_non_git=bool(config.get("allow_non_git")),
                           git_worktree=config.get("_git_worktree"))
    run.snapshot.update({"base_sha": base_sha, "stage": "baseline"})
    if base_sha is None:
        run.emit("warning", message="Repository has no commits; reviewing the whole working tree.")
    run.emit("run_start", repo=str(repo), base_sha=base_sha,
             reviewers=[r["slot_id"] for r in config["reviewers"]],
             max_iterations=max_iter)

    # --- baseline: was validation already failing before we touched it? ----
    # Without this the loop cannot tell "the change broke the build" from "the
    # build was broken when we arrived", and every later result is ambiguous.
    baseline_before = worktree_fingerprint(repo, config)
    baseline_status, baseline_results = run_validation(run, label="baseline")
    run.snapshot["baseline_status"] = baseline_status
    _safe_write_text(run_dir / "validation-00.json", json.dumps(
        {"stage": "baseline", "status": baseline_status, "results": baseline_results}, indent=2))
    if worktree_fingerprint(repo, config) != baseline_before:
        raise RuntimeError(
            "worktree changed while baseline validation was running; its result "
            "does not describe a stable filesystem snapshot"
        )
    run.emit("baseline_validation", status=baseline_status,
             commands=len(baseline_results))
    if baseline_status == VALIDATION_FAILED:
        run.emit("warning", message="Validation was already failing before the task "
                                    "started; the independent gate cannot attribute a "
                                    "later failure to this change.")
        if config.get("require_clean_baseline", True):
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

    run.snapshot["stage"] = "implement"
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
    _safe_write_text(run_dir / "implementer-00.md", text)

    outcome = "max_iterations_reached"
    ledger = Ledger()
    run.snapshot["ledger"] = ledger
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
        run.snapshot.update({"rounds": iteration, "stage": "validation"})

        run.emit("iteration_start", iteration=iteration)

        # --- independent gate: tests decide, not model consensus -----------
        validation_before = worktree_fingerprint(repo, config)
        val_status, val_results = run_validation(run)
        run.snapshot["validation_status"] = val_status
        val_ok = val_status == VALIDATION_PASSED
        _safe_write_text(run_dir / f"validation-{iteration:02d}.json",
                         json.dumps(val_results, indent=2))

        reviewed_fingerprint = worktree_fingerprint(repo, config)
        if reviewed_fingerprint != validation_before:
            raise RuntimeError(
                "worktree changed while validation was running; the validation "
                "result does not cover the reviewed filesystem snapshot"
            )
        run.snapshot["stage"] = "diff_collection"
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
        workers = min(len(reviewers), MAX_AGENT_WORKERS) \
            if config.get("parallel", True) else 1
        run.snapshot["stage"] = "review"
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(do_review, reviewers))

        if worktree_fingerprint(repo, config) != reviewed_fingerprint:
            raise RuntimeError(
                "worktree changed after validation/diff collection while reviewers "
                "were running; approval is invalid and the run failed closed"
            )

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
            _safe_write_text(run_dir / f"review-{iteration:02d}-{slot}.json",
                             json.dumps(obj if isinstance(obj, dict) else
                                        {"verdict": "unparsed", "findings": []}, indent=2))
            counts = {s: sum(1 for x in found if x["severity"] == s) for s in SEVERITIES}
            run.emit("review_done", reviewer=slot,
                     label=rv.get("label") or slot,
                     verdict=(_verdict(obj) if isinstance(obj, dict) else "") or "unparsed",
                     usable=not problem,
                     counts={k: v for k, v in counts.items() if v})
            round_findings += found
        run.snapshot["missing_reviewers"] = list(missing_reviewers)

        panel_complete = not missing_reviewers
        merged = dedupe(round_findings)
        _safe_write_text(run_dir / f"findings-{iteration:02d}.json",
                         json.dumps(merged, indent=2))
        ledger.record_round(iteration, merged, panel_complete=panel_complete)
        _safe_write_text(run_dir / "ledger.json", json.dumps(ledger.to_json(), indent=2))

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
        run.snapshot["stage"] = "fix"
        ok, text = invoke_agent(run, config["implementer"],
                                FIX_PROMPT.format(task=task, count=len(reviewers),
                                                  reviews=payload),
                                readonly=False, label="Implementer (fixes)",
                                log_name=f"iter{iteration:02d}-fix", attempts=2)
        if not ok:
            run.emit("run_failed", stage="fix", error=text)
            outcome = "implementer_failed"
            break
        _safe_write_text(run_dir / f"implementer-{iteration:02d}.md", text)

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
                     brief: bool = False, baseline: Optional[str] = None) -> str:
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
    entries = list(history.iterdir())
    if any(stat.S_ISLNK(entry.lstat().st_mode) for entry in entries):
        raise RuntimeError(f"unsafe symlink in review-loop history: {history}")
    n = len([d for d in entries if stat.S_ISDIR(d.lstat().st_mode)]) + 1
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
    "run_failed": 1,
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
    "baseline_failed": "Validation was already failing before the task started. A clean "
                       "baseline is required by default so the task cannot silently grow. "
                       "Fix the build first, or explicitly set `require_clean_baseline: "
                       "false` for a task whose purpose includes repairing it.",
    "max_iterations_reached": "The loop hit its iteration cap with blocking findings "
                              "still open. This needs a human — do not simply raise the cap.",
    "stopped_by_user": "Stopped on request. The working tree holds whatever the last "
                       "completed step produced.",
    "implementer_failed": "The build agent could not complete a fix round. See logs/.",
    "run_failed": "The orchestrator encountered an unexpected error and failed closed. "
                  "See the run event stream and logs for the recorded error.",
    "no_progress": "A fix round changed nothing the reviewers cared about, so the loop "
                   "stopped rather than spending another panel on the same answer. "
                   "The findings below need a human.",
}


VALIDATION_LABEL = {
    VALIDATION_PASSED: "passing",
    VALIDATION_FAILED: "FAILING",
    VALIDATION_NOT_CONFIGURED: "NOT CONFIGURED — nothing was independently verified",
}


def _write_final(run: Run, base_sha: Optional[str], outcome: str,
                 ledger: Ledger, rounds: int, validation_status: str,
                 blocking: set[str], baseline_status: str = VALIDATION_NOT_CONFIGURED,
                 missing_reviewers: Optional[list[str]] = None,
                 failure_stage: Optional[str] = None,
                 failure_detail: Optional[str] = None) -> Path:
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
    if failure_stage or failure_detail:
        lines += ["## Failure", "",
                  f"- Stage: {failure_stage or 'unknown'}",
                  f"- Detail: {failure_detail or 'unavailable'}", ""]
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
    _safe_write_text(path, body)
    state = _state_for_run(run.dir)
    if state is not None:
        _safe_write_text(state / "final.md", body)
    return path


# ---------------------------------------------------------------------------
# start / status / stop
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": "invalid config", "problems": [str(exc)]}, indent=2),
              file=sys.stderr)
        return 1
    if not isinstance(config, dict):
        print(json.dumps({"error": "invalid config",
                          "problems": ["run config must be a JSON object."]}, indent=2),
              file=sys.stderr)
        return 1
    # Validate before detaching: a config error must surface here, where the
    # caller can still see it, not in a log file nobody is watching.
    problems = validate_config(config)
    repo, repo_problems = resolve_config_repo(config)
    problems += repo_problems
    if repo is not None:
        problems += preflight_repo(repo, config)
    if problems:
        print(json.dumps({"error": "invalid config", "problems": problems}, indent=2),
              file=sys.stderr)
        return 1
    assert repo is not None
    try:
        state = state_for_repo(repo, config.get("_git_worktree"))
        _ensure_state_ignored(state)
    except (GitError, RuntimeError) as exc:
        print(json.dumps({"error": "unsafe state", "problems": [str(exc)]}, indent=2),
              file=sys.stderr)
        return 1

    # Claim the worktree before reserving a run directory or repointing
    # `current-run`, so a second `start` cannot detach a run that would fight
    # the first one. Our own pid holds the lock until the child adopts it.
    token = new_lock_token()
    held = acquire_lock(state, token, os.getpid())
    if held:
        print(json.dumps({"error": "run already active", "problems": [held]}, indent=2),
              file=sys.stderr)
        return 1

    run_dir: Optional[Path] = None
    proc: Optional[subprocess.Popen] = None
    log = None
    try:
        run_dir = _new_run_dir(state)
        config["run_dir"] = str(run_dir)
        config["lock_token"] = token
        resolved = run_dir / "config.json"
        _safe_write_text(resolved, json.dumps(config, indent=2))
        log = (run_dir / "run.log").open("w")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--config", str(resolved)],
            cwd=str(repo), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Name the child on the lock so the window between this process exiting
        # and the child adopting does not look like a crashed run to the next caller.
        handoff_lock(state, token, proc.pid, run_dir)
        _safe_write_text(run_dir / "pid", str(proc.pid))
        _safe_write_text(state / "current-run", str(run_dir))
        print(json.dumps({"run_dir": str(run_dir), "pid": proc.pid,
                          "progress": str(run_dir / "progress.jsonl")}, indent=2))
        return 0
    except Exception as exc:
        if run_dir is not None:
            _terminate_marked_processes(run_dir, grace=0.5)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if run_dir is None:
            release_starting_lock(state, token)
        else:
            handoff_lock(state, token, os.getpid(), run_dir)
            terminal = False
            try:
                if (run_dir / "progress.jsonl").exists():
                    terminal = any(
                        json.loads(line).get("event") == "run_complete"
                        for line in (run_dir / "progress.jsonl").read_text().splitlines()
                        if line.strip()
                    )
            except (OSError, ValueError, AttributeError):
                terminal = False
            if not terminal:
                try:
                    _terminal_run_failure(
                        config, repo, state, run_dir,
                        f"{type(exc).__name__}: {exc}", "launch")
                except Exception as report_exc:
                    print(json.dumps({
                        "error": "could not write terminal launch report",
                        "detail": f"{type(report_exc).__name__}: {report_exc}",
                    }), file=sys.stderr)
            release_lock(state, run_dir)
            release_starting_lock(state, token)
        print(json.dumps({"error": "could not launch run", "detail": str(exc)}),
              file=sys.stderr)
        return 1
    finally:
        if log is not None:
            log.close()


def _resolve_run(args: argparse.Namespace) -> Optional[Path]:
    if getattr(args, "run", None):
        candidate = Path(args.run).expanduser()
        try:
            return _validated_run_dir(candidate, getattr(args, "repo", None))
        except RuntimeError:
            return None
    configured = Path(getattr(args, "repo", None) or os.getcwd())
    try:
        repo = canonical_worktree_root(configured, allow_non_git=True)
    except (OSError, RuntimeError):
        repo = configured.expanduser().resolve()
    try:
        state = state_for_repo(repo, None)
        _ensure_state_ignored(state)
    except (GitError, RuntimeError):
        return None
    pointer = state / "current-run"
    if pointer.exists():
        try:
            return _validated_run_dir(Path(pointer.read_text().strip()), str(repo))
        except RuntimeError:
            return None
    history = state / "history"
    if history.exists():
        dirs = sorted([d for d in history.iterdir()
                       if not stat.S_ISLNK(d.lstat().st_mode) and d.is_dir()])
        if dirs:
            try:
                return _validated_run_dir(dirs[-1], str(repo))
            except RuntimeError:
                return None
    return None


def _validated_run_dir(candidate: Path, repo_hint: Optional[str] = None) -> Path:
    """Authenticate an explicit run path before any read, write, or signal."""
    absolute = candidate.absolute()
    if not os.path.lexists(str(absolute)) or stat.S_ISLNK(absolute.lstat().st_mode) \
            or not absolute.is_dir():
        raise RuntimeError("run directory is missing, symlinked, or not a directory")
    config_path = absolute / "config.json"
    try:
        mode = config_path.lstat().st_mode
    except OSError as exc:
        raise RuntimeError("run directory has no owned config.json") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("run config is not a regular file")
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("run config is unreadable") from exc
    raw_repo = repo_hint or config.get("repo")
    if not isinstance(raw_repo, str):
        raise RuntimeError("run config has no repository identity")
    repo = canonical_worktree_root(Path(raw_repo), allow_non_git=True)
    git_worktree = config.get("_git_worktree")
    state = state_for_repo(repo, git_worktree)
    _ensure_state_ignored(state)
    history = state / "history"
    if absolute.resolve().parent != history.resolve() or not re.fullmatch(
            r"run-[0-9]+|run-failed-[A-Za-z0-9-]+", absolute.name):
        raise RuntimeError("run directory is outside the canonical state history")
    if config_path.resolve() != (absolute / "config.json").resolve():
        raise RuntimeError("run config does not belong to this run")
    for reserved in ("progress.jsonl", "final.md", "STOP", "pid"):
        child = absolute / reserved
        if os.path.lexists(str(child)) and stat.S_ISLNK(child.lstat().st_mode):
            raise RuntimeError(f"unsafe symlink in run directory: {reserved}")
    return absolute.resolve()


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
    pid = _locked_run_pid(run_dir)
    running = pid is not None and _pid_is_review_loop(pid, run_dir)
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
    if args.kill:
        pid = _locked_run_pid(run_dir)
        if pid is None or not _pid_is_review_loop(pid, run_dir):
            print(json.dumps({
                "error": "refusing to signal an unverified process",
                "run_dir": str(run_dir),
                "hint": "The run is not actively locked by a matching review-loop process."
            }), file=sys.stderr)
            return 1
    else:
        # Even a cooperative stop is a write. Require a live authenticated
        # lock so an explicit --run path cannot redirect it through a planted
        # file or a historical run.
        pid = _locked_run_pid(run_dir)
        if pid is None or not _pid_is_review_loop(pid, run_dir):
            print(json.dumps({"error": "run is not actively owned",
                              "run_dir": str(run_dir)}), file=sys.stderr)
            return 1
    _safe_write_text(run_dir / "STOP", "stop")
    if args.kill:
        remaining = _terminate_marked_processes(run_dir)
        if remaining:
            print(json.dumps({
                "error": "could not stop active command tree",
                "run_dir": str(run_dir),
                "remaining_pids": sorted(remaining),
            }), file=sys.stderr)
            return 1
        current_pid = _locked_run_pid(run_dir)
        if current_pid is None:
            # Stopping the active command can let the orchestrator observe STOP
            # and finish normally. Its old PID is no longer authority to signal.
            print(json.dumps({"stopping": str(run_dir)}))
            return 0
        if current_pid != pid or not _pid_is_review_loop(current_pid, run_dir):
            print(json.dumps({
                "error": "run identity changed while stopping",
                "run_dir": str(run_dir),
            }), file=sys.stderr)
            return 1
        try:
            # The lock authenticates exactly one orchestrator PID, never its
            # shell/CI process group. Active command trees were drained above.
            os.kill(current_pid, signal.SIGTERM)
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
