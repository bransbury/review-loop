#!/usr/bin/env python3
"""Tests for review-loop's parsing and merge logic.

These cover the parts that decide whether the loop keeps going, which is where
a silent bug would be most expensive: a dropped finding looks identical to a
clean review.

Run with `python3 tests/test_core.py` — no pytest required.
"""

from __future__ import annotations

import json
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "review-loop" / "scripts"))

from review_loop import (  # noqa: E402
    GitError, STATE_DIRNAME, VALIDATION_FAILED, VALIDATION_NOT_CONFIGURED,
    VALIDATION_PASSED,
    ADAPTERS, Ledger, Run, dedupe, extract_json, invoke_agent, normalise_findings,
    validate_config,
    _keyword_hit, _signal_hit, _verdict, _write_final, acquire_lock, release_lock,
    collect_diff, base_commit, diff_note, preflight_repo, run_validation,
    _claude_argv, _codex_argv, _copilot_argv, _new_run_dir, _porcelain_entries,
    _extract_final_text, canonical_worktree_root, cmd_run, cmd_start, cmd_stop,
    cmd_suggest, exit_code,
    handoff_lock,
    resolve_config_repo, state_for_repo, worktree_fingerprint,
    _ensure_state_ignored,
    new_lock_token, review_problem, _locked_run_pid,
    _terminal_run_failure,
)


def finding(reviewer: str, **kw):
    base = {"id": "X-1", "severity": "medium", "file": "a.py", "line": 1,
            "category": "correctness", "problem": "p", "impact": "i",
            "recommended_fix": "f"}
    base.update(kw)
    return normalise_findings(reviewer, {"findings": [base]})


class ExtractJson(unittest.TestCase):
    """Unconstrained adapter output still needs defensive parsing."""

    def test_fenced_block_surrounded_by_prose(self):
        text = ('Here is my review.\n\n```json\n'
                '{"verdict":"changes_requested","findings":[]}\n```\nHope that helps!')
        self.assertEqual(extract_json(text)["verdict"], "changes_requested")

    def test_bare_object(self):
        self.assertEqual(extract_json('{"verdict":"approved","findings":[]}')["verdict"],
                         "approved")

    def test_unfenced_object_after_preamble(self):
        text = 'Sure thing:\n{"verdict":"approved","findings":[]}'
        self.assertEqual(extract_json(text)["verdict"], "approved")

    def test_last_block_wins_when_several(self):
        text = ('```json\n{"verdict":"approved","findings":[]}\n```\n'
                'Correction:\n```json\n{"verdict":"changes_requested","findings":[]}\n```')
        self.assertEqual(extract_json(text)["verdict"], "changes_requested")

    def test_rejects_garbage_rather_than_guessing(self):
        for bad in ("", "no json here", "{not json}", "```\nplain text\n```"):
            self.assertIsNone(extract_json(bad), bad)

    def test_ignores_unrelated_json(self):
        self.assertIsNone(extract_json('{"unrelated":true}'))


class Normalise(unittest.TestCase):
    def test_unknown_severity_becomes_medium(self):
        # A reviewer inventing "CRITICAL" must not silently drop out of the gate.
        out = normalise_findings("security", {"findings": [{"severity": "CRITICAL",
                                                            "file": "a.py", "problem": "x"}]})
        self.assertEqual(out[0]["severity"], "medium")

    def test_severity_case_and_whitespace_tolerated(self):
        out = normalise_findings("qa", {"findings": [{"severity": " High ", "file": "a.py"}]})
        self.assertEqual(out[0]["severity"], "high")

    def test_generates_id_when_missing(self):
        out = normalise_findings("security", {"findings": [{"file": "a.py"}]})
        self.assertEqual(out[0]["id"], "SECURITY-001")

    def test_handles_none_and_malformed_items(self):
        self.assertEqual(normalise_findings("qa", None), [])
        self.assertEqual(normalise_findings("qa", {"findings": ["nonsense", 42]}), [])

    def test_nits_are_dropped(self):
        # Policy discards them, so carrying them through costs tokens for nothing.
        out = normalise_findings("qa", {"findings": [
            {"severity": "nit", "file": "a.py", "problem": "rename this"},
            {"severity": "high", "file": "a.py", "problem": "real defect"}]})
        self.assertEqual([f["severity"] for f in out], ["high"])

    def test_findings_are_capped_most_severe_first(self):
        many = [{"severity": "low", "file": f"f{i}.py", "problem": f"p{i}"} for i in range(30)]
        many.append({"severity": "blocker", "file": "boom.py", "problem": "the real one"})
        out = normalise_findings("qa", {"findings": many}, max_findings=10)
        self.assertEqual(len(out), 10)
        # The blocker must survive the cap even though it arrived last.
        self.assertEqual(out[0]["severity"], "blocker")

    def test_long_fields_are_clipped_and_whitespace_collapsed(self):
        out = normalise_findings("qa", {"findings": [
            {"severity": "high", "file": "a.py", "problem": "x " * 800,
             "impact": "line one\n\n   line two"}]})
        self.assertLessEqual(len(out[0]["problem"]), 601)
        self.assertEqual(out[0]["impact"], "line one line two")


class Dedupe(unittest.TestCase):
    def test_merges_same_defect_described_differently(self):
        a = finding("architect", severity="medium", file="src/rl.ts", line=74,
                    category="race-condition",
                    problem="counter increment and expiry assignment happen independently")
        b = finding("adversarial-qa", severity="blocker", file="src/rl.ts", line=78,
                   category="race-condition",
                   problem="expiry assignment and counter increment are independent operations")
        merged = dedupe(a + b)
        self.assertEqual(len(merged), 1)
        # Highest severity wins, so the gate cannot be softened by a milder duplicate.
        self.assertEqual(merged[0]["severity"], "blocker")
        self.assertEqual(merged[0]["corroborated_by"], ["architect"])

    def test_distant_lines_are_separate_defects(self):
        a = finding("architect", file="src/rl.ts", line=74, problem="counter and expiry independent")
        b = finding("qa", file="src/rl.ts", line=400, problem="counter and expiry independent")
        self.assertEqual(len(dedupe(a + b)), 2)

    def test_different_files_never_merge(self):
        a = finding("architect", file="src/rl.ts", problem="identical problem text here")
        b = finding("qa", file="src/auth.ts", problem="identical problem text here")
        self.assertEqual(len(dedupe(a + b)), 2)

    def test_same_reviewer_does_not_corroborate_itself(self):
        a = finding("qa", file="a.py", line=1, problem="counter and expiry independent")
        b = finding("qa", file="a.py", line=2, problem="counter and expiry independent")
        merged = dedupe(a + b)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].get("corroborated_by", []), [])

    def test_unrelated_problems_in_same_file_are_kept(self):
        a = finding("architect", file="a.py", line=10,
                    problem="missing ownership verification before deletion")
        b = finding("qa", file="a.py", line=12,
                    problem="unbounded pagination loads entire table into memory")
        self.assertEqual(len(dedupe(a + b)), 2)

    def test_empty_input(self):
        self.assertEqual(dedupe([]), [])


class ValidateConfig(unittest.TestCase):
    """Config errors must surface before launch, not as a silent clean review."""

    def setUp(self):
        # Config-shape tests must not depend on which agent CLIs happen to be
        # installed on the machine running the suite. PATH validation has its
        # own explicit test below.
        cli_lookup = patch("review_loop.shutil.which",
                           side_effect=lambda binary: f"/usr/bin/{binary}")
        cli_lookup.start()
        self.addCleanup(cli_lookup.stop)

    def base(self, **over):
        cfg = {"task": "do a thing",
               "validation": {"commands": ["true"]},
               "implementer": {"cli": "claude", "model": "opus", "effort": "high"},
               "reviewers": [{"persona": "security", "cli": "claude", "effort": "max"}]}
        cfg.update(over)
        return cfg

    def test_valid_config_passes(self):
        self.assertEqual(validate_config(self.base()), [])

    def test_configured_cli_must_be_on_path(self):
        with patch("review_loop.shutil.which", return_value=None):
            errors = validate_config(self.base())
        self.assertTrue(any("`claude` is configured but not on PATH" in error
                            for error in errors))

    def test_no_reviewers_is_rejected(self):
        # Otherwise the loop finds zero blockers and reports "approved".
        self.assertTrue(any("reviewer" in e for e in validate_config(self.base(reviewers=[]))))

    def test_empty_task_is_rejected(self):
        self.assertTrue(any("task" in e for e in validate_config(self.base(task="   "))))

    def test_unknown_cli_is_rejected(self):
        cfg = self.base(reviewers=[{"persona": "security", "cli": "gemini"}])
        self.assertTrue(any("gemini" in e for e in validate_config(cfg)))

    def test_effort_must_be_supported_by_that_cli(self):
        # `ultra` exists on codex but not on claude.
        cfg = self.base(reviewers=[{"persona": "qa", "cli": "claude", "effort": "ultra"}])
        self.assertTrue(any("ultra" in e for e in validate_config(cfg)))

    def test_current_copilot_minimal_effort_is_supported(self):
        cfg = self.base(reviewers=[{"persona": "qa", "cli": "copilot",
                                    "effort": "minimal"}])
        self.assertEqual(validate_config(cfg), [])

    def test_unknown_severity_is_rejected(self):
        cfg = self.base(blocking_severities=["blocker", "urgent"])
        self.assertTrue(any("urgent" in e for e in validate_config(cfg)))

    def test_zero_iterations_is_rejected(self):
        self.assertTrue(any("max_iterations" in e
                            for e in validate_config(self.base(max_iterations=0))))

    def test_permission_mode_must_be_supported(self):
        cfg = self.base(permission_mode="auto")
        self.assertTrue(any("permission_mode" in e and "auto" in e
                            for e in validate_config(cfg)))

    def test_supported_permission_modes_pass(self):
        for mode in ("acceptEdits", "bypassPermissions"):
            with self.subTest(mode=mode):
                self.assertEqual(validate_config(self.base(permission_mode=mode)), [])

    def test_internal_handoff_fields_must_be_paired(self):
        for field, value in (("lock_token", "known"), ("run_dir", "/tmp/run-01")):
            with self.subTest(field=field):
                errors = validate_config(self.base(**{field: value}))
                self.assertTrue(any("internal handoff fields" in error
                                    for error in errors))

    def test_all_numeric_runtime_fields_are_range_checked(self):
        fields = {
            "max_iterations": 0,
            "agent_timeout_seconds": 0,
            "validation_timeout_seconds": 0,
            "max_log_chars": 3,
            "max_file_chars": 0,
            "max_untracked_files": -1,
            "max_diff_chars": 0,
        }
        for field, bad in fields.items():
            for value in (bad, "12", 1.5, True):
                with self.subTest(field=field, value=value):
                    self.assertTrue(any(field in error for error in
                                        validate_config(self.base(**{field: value}))))

    def test_numeric_runtime_fields_have_safe_upper_bounds(self):
        for field in ("max_iterations", "agent_timeout_seconds",
                      "validation_timeout_seconds", "max_log_chars",
                      "max_file_chars", "max_untracked_files", "max_diff_chars"):
            with self.subTest(field=field):
                errors = validate_config(self.base(**{field: 10 ** 1000}))
                self.assertTrue(any(field in error and "at most" in error
                                    for error in errors))

    def test_panel_command_and_input_counts_are_bounded(self):
        reviewers = [{"persona": f"qa-{i}", "cli": "claude"} for i in range(9)]
        self.assertTrue(any("At most 8 reviewers" in error for error in
                            validate_config(self.base(reviewers=reviewers))))
        commands = ["true"] * 33
        self.assertTrue(any("At most 32 validation" in error for error in
                            validate_config(self.base(validation={"commands": commands}))))
        copilot = self.base(reviewers=[{"persona": "qa", "cli": "copilot"}],
                            max_diff_chars=400_001)
        self.assertTrue(any("Copilot" in error and "400000" in error
                            for error in validate_config(copilot)))

    def test_blocking_policy_is_an_upward_closed_threshold(self):
        accepted = (["blocker"], ["blocker", "high"],
                    ["blocker", "high", "medium"],
                    ["blocker", "high", "medium", "low"])
        for severities in accepted:
            self.assertEqual(validate_config(self.base(blocking_severities=severities)), [])
        for severities in ([], ["nit"], ["high"], ["blocker", "medium"]):
            self.assertTrue(any("blocking_severities" in error for error in
                                validate_config(self.base(blocking_severities=severities))))

    def test_runtime_booleans_must_really_be_booleans(self):
        for field in ("allow_dirty", "allow_non_git", "allow_missing_validation",
                      "require_clean_baseline", "parallel", "exclude_noise"):
            with self.subTest(field=field):
                self.assertTrue(any(field in error for error in
                                    validate_config(self.base(**{field: 1}))))

    def test_other_runtime_shapes_are_checked(self):
        cases = [
            ({"task": {"not": "text"}}, "task"),
            ({"repo": []}, "repo"),
            ({"blocking_severities": "high"}, "blocking_severities"),
            ({"exclude_paths": "dist"}, "exclude_paths"),
            ({"reviewers": [{"persona": "../escape", "cli": "claude"}]}, "persona"),
            ({"reviewers": [{"persona": "security", "cli": "claude",
                              "label": 7}]}, "label"),
        ]
        for overrides, field in cases:
            with self.subTest(field=field):
                self.assertTrue(any(field in error for error in
                                    validate_config(self.base(**overrides))))

    def test_duplicate_personas_get_distinct_slot_ids(self):
        # Same persona on two models is a legitimate panel; their outputs must
        # not overwrite each other.
        cfg = self.base(reviewers=[{"persona": "security", "cli": "claude"},
                                   {"persona": "security", "cli": "codex"}])
        self.assertEqual(validate_config(cfg), [])
        self.assertEqual([r["slot_id"] for r in cfg["reviewers"]],
                         ["security", "security-2"])

    def test_duplicate_slots_can_corroborate_each_other(self):
        cfg = self.base(reviewers=[{"persona": "security", "cli": "claude"},
                                   {"persona": "security", "cli": "codex"}])
        validate_config(cfg)
        ids = [r["slot_id"] for r in cfg["reviewers"]]
        a = finding(ids[0], file="a.py", line=1, problem="missing ownership check on delete")
        b = finding(ids[1], file="a.py", line=3, problem="ownership check missing before delete")
        merged = dedupe(a + b)
        self.assertEqual(len(merged), 1)
        # The first slot's finding is kept; the second corroborates it.
        self.assertEqual(merged[0]["reviewer"], ids[0])
        self.assertEqual(merged[0]["corroborated_by"], [ids[1]])

    def test_label_defaults_from_persona(self):
        cfg = self.base()
        validate_config(cfg)
        self.assertEqual(cfg["reviewers"][0]["label"], "Security Engineer")

    def test_validation_is_idempotent(self):
        # `start` validates, writes the resolved config, and the detached `run`
        # validates it again — labels used to gain a suffix each time.
        cfg = self.base(reviewers=[{"persona": "security", "cli": "claude"},
                                   {"persona": "security", "cli": "codex"}])
        validate_config(cfg)
        first = json.dumps(cfg, sort_keys=True)
        validate_config(cfg)
        self.assertEqual(json.dumps(cfg, sort_keys=True), first)
        self.assertEqual([r["label"] for r in cfg["reviewers"]],
                         ["Security Engineer", "Security Engineer #2"])

    def test_user_supplied_label_is_preserved_across_validations(self):
        cfg = self.base(reviewers=[{"persona": "security", "cli": "claude",
                                    "label": "My Reviewer"}])
        validate_config(cfg)
        validate_config(cfg)
        self.assertEqual(cfg["reviewers"][0]["label"], "My Reviewer")

    def test_missing_validation_is_rejected(self):
        # Without commands there is no independent gate, and "nothing ran" was
        # previously reported as "validation passed".
        cfg = self.base(validation={"commands": []})
        self.assertTrue(any("validation" in e for e in validate_config(cfg)))

    def test_missing_validation_can_be_opted_into_explicitly(self):
        cfg = self.base(validation={"commands": []}, allow_missing_validation=True)
        self.assertEqual(validate_config(cfg), [])

    def test_validation_commands_must_be_a_list(self):
        cfg = self.base(validation={"commands": "python3 tests/test_core.py"})
        self.assertTrue(any("commands` must be a list" in e
                            for e in validate_config(cfg)))

    def test_validation_commands_cannot_contain_shell_noops(self):
        # An empty command exits zero in the shell, so accepting it would turn
        # an independent gate that ran nothing into a passing one.
        for bad in ([""], ["   "], [None]):
            cfg = self.base(validation={"commands": bad})
            self.assertTrue(any("empty or non-string" in e
                                for e in validate_config(cfg)), bad)

    def test_validation_must_be_an_object(self):
        for bad in ("true", [], ""):
            cfg = self.base(validation=bad, allow_missing_validation=True)
            self.assertTrue(any("must be an object" in e
                                for e in validate_config(cfg)), bad)

    def test_falsey_non_list_commands_are_not_treated_as_missing(self):
        for bad in ("", None):
            cfg = self.base(validation={"commands": bad},
                            allow_missing_validation=True)
            self.assertTrue(any("commands` must be a list" in e
                                for e in validate_config(cfg)), bad)


class PanelRouting(unittest.TestCase):
    """Signals decide which reviewers get suggested. A false positive wastes a
    reviewer slot; a false negative silently drops the one that mattered."""

    def test_whole_word_by_default(self):
        # The bug this exists for: "log" firing on "login" and putting the
        # observability reviewer on every auth task.
        self.assertFalse(_keyword_hit("log", "add sso login support"))
        self.assertTrue(_keyword_hit("log", "write a log line"))
        self.assertTrue(_keyword_hit("login", "add sso login support"))

    def test_star_suffix_matches_stems(self):
        self.assertTrue(_keyword_hit("optimi*", "optimise the query"))
        self.assertTrue(_keyword_hit("optimi*", "optimization pass"))
        self.assertTrue(_keyword_hit("slow*", "it loads slowly"))
        self.assertFalse(_keyword_hit("slow*", "unrelated text"))

    def test_empty_keyword_never_matches(self):
        self.assertFalse(_keyword_hit("", "anything at all"))

    def test_signal_matches_whole_path_segment(self):
        paths = ["src/security/guard.ts", "docs/readme.md"]
        self.assertTrue(_signal_hit("security", paths))
        # Must not fire on a file that merely contains the word in its name.
        self.assertFalse(_signal_hit("security", ["personas/security.md"]))

    def test_signal_extension_matches_suffix(self):
        self.assertTrue(_signal_hit(".sql", ["db/migrations/001_init.sql"]))
        self.assertFalse(_signal_hit(".sql", ["db/sqlhelper.py"]))

    def test_empty_signal_never_matches(self):
        self.assertFalse(_signal_hit("", ["a/b/c.py"]))


class DiffCollection(unittest.TestCase):
    """The diff is sent to every reviewer on every round, so its size is
    multiplied by panel size and round count. Excluding the wrong thing is
    worse than the waste, so both directions are checked."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self._git("init")
        self._git("config", "user.email", "t@t.co")
        self._git("config", "user.name", "T")
        (self.repo / "app.py").write_text("print(1)\n")
        (self.repo / "package-lock.json").write_text("{}\n")
        (self.repo / "dist").mkdir()
        (self.repo / "dist" / "bundle.js").write_text("x\n")
        self._git("add", "-A")
        self._git("commit", "-m", "init")

    def _git(self, *args):
        import subprocess
        subprocess.run(["git", *args], cwd=self.repo, capture_output=True)

    def test_real_changes_survive_and_noise_is_dropped(self):
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        (self.repo / "package-lock.json").write_text('{"deps": %s}\n' % ("x" * 40000))
        (self.repo / "dist" / "bundle.js").write_text("var x=1;" * 3000)

        diff, man = collect_diff(self.repo, base_commit(self.repo), {})
        self.assertIn("def add(a, b)", diff)          # the change under review
        self.assertNotIn("package-lock", diff)        # lockfile noise
        self.assertNotIn("bundle.js", diff)           # build output
        self.assertLess(man["chars"], 2000)

    def test_exclusions_can_be_turned_off(self):
        (self.repo / "package-lock.json").write_text('{"deps": %s}\n' % ("x" * 5000))
        diff, _ = collect_diff(self.repo, base_commit(self.repo), {"exclude_noise": False})
        self.assertIn("package-lock", diff)

    def test_large_file_is_truncated_not_dropped(self):
        (self.repo / "app.py").write_text("# line\n" * 20000)
        diff, man = collect_diff(self.repo, base_commit(self.repo), {"max_file_chars": 5000})
        self.assertTrue(man["truncated_files"])
        self.assertIn("truncated at 5000", diff)
        # The reviewer must be told, or it reviews a partial picture unknowingly.
        self.assertIn("app.py", diff_note(man, {}))

    def test_sections_are_newline_separated(self):
        (self.repo / "app.py").write_text("x = 1")   # no trailing newline
        (self.repo / "new.py").write_text("y = 2\n")
        diff, _ = collect_diff(self.repo, base_commit(self.repo), {})
        self.assertNotIn("x = 1---", diff)

    def test_custom_exclusions_are_honoured(self):
        (self.repo / "app.py").write_text("changed\n")
        diff, _ = collect_diff(self.repo, base_commit(self.repo),
                               {"exclude_paths": ["app.py"]})
        self.assertNotIn("changed", diff)

    def test_untracked_files_past_the_cap_are_named_not_dropped(self):
        # The "never silently partial" guarantee: files past the cap were
        # counted in the manifest but absent from the payload and the note.
        for i in range(5):
            (self.repo / f"new{i}.py").write_text(f"x = {i}\n")
        diff, man = collect_diff(self.repo, base_commit(self.repo),
                                 {"max_untracked_files": 2})
        self.assertEqual(man["untracked_files"], 5)
        self.assertEqual(man["untracked_included"], 2)
        self.assertEqual(len(man["omitted_files"]), 3)
        for p in man["omitted_files"]:
            self.assertIn(p, diff)                       # named in the payload
        self.assertIn("3 new files", diff_note(man, {}))  # and to the reviewer

    def test_untracked_unicode_and_control_character_paths_are_inlined(self):
        names = ["café.py", "tab\tname.py", "line\nbreak.py"]
        for name in names:
            (self.repo / name).write_text(f"content:{name!r}\n")
        diff, manifest = collect_diff(self.repo, base_commit(self.repo), {})
        self.assertEqual(manifest["untracked_files"], len(names))
        for name in names:
            self.assertIn(f"--- new file: {name} ---", diff)
            self.assertIn(f"content:{name!r}", diff)

    def test_untracked_symlink_is_not_followed_outside_the_repo(self):
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("DO-NOT-EXFILTRATE\n")
        (self.repo / "linked-secret").symlink_to(outside)

        diff, _ = collect_diff(self.repo, base_commit(self.repo), {})

        self.assertIn("new symlink: linked-secret", diff)
        self.assertNotIn("DO-NOT-EXFILTRATE", diff)

    def test_repository_named_state_directory_is_user_content_and_is_reviewed(self):
        state = self.repo / STATE_DIRNAME
        state.mkdir()
        (state / "task.md").write_text("the task\n")
        diff, _ = collect_diff(self.repo, base_commit(self.repo), {})
        self.assertIn("the task", diff)

    def test_state_prefix_sibling_is_still_reviewed(self):
        (self.repo / ".review-loop-config").write_text("important = true\n")
        diff, man = collect_diff(self.repo, base_commit(self.repo), {})
        self.assertIn(".review-loop-config", diff)
        self.assertEqual(man["untracked_files"], 1)

    def test_git_diff_failure_is_not_mistaken_for_an_empty_diff(self):
        with patch("review_loop.git", side_effect=GitError("diff exploded")):
            with self.assertRaisesRegex(GitError, "diff exploded"):
                collect_diff(self.repo, "abc123", {})

    def test_unborn_repository_includes_staged_index_changes(self):
        repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init"], cwd=repo, check=True,
                       capture_output=True)
        (repo / "staged.txt").write_text("must be reviewed\n")
        subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True,
                       capture_output=True)
        self.assertIsNone(base_commit(repo))
        diff, manifest = collect_diff(repo, None, {})
        self.assertIn("staged.txt", diff)
        self.assertIn("must be reviewed", diff)
        self.assertGreaterEqual(manifest["files"], 1)


class WriteFinal(unittest.TestCase):
    """The report is written once, at the end, after everything expensive has
    already run. A crash here throws away the whole run, so every branch of it
    is exercised."""

    BLOCKING = {"blocker", "high", "medium"}

    def _write(self, findings, outcome="approved", validation=VALIDATION_PASSED,
               ledger=None, **kw):
        tmp = Path(tempfile.mkdtemp())
        (tmp / STATE_DIRNAME).mkdir()
        run_dir = tmp / STATE_DIRNAME / "history" / "run-01"
        run_dir.mkdir(parents=True)
        run = Run(tmp, run_dir, {"reviewers": [{"persona": "security",
                                                "label": "Security Engineer"}]})
        if ledger is None:
            ledger = Ledger()
            ledger.record_round(1, findings)
        path = _write_final(run, "abc123", outcome, ledger, 2, validation,
                            self.BLOCKING, **kw)
        return path.read_text()

    def test_blocking_and_advisory_are_separated(self):
        body = self._write(finding("qa", severity="high", file="a.py", problem="blocking one")
                           + finding("qa", severity="low", file="b.py", problem="advisory one"))
        self.assertIn("Blocking findings still open", body)
        self.assertIn("Advisory findings", body)
        self.assertIn("Blocking findings open: 1", body)
        self.assertIn("Advisory findings open: 1", body)

    def test_advisory_only(self):
        # This path crashed once: the section helper rebound its closure variable.
        body = self._write(finding("qa", severity="low", file="b.py", problem="advisory only"))
        self.assertIn("Advisory findings", body)
        self.assertNotIn("Blocking findings still open", body)

    def test_blocking_only(self):
        body = self._write(finding("qa", severity="blocker", file="a.py", problem="bad"))
        self.assertIn("Blocking findings still open", body)
        self.assertNotIn("## Advisory findings", body)

    def test_no_findings(self):
        body = self._write([])
        self.assertIn("None raised in any round.", body)

    def test_records_corroboration_and_failing_validation(self):
        f = finding("qa", severity="high", file="a.py", problem="x")
        f[0]["corroborated_by"] = ["architect"]
        body = self._write(f, outcome="max_iterations_reached",
                           validation=VALIDATION_FAILED)
        self.assertIn("independently raised by architect", body)
        self.assertIn("Validation: FAILING", body)
        self.assertIn("needs a human", body)

    def test_unconfigured_validation_never_reads_as_passing(self):
        body = self._write([], validation=VALIDATION_NOT_CONFIGURED)
        self.assertIn("NOT CONFIGURED", body)
        self.assertNotIn("Validation: passing", body)

    def test_missing_reviewers_are_called_out(self):
        body = self._write([], outcome="review_incomplete",
                           missing_reviewers=["security"])
        self.assertIn("Panel incomplete", body)
        self.assertIn("security", body)

    def test_resolved_findings_survive_into_the_report(self):
        led = Ledger()
        led.record_round(1, finding("qa", severity="high", file="a.py",
                                    problem="unchecked ownership before delete"))
        led.record_round(2, [])
        body = self._write([], ledger=led)
        self.assertIn("Findings raised across all rounds: 1", body)
        self.assertIn("Findings resolved: 1", body)
        self.assertIn("Findings raised and resolved", body)
        self.assertIn("resolved in round 2", body)

    def test_every_outcome_renders(self):
        for outcome in ("approved", "approved_unverified", "review_incomplete",
                        "validation_not_configured", "baseline_failed",
                        "max_iterations_reached", "no_progress",
                        "stopped_by_user", "implementer_failed", "unexpected_value"):
            body = self._write([], outcome=outcome)
            self.assertIn(f"**Outcome: {outcome}**", body)


class Verdicts(unittest.TestCase):
    """A reviewer that did not clearly approve must not be read as approving."""

    def test_explicit_approval(self):
        self.assertEqual(_verdict({"verdict": "approved"}), "approved")
        self.assertEqual(_verdict({"verdict": " Approved "}), "approved")

    def test_absent_verdict_is_not_an_approval_claim(self):
        # Empty string means "not stated"; the findings list speaks instead.
        self.assertEqual(_verdict({"findings": []}), "")
        self.assertEqual(_verdict({"verdict": ""}), "")

    def test_anything_else_requests_changes(self):
        for raw in ("changes_requested", "CHANGES_REQUESTED", "needs work", "reject"):
            self.assertEqual(_verdict({"verdict": raw}), "changes_requested", raw)


class ValidationTriState(unittest.TestCase):
    """"Nothing ran" and "the suite passed" are different facts. Collapsing
    them into True let an unverified run report as approved."""

    def _run(self, commands):
        tmp = Path(tempfile.mkdtemp())
        run_dir = tmp / "run"
        run_dir.mkdir()
        return run_validation(Run(tmp, run_dir, {"validation": {"commands": commands}}))

    def test_no_commands_is_not_configured(self):
        status, results = self._run([])
        self.assertEqual(status, VALIDATION_NOT_CONFIGURED)
        self.assertEqual(results, [])

    def test_passing_command(self):
        status, results = self._run(["exit 0"])
        self.assertEqual(status, VALIDATION_PASSED)
        self.assertTrue(results[0]["passed"])

    def test_one_failure_fails_the_gate(self):
        status, _ = self._run(["exit 0", "exit 1"])
        self.assertEqual(status, VALIDATION_FAILED)


class ProcessTreeTimeouts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def _script(self, marker):
        pid_file = self.tmp / f"{marker}.pid"
        stopped_file = self.tmp / f"{marker}.stopped"
        script = self.tmp / f"{marker}.py"
        child_code = (
            "import signal,sys,time; from pathlib import Path; "
            f"p=Path({str(stopped_file)!r}); "
            "signal.signal(signal.SIGTERM, lambda *_: (p.write_text('stopped'), "
            "sys.exit(0))); time.sleep(60)"
        )
        script.write_text(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            f"print('partial-{marker}', flush=True)\n"
            "time.sleep(60)\n"
        )
        return script, pid_file, stopped_file

    def _assert_descendant_stopped(self, stopped_file):
        deadline = time.time() + 3
        while time.time() < deadline:
            if stopped_file.exists():
                return
            time.sleep(0.05)
        self.fail("descendant did not receive the process-group termination signal")

    def _assert_pid_gone(self, pid):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"descendant {pid} is still alive")

    def test_validation_timeout_kills_descendants_and_keeps_partial_output(self):
        script, _pid_file, stopped_file = self._script("validation")
        config = {"validation": {"commands": [f"{sys.executable} {script}"]},
                  "validation_timeout_seconds": 1}
        status, results = run_validation(Run(self.tmp, self.run_dir, config))
        self.assertEqual(status, VALIDATION_FAILED)
        self.assertIn("partial-validation", results[0]["output_tail"])
        self.assertIn("timed out after 1s", results[0]["output_tail"])
        self._assert_descendant_stopped(stopped_file)

    def test_agent_timeout_kills_descendants_and_keeps_partial_log(self):
        script, _pid_file, stopped_file = self._script("agent")
        config = {"permission_mode": "acceptEdits", "agent_timeout_seconds": 1,
                  "max_log_chars": 10000}
        run = Run(self.tmp, self.run_dir, config)
        adapter = dict(ADAPTERS["claude"])
        adapter.update({
            "bin": Path(sys.executable).name,
            "argv": lambda *_args: ([sys.executable, str(script)], None),
            "reads_out_file": False,
            "config_dir_env": None,
        })
        with patch.dict(ADAPTERS, {"claude": adapter}):
            ok, message = invoke_agent(
                run, {"cli": "claude"}, "prompt", False, "Agent", "timeout")
        self.assertFalse(ok)
        self.assertIn("timed out after 1s", message)
        self.assertIn("partial-agent", (self.run_dir / "logs/timeout.log").read_text())
        self._assert_descendant_stopped(stopped_file)

    def test_ignore_term_descendant_with_closed_pipes_is_still_killed(self):
        heartbeat = self.tmp / "heartbeat"
        pid_file = self.tmp / "ignored.pid"
        child_code = (
            "import signal,time\n"
            "from pathlib import Path\n"
            f"p=Path({str(heartbeat)!r})\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "i=0\n"
            "while True:\n"
            " i += 1\n"
            " p.write_text(str(i))\n"
            " time.sleep(0.05)\n"
        )
        parent = self.tmp / "ignore_parent.py"
        parent.write_text(
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "print('partial-ignore', flush=True)\n"
            "time.sleep(60)\n"
        )
        config = {"validation": {"commands": [f"{sys.executable} {parent}"]},
                  "validation_timeout_seconds": 1}
        status, results = run_validation(Run(self.tmp, self.run_dir, config))
        self.assertEqual(status, VALIDATION_FAILED)
        self.assertIn("partial-ignore", results[0]["output_tail"])
        before = heartbeat.read_text()
        time.sleep(0.25)
        after = heartbeat.read_text()
        pid = int(pid_file.read_text())
        try:
            self.assertEqual(before, after, f"descendant {pid} kept running after timeout")
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def test_setsid_descendant_is_tracked_and_killed(self):
        heartbeat = self.tmp / "setsid-heartbeat"
        pid_file = self.tmp / "setsid.pid"
        child_code = (
            "import os,signal,time\n"
            "from pathlib import Path\n"
            "os.setsid()\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"p=Path({str(heartbeat)!r})\n"
            "i=0\n"
            "while True:\n"
            " i += 1\n"
            " p.write_text(str(i))\n"
            " time.sleep(0.05)\n"
        )
        parent = self.tmp / "setsid_parent.py"
        parent.write_text(
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "print('partial-setsid', flush=True)\n"
            "time.sleep(60)\n"
        )
        config = {"validation": {"commands": [f"{sys.executable} {parent}"]},
                  "validation_timeout_seconds": 1}
        status, results = run_validation(Run(self.tmp, self.run_dir, config))
        self.assertEqual(status, VALIDATION_FAILED)
        self.assertIn("partial-setsid", results[0]["output_tail"])
        before = heartbeat.read_text()
        time.sleep(0.25)
        after = heartbeat.read_text()
        pid = int(pid_file.read_text())
        try:
            self.assertEqual(before, after, f"setsid descendant {pid} survived timeout")
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def test_sanitized_setsid_descendant_is_killed_without_environment_marker(self):
        pid_file = self.tmp / "sanitized.pid"
        ready = self.tmp / "sanitized.ready"
        child_code = (
            "import os,time\n"
            "from pathlib import Path\n"
            "os.setsid()\n"
            f"Path({str(ready)!r}).write_text('ready')\n"
            "while True: time.sleep(1)\n"
        )
        parent = self.tmp / "sanitized_parent.py"
        parent.write_text(
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "env={}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            f"deadline=time.time()+3\n"
            f"while not Path({str(ready)!r}).exists() and time.time()<deadline: time.sleep(.01)\n"
            "print('stdout-before-timeout', flush=True)\n"
            "print('stderr-before-timeout', file=sys.stderr, flush=True)\n"
            "time.sleep(60)\n"
        )
        config = {"validation": {"commands": [f"{sys.executable} {parent}"]},
                  "validation_timeout_seconds": 1, "max_log_chars": 10000}
        status, results = run_validation(Run(self.tmp, self.run_dir, config))
        self.assertEqual(status, VALIDATION_FAILED)
        self.assertIn("stdout-before-timeout", results[0]["output_tail"])
        self.assertIn("stderr-before-timeout", results[0]["output_tail"])
        self.assertTrue(ready.exists(), "child-ready handshake did not complete")
        child_pid = int(pid_file.read_text())
        records = [json.loads(line) for line in
                   (self.run_dir / "processes.jsonl").read_text().splitlines()]
        self.assertIn(child_pid, [record["pid"] for record in records])
        self._assert_pid_gone(child_pid)

    def test_invalid_utf8_is_replaced_and_output_budget_is_enforced(self):
        invalid = self.tmp / "invalid.py"
        invalid.write_text("import os; os.write(1, b'good\\xfftail')\n")
        status, results = run_validation(Run(
            self.tmp, self.run_dir,
            {"validation": {"commands": [f"{sys.executable} {invalid}"]},
             "max_log_chars": 1000}))
        self.assertEqual(status, VALIDATION_PASSED)
        self.assertIn("good", results[0]["output_tail"])
        self.assertIn("tail", results[0]["output_tail"])

        noisy = self.tmp / "noisy.py"
        noisy.write_text("print('x' * 100000)\n")
        status, results = run_validation(Run(
            self.tmp, self.run_dir,
            {"validation": {"commands": [f"{sys.executable} {noisy}"]},
             "max_log_chars": 1000}))
        self.assertEqual(status, VALIDATION_FAILED)
        self.assertLess(len(results[0]["output_tail"]), 5000)
        self.assertIn("safety limit", results[0]["output_tail"])


class LedgerHistory(unittest.TestCase):
    """The loop re-reviews from scratch, so the last round is a snapshot. The
    ledger is what makes "raised", "resolved" and "came back" answerable."""

    def test_finding_resolved_when_a_complete_panel_stops_reporting_it(self):
        led = Ledger()
        led.record_round(1, finding("qa", file="a.py", problem="unbounded pagination query"))
        led.record_round(2, [])
        self.assertEqual([e["state"] for e in led.entries], ["resolved"])
        self.assertEqual(led.entries[0]["resolved_in"], 2)
        self.assertEqual(led.open_findings(), [])

    def test_reworded_finding_is_the_same_defect_not_a_new_one(self):
        led = Ledger()
        led.record_round(1, finding("qa", file="a.py", line=10,
                                    problem="pagination loads entire table into memory"))
        led.record_round(2, finding("qa", file="a.py", line=12,
                                    problem="entire table loaded into memory by pagination"))
        self.assertEqual(len(led.entries), 1)
        self.assertEqual(led.entries[0]["rounds_seen"], [1, 2])

    def test_a_fix_that_did_not_hold_is_marked_reappeared(self):
        led = Ledger()
        f = finding("qa", file="a.py", problem="unbounded pagination query")
        led.record_round(1, f)
        led.record_round(2, [])
        led.record_round(3, f)
        self.assertEqual(led.entries[0]["state"], "reappeared")
        self.assertNotIn("resolved_in", led.entries[0])

    def test_low_severity_findings_are_not_lost_after_a_later_round(self):
        led = Ledger()
        led.record_round(1, finding("qa", severity="low", file="a.py",
                                    problem="confusing variable naming here"))
        led.record_round(2, finding("qa", severity="high", file="b.py",
                                    problem="missing ownership verification"))
        self.assertEqual(len(led.entries), 2)

    def test_incomplete_panel_never_resolves_anything(self):
        # Silence from a reviewer that never answered is not evidence of a fix.
        led = Ledger()
        led.record_round(1, finding("qa", file="a.py", problem="unbounded pagination query"))
        led.record_round(2, [], panel_complete=False)
        self.assertEqual(led.entries[0]["state"], "open")

    def test_severity_escalation_is_kept(self):
        led = Ledger()
        led.record_round(1, finding("qa", severity="medium", file="a.py",
                                    problem="missing ownership verification on delete"))
        led.record_round(2, finding("arch", severity="blocker", file="a.py",
                                    problem="ownership verification missing before delete"))
        self.assertEqual(led.entries[0]["severity"], "blocker")
        self.assertEqual(led.entries[0]["corroborated_by"], ["arch"])


class ReadOnlyEnforcement(unittest.TestCase):
    """Reviewers must be read-only by flag, not by request in a prompt."""

    def test_copilot_denies_the_shell_outright(self):
        # Copilot's `write` permission explicitly excludes shell invocations, so
        # denying named commands left `sed -i`, `rm` and redirection allowed.
        argv, _ = _copilot_argv("p", "auto", "high", True, "acceptEdits", Path("/tmp/o"))
        pairs = list(zip(argv, argv[1:]))
        self.assertIn(("--deny-tool", "shell"), pairs)
        self.assertIn(("--deny-tool", "write"), pairs)
        self.assertNotIn("shell(git commit)", argv)
        self.assertNotIn("--allow-all-tools", argv)
        self.assertIn("--available-tools=view,grep,glob", argv)
        self.assertIn("--disable-builtin-mcps", argv)
        self.assertIn("--no-custom-instructions", argv)

    def test_copilot_accept_edits_denies_shell_but_allows_file_tools(self):
        argv, _ = _copilot_argv("p", "auto", "high", False, "acceptEdits", Path("/tmp/o"))
        self.assertIn(("--allow-tool", "write"), list(zip(argv, argv[1:])))
        self.assertIn(("--deny-tool", "shell"), list(zip(argv, argv[1:])))
        self.assertNotIn(("--deny-tool", "write"), list(zip(argv, argv[1:])))
        self.assertNotIn("--allow-all-tools", argv)

    def test_copilot_bypass_really_allows_every_permission_class(self):
        argv, _ = _copilot_argv("p", "auto", "high", False,
                                "bypassPermissions", Path("/tmp/o"))
        self.assertIn("--allow-all", argv)
        self.assertNotIn("--deny-tool", argv)

    def test_claude_maps_both_native_permission_modes(self):
        for mode in ("acceptEdits", "bypassPermissions"):
            with self.subTest(mode=mode):
                argv, _ = _claude_argv("p", "opus", "high", False, mode,
                                       Path("/tmp/o"))
                self.assertIn(("--permission-mode", mode), list(zip(argv, argv[1:])))

    def test_claude_uses_inline_schema_when_the_cli_supports_it(self):
        with patch("review_loop._claude_supports_json_schema", return_value=True):
            argv, _ = _claude_argv("p", "opus", "high", True,
                                   "acceptEdits", Path("/tmp/o"))
        index = argv.index("--json-schema")
        schema = json.loads(argv[index + 1])
        self.assertEqual(schema["required"], ["verdict", "findings"])
        self.assertNotIn("_comment", json.dumps(schema))

    def test_claude_older_cli_falls_back_without_schema_flag(self):
        with patch("review_loop._claude_supports_json_schema", return_value=False):
            argv, _ = _claude_argv("p", "opus", "high", True,
                                   "acceptEdits", Path("/tmp/o"))
        self.assertNotIn("--json-schema", argv)

    def test_claude_reviewer_disables_project_hooks_and_customizations(self):
        with patch("review_loop._claude_supports_safe_mode", return_value=True):
            argv, _ = _claude_argv("p", "opus", "high", True,
                                   "acceptEdits", Path("/tmp/o"))
        self.assertIn("--safe-mode", argv)
        with patch("review_loop._claude_supports_safe_mode", return_value=False):
            argv, _ = _claude_argv("p", "opus", "high", True,
                                   "acceptEdits", Path("/tmp/o"))
        self.assertIn(("--setting-sources", ""), list(zip(argv, argv[1:])))

    def test_claude_structured_output_is_extracted_from_json_envelope(self):
        output = json.dumps({"result": "fallback", "structured_output": {
            "verdict": "approved", "findings": []}})
        self.assertEqual(json.loads(_extract_final_text("claude", output)),
                         {"verdict": "approved", "findings": []})

    def test_codex_maps_accept_edits_to_workspace_sandbox(self):
        argv, _ = _codex_argv("p", "gpt", "high", False,
                              "acceptEdits", Path("/tmp/o"))
        self.assertIn(("-s", "workspace-write"), list(zip(argv, argv[1:])))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_maps_bypass_to_no_sandbox(self):
        argv, _ = _codex_argv("p", "gpt", "high", False,
                              "bypassPermissions", Path("/tmp/o"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("workspace-write", argv)

    def test_readonly_overrides_bypass_for_every_adapter(self):
        cases = [
            (_claude_argv, ("--permission-mode", "plan"), "bypassPermissions"),
            (_copilot_argv, ("--deny-tool", "write"), "--allow-all"),
            (_codex_argv, ("-s", "read-only"),
             "--dangerously-bypass-approvals-and-sandbox"),
        ]
        for adapter, required, forbidden in cases:
            with self.subTest(adapter=adapter.__name__):
                argv, _ = adapter("p", "model", "high", True,
                                  "bypassPermissions", Path("/tmp/o"))
                self.assertIn(required, list(zip(argv, argv[1:])))
                self.assertNotIn(forbidden, argv)


class RepoSafety(unittest.TestCase):
    """The script is driven directly as often as through the skill, so these
    guarantees cannot live in a host prompt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _git(self, *args, cwd=None):
        import subprocess
        subprocess.run(["git", *args], cwd=cwd or self.tmp, capture_output=True)

    def _repo(self):
        self._git("init")
        self._git("config", "user.email", "t@t.co")
        self._git("config", "user.name", "T")
        (self.tmp / "a.py").write_text("x = 1\n")
        self._git("add", "-A")
        self._git("commit", "-m", "init")

    def test_non_git_directory_is_refused(self):
        problems = preflight_repo(self.tmp, {})
        self.assertTrue(any("not a git working tree" in p for p in problems))

    def test_non_git_can_be_overridden(self):
        self.assertEqual(preflight_repo(self.tmp, {"allow_non_git": True}), [])

    def test_suggest_works_before_non_git_override_is_configured(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cmd_suggest(types.SimpleNamespace(repo=str(self.tmp), task="secure parser"))
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["repo"], str(self.tmp.resolve()))
        self.assertTrue(result["suggested"])

    def test_dirty_tree_is_refused_and_names_the_files(self):
        self._repo()
        (self.tmp / "a.py").write_text("x = 2\n")
        problems = preflight_repo(self.tmp, {})
        self.assertTrue(any("dirty" in p and "a.py" in p for p in problems))

    def test_dirty_tree_can_be_overridden(self):
        self._repo()
        (self.tmp / "a.py").write_text("x = 2\n")
        self.assertEqual(preflight_repo(self.tmp, {"allow_dirty": True}), [])

    def test_clean_tree_passes(self):
        self._repo()
        self.assertEqual(preflight_repo(self.tmp, {}), [])

    def test_ignored_state_directory_does_not_count_as_dirty(self):
        self._repo()
        (self.tmp / ".gitignore").write_text(f"{STATE_DIRNAME}/\n")
        self._git("add", "-A")
        self._git("commit", "-m", "ignore")
        (self.tmp / STATE_DIRNAME).mkdir()
        (self.tmp / STATE_DIRNAME / "task.md").write_text("task\n")
        self.assertEqual(preflight_repo(self.tmp, {}), [])

    def test_subdirectories_resolve_to_the_same_worktree_root(self):
        self._repo()
        one = self.tmp / "packages" / "one"
        two = self.tmp / "packages" / "two"
        one.mkdir(parents=True)
        two.mkdir(parents=True)
        self.assertEqual(canonical_worktree_root(one), self.tmp.resolve())
        self.assertEqual(canonical_worktree_root(two), self.tmp.resolve())

    def test_config_repo_is_rewritten_to_the_worktree_root(self):
        self._repo()
        subdir = self.tmp / "src" / "nested"
        subdir.mkdir(parents=True)
        cfg = {"repo": str(subdir)}
        root, problems = resolve_config_repo(cfg)
        self.assertEqual(problems, [])
        self.assertEqual(root, self.tmp.resolve())
        self.assertEqual(cfg["repo"], str(self.tmp.resolve()))

    def test_non_git_override_preserves_the_configured_root(self):
        subdir = self.tmp / "scope"
        subdir.mkdir()
        cfg = {"repo": str(subdir), "allow_non_git": True}
        root, problems = resolve_config_repo(cfg)
        self.assertEqual(problems, [])
        self.assertEqual(root, subdir.resolve())

    def test_subdirectory_configs_share_one_state_and_lock(self):
        self._repo()
        first = self.tmp / "first"
        second = self.tmp / "second"
        first.mkdir()
        second.mkdir()
        root_a = canonical_worktree_root(first)
        root_b = canonical_worktree_root(second)
        self.assertEqual(state_for_repo(root_a), state_for_repo(root_b))

    def test_non_git_override_cannot_mask_failure_inside_git_metadata(self):
        self._repo()
        cfg = {"repo": str(self.tmp), "allow_non_git": True}
        with patch("review_loop._git_toplevel",
                   return_value=(None, "fatal: corrupt repository")):
            root, problems = resolve_config_repo(cfg)
        self.assertIsNone(root)
        self.assertTrue(any("cannot override" in problem for problem in problems))

    def test_state_is_outside_worktree_and_symlinked_state_is_rejected(self):
        self._repo()
        state = state_for_repo(self.tmp)
        self.assertFalse(str(state).startswith(str(self.tmp / STATE_DIRNAME)))
        target = self.tmp / "attacker-state"
        target.mkdir()
        state.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            _ensure_state_ignored(state)


class RunLock(unittest.TestCase):
    """Two runs in one worktree would edit the same files and race on
    `current-run`; the second must be told, not silently allowed."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / STATE_DIRNAME
        self.state.mkdir()
        (self.state / ".owner").write_text("review-loop managed state v1\n")

    def _held(self):
        import json as _json
        return _json.loads((self.state / "lock").read_text())

    def test_second_live_run_is_refused(self):
        self.assertIsNone(acquire_lock(self.state, new_lock_token(), os.getpid()))
        err = acquire_lock(self.state, new_lock_token(), os.getpid())
        self.assertIsNotNone(err)
        self.assertIn("already active", err)

    def test_two_concurrent_starts_cannot_both_acquire(self):
        # Both `start` calls pick the same next run number before either has
        # created it, so identity cannot come from the run directory.
        self.assertIsNone(acquire_lock(self.state, new_lock_token(), os.getpid(),
                                       Path("/runs/run-01")))
        self.assertIsNotNone(acquire_lock(self.state, new_lock_token(), os.getpid(),
                                          Path("/runs/run-01")))

    def test_child_adopts_its_parents_lock_with_the_handoff_token(self):
        token = new_lock_token()
        self.assertIsNone(acquire_lock(self.state, token, os.getpid()))
        self.assertIsNone(acquire_lock(self.state, token, os.getpid(),
                                       Path("/runs/run-01"), adopt_only=True))
        self.assertEqual(self._held()["run_dir"], "/runs/run-01")

    def test_a_handoff_token_works_only_once(self):
        # Otherwise re-running the same resolved config would adopt a live lock.
        token = new_lock_token()
        acquire_lock(self.state, token, os.getpid())
        acquire_lock(self.state, token, os.getpid(), Path("/runs/run-01"),
                     adopt_only=True)
        self.assertIsNotNone(acquire_lock(self.state, token, os.getpid(),
                                          Path("/runs/run-01"), adopt_only=True))

    def test_resolved_config_cannot_recreate_a_missing_handoff_lock(self):
        # Once a run completes its lock is removed but its resolved config still
        # contains the old token and run_dir. Replaying that file must not
        # overwrite the run's config, progress stream and findings.
        err = acquire_lock(self.state, "consumed-token", os.getpid(),
                           Path("/runs/run-01"), adopt_only=True)
        self.assertIsNotNone(err)
        self.assertIn("could not adopt", err)
        self.assertFalse((self.state / "lock").exists())

    def test_a_lock_mid_write_is_not_mistaken_for_a_stale_one(self):
        # An O_EXCL create leaves the file empty for an instant. Two `start`
        # calls racing both read nothing, both called it corrupt, both took it.
        (self.state / "lock").write_text("")
        err = acquire_lock(self.state, new_lock_token(), os.getpid())
        self.assertIsNotNone(err)
        self.assertIn("holding this worktree", err)

    def test_an_old_unreadable_lock_is_still_reclaimable(self):
        lock = self.state / "lock"
        lock.write_text("{ truncated")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.assertIsNone(acquire_lock(self.state, new_lock_token(), os.getpid()))

    def test_stale_lock_from_a_dead_run_is_taken_over(self):
        dead = 999_999_999   # not a live pid
        (self.state / "lock").write_text(
            '{"pid": %d, "token": "other", "run_dir": "/runs/run-01"}' % dead)
        self.assertIsNone(acquire_lock(self.state, new_lock_token(), os.getpid()))

    def test_handoff_names_the_child_but_not_a_released_lock(self):
        token = new_lock_token()
        acquire_lock(self.state, token, os.getpid())
        handoff_lock(self.state, token, 4242, Path("/runs/run-01"))
        self.assertEqual(self._held()["pid"], 4242)
        # Once the child has adopted (token rotated), a late handoff is a no-op.
        acquire_lock(self.state, self._held()["token"], 777, Path("/runs/run-01"),
                     adopt_only=True)
        handoff_lock(self.state, token, 9999, Path("/runs/run-01"))
        self.assertEqual(self._held()["pid"], 777)

    def test_handoff_cannot_restore_a_token_consumed_during_its_read(self):
        token = new_lock_token()
        acquire_lock(self.state, token, os.getpid())
        parent_read = threading.Event()
        allow_parent = threading.Event()
        child_done = threading.Event()
        original_loads = json.loads

        def delayed_loads(value, *args, **kwargs):
            parsed = original_loads(value, *args, **kwargs)
            if threading.current_thread().name == "late-parent-handoff":
                parent_read.set()
                allow_parent.wait(2)
            return parsed

        parent = threading.Thread(
            name="late-parent-handoff",
            target=handoff_lock,
            args=(self.state, token, 4242, Path("/runs/run-01")),
        )

        def adopt():
            acquire_lock(self.state, token, 777, Path("/runs/run-01"),
                         adopt_only=True)
            child_done.set()

        with patch("review_loop.json.loads", side_effect=delayed_loads):
            parent.start()
            self.assertTrue(parent_read.wait(1))
            child = threading.Thread(target=adopt)
            child.start()
            # Without serialization the child consumes the token while the
            # parent is paused, after which the parent's stale write restores
            # it. With the guard the child waits for the transition.
            child_done.wait(0.2)
            allow_parent.set()
            parent.join(2)
            child.join(2)
        self.assertFalse(parent.is_alive() or child.is_alive())
        self.assertNotEqual(self._held()["token"], token)
        self.assertEqual(self._held()["pid"], 777)

    def test_release_only_removes_our_own_lock(self):
        acquire_lock(self.state, new_lock_token(), os.getpid(), Path("/runs/run-01"))
        release_lock(self.state, Path("/runs/run-99"))
        self.assertTrue((self.state / "lock").exists())
        release_lock(self.state, Path("/runs/run-01"))
        self.assertFalse((self.state / "lock").exists())

    def test_run_directory_is_reserved_atomically(self):
        # Returning a name without claiming it lets two callers share one.
        a = _new_run_dir(self.state)
        b = _new_run_dir(self.state)
        self.assertNotEqual(a, b)
        self.assertTrue(a.is_dir() and b.is_dir())

    def test_historical_pid_file_without_a_lock_cannot_authorize_kill(self):
        run_dir = self.state / "history" / "run-01"
        run_dir.mkdir(parents=True)
        (run_dir / "pid").write_text(str(os.getpid()))
        self.assertIsNone(_locked_run_pid(run_dir))

    def test_lock_and_pid_file_must_name_the_same_run(self):
        run_dir = self.state / "history" / "run-01"
        run_dir.mkdir(parents=True)
        (run_dir / "pid").write_text(str(os.getpid()))
        acquire_lock(self.state, new_lock_token(), os.getpid(),
                     self.state / "history" / "run-02")
        self.assertIsNone(_locked_run_pid(run_dir))

    def test_matching_live_lock_authorizes_only_its_recorded_pid(self):
        run_dir = self.state / "history" / "run-01"
        run_dir.mkdir(parents=True)
        (run_dir / "pid").write_text(str(os.getpid()))
        acquire_lock(self.state, new_lock_token(), os.getpid(), run_dir)
        self.assertEqual(_locked_run_pid(run_dir), os.getpid())


class ExitContract(unittest.TestCase):
    """The exit code is the whole interface in CI."""

    def test_approval_is_zero(self):
        self.assertEqual(exit_code("approved"), 0)
        self.assertEqual(exit_code("approved_unverified"), 0)

    def test_could_not_run_is_one_wherever_it_happened(self):
        # This exited 1 before the first round and 2 from a fix round.
        self.assertEqual(exit_code("implementer_failed"), 1)
        self.assertEqual(exit_code("baseline_failed"), 1)
        self.assertEqual(exit_code("run_failed"), 1)

    def test_ran_but_did_not_approve_is_two(self):
        for outcome in ("review_incomplete", "validation_not_configured",
                        "max_iterations_reached", "no_progress", "stopped_by_user",
                        "something_new"):
            self.assertEqual(exit_code(outcome), 2, outcome)


class PorcelainParsing(unittest.TestCase):
    """`git status` output decides whether the loop refuses to start."""

    def test_paths_with_spaces_survive(self):
        entries = _porcelain_entries(" M src/my file.py\0?? other.py\0")
        self.assertEqual(entries, [(" M", "src/my file.py"), ("??", "other.py")])

    def test_rename_source_is_not_read_as_an_entry(self):
        entries = _porcelain_entries("R  new.py\0old.py\0 M app.py\0")
        self.assertEqual([p for _, p in entries], ["new.py", "app.py"])

    def test_empty_output(self):
        self.assertEqual(_porcelain_entries(""), [])


class TerminalFailureContract(unittest.TestCase):
    def setUp(self):
        self.state_root = Path(tempfile.mkdtemp()) / "state"
        self.env = patch.dict(os.environ,
                              {"REVIEW_LOOP_STATE_ROOT": str(self.state_root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _state(self, root):
        return state_for_repo(root, False)

    def _config(self, root):
        return {
            "task": "exercise crash handling",
            "repo": str(root),
            "allow_non_git": True,
            "permission_mode": "acceptEdits",
            "validation": {"commands": ["true"]},
            "implementer": {"cli": "claude", "effort": "high"},
            "reviewers": [{"persona": "security", "cli": "claude"}],
        }

    def test_unexpected_orchestration_error_writes_terminal_state_and_unlocks(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root)
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config))
        args = types.SimpleNamespace(config=str(config_path))
        with patch("review_loop.shutil.which", return_value="/fake/claude"), \
             patch("review_loop._run_loop", side_effect=RuntimeError("boom")):
            rc = cmd_run(args)
        self.assertEqual(rc, 1)
        state = self._state(root)
        run_dir = next((state / "history").iterdir())
        events = [json.loads(line) for line in
                  (run_dir / "progress.jsonl").read_text().splitlines()]
        self.assertEqual(events[-2]["event"], "run_failed")
        self.assertEqual(events[-1]["event"], "run_complete")
        self.assertEqual(events[-1]["outcome"], "run_failed")
        self.assertIn("Outcome: run_failed", (run_dir / "final.md").read_text())
        self.assertFalse((state / "lock").exists())

    def test_run_directory_failure_still_terminalizes_and_unlocks(self):
        root = Path(tempfile.mkdtemp())
        config_path = root / "config.json"
        config_path.write_text(json.dumps(self._config(root)))
        args = types.SimpleNamespace(config=str(config_path))
        with patch("review_loop.shutil.which", return_value="/fake/claude"), \
             patch("review_loop._new_run_dir", side_effect=RuntimeError("mkdir boom")):
            rc = cmd_run(args)
        self.assertEqual(rc, 1)
        state = self._state(root)
        run_dir = next((state / "history").iterdir())
        self.assertIn("Outcome: run_failed", (run_dir / "final.md").read_text())
        self.assertFalse((state / "lock").exists())

    def test_missing_detached_handoff_is_rejected_without_writing(self):
        root = Path(tempfile.mkdtemp())
        state = self._state(root)
        _ensure_state_ignored(state)
        run_dir = state / "history/run-01"
        run_dir.mkdir(parents=True)
        config = self._config(root)
        config.update({"run_dir": str(run_dir), "lock_token": "missing-token"})
        config_path = run_dir / "config.json"
        config_path.write_text(json.dumps(config))
        with patch("review_loop.shutil.which", return_value="/fake/claude"):
            rc = cmd_run(types.SimpleNamespace(config=str(config_path)))
        self.assertEqual(rc, 1)
        self.assertFalse((run_dir / "progress.jsonl").exists())
        self.assertFalse((run_dir / "final.md").exists())

    def test_start_releases_lock_when_run_reservation_fails(self):
        root = Path(tempfile.mkdtemp())
        config_path = root / "config.json"
        config_path.write_text(json.dumps(self._config(root)))
        with patch("review_loop.shutil.which", return_value="/fake/claude"), \
             patch("review_loop._new_run_dir", side_effect=RuntimeError("reserve boom")):
            rc = cmd_start(types.SimpleNamespace(config=str(config_path)))
        self.assertEqual(rc, 1)
        self.assertFalse((self._state(root) / "lock").exists())

    def test_start_popen_failure_terminalizes_reserved_run(self):
        root = Path(tempfile.mkdtemp())
        config_path = root / "config.json"
        config_path.write_text(json.dumps(self._config(root)))
        with patch("review_loop.shutil.which", return_value="/fake/claude"), \
             patch("review_loop.subprocess.Popen", side_effect=OSError("spawn boom")):
            rc = cmd_start(types.SimpleNamespace(config=str(config_path)))
        self.assertEqual(rc, 1)
        state = self._state(root)
        run_dir = next((state / "history").iterdir())
        events = [json.loads(line) for line in
                  (run_dir / "progress.jsonl").read_text().splitlines()]
        self.assertEqual(events[-2]["event"], "run_failed")
        self.assertEqual(events[-1]["event"], "run_complete")
        self.assertIn("Outcome: run_failed", (run_dir / "final.md").read_text())
        self.assertFalse((state / "lock").exists())

    def test_unrecognized_state_child_is_rejected_without_rewriting_it(self):
        root = Path(tempfile.mkdtemp())
        state = self._state(root)
        _ensure_state_ignored(state)
        (state / "history").write_text("not a directory")
        config_path = root / "config.json"
        config_path.write_text(json.dumps(self._config(root)))
        with patch("review_loop.shutil.which", return_value="/fake/claude"):
            rc = cmd_run(types.SimpleNamespace(config=str(config_path)))
        self.assertEqual(rc, 1)
        self.assertEqual((state / "history").read_text(), "not a directory")
        self.assertEqual(list(state.glob("run-failed-*")), [])
        self.assertFalse((state / "lock").exists())

    def test_closed_stdout_does_not_break_terminal_events_or_report(self):
        root = Path(tempfile.mkdtemp())
        state = self._state(root)
        _ensure_state_ignored(state)
        run_dir = _new_run_dir(state)
        config = self._config(root)
        with patch("builtins.print", side_effect=BrokenPipeError):
            rc = _terminal_run_failure(config, root, state, run_dir, "pipe closed")
        self.assertEqual(rc, 1)
        events = [json.loads(line) for line in
                  (run_dir / "progress.jsonl").read_text().splitlines()]
        self.assertEqual([event["event"] for event in events[-2:]],
                         ["run_failed", "run_complete"])
        self.assertIn("pipe closed", (run_dir / "final.md").read_text())


class StopKillContract(unittest.TestCase):
    def test_explicit_unowned_run_path_cannot_redirect_stop_write(self):
        root = Path(tempfile.mkdtemp())
        run_dir = root / "run-01"
        run_dir.mkdir()
        victim = root / "victim.txt"
        victim.write_text("keep")
        (run_dir / "STOP").symlink_to(victim)
        rc = cmd_stop(types.SimpleNamespace(run=str(run_dir), repo=None, kill=False))
        self.assertEqual(rc, 1)
        self.assertEqual(victim.read_text(), "keep")

    def test_finished_orchestrator_is_not_signaled_after_tree_drain(self):
        run_dir = Path(tempfile.mkdtemp()) / "run-01"
        run_dir.mkdir()
        pid = 424242
        args = types.SimpleNamespace(run=str(run_dir), repo=None, kill=True)
        with patch("review_loop._resolve_run", return_value=run_dir), \
             patch("review_loop._locked_run_pid", side_effect=[pid, None]), \
             patch("review_loop._pid_is_review_loop", return_value=True), \
             patch("review_loop._terminate_marked_processes", return_value=set()), \
             patch("review_loop.os.kill") as kill:
            rc = cmd_stop(args)
        self.assertEqual(rc, 0)
        kill.assert_not_called()

    def test_stop_signals_only_verified_pid_never_its_process_group(self):
        root = Path(tempfile.mkdtemp())
        state = root / STATE_DIRNAME
        (state / ".owner").parent.mkdir(parents=True, exist_ok=True)
        (state / ".owner").write_text("review-loop managed state v1\n")
        run_dir = state / "history/run-01"
        run_dir.mkdir(parents=True)
        (run_dir / "pid").write_text(str(os.getpid()))
        acquire_lock(state, new_lock_token(), os.getpid(), run_dir)
        args = types.SimpleNamespace(run=str(run_dir), repo=None, kill=True)
        with patch("review_loop._resolve_run", return_value=run_dir), \
             patch("review_loop._pid_is_review_loop", return_value=True), \
             patch("review_loop._terminate_marked_processes", return_value=set()), \
             patch("review_loop.os.kill") as kill, \
             patch("review_loop.os.killpg") as killpg:
            rc = cmd_stop(args)
        self.assertEqual(rc, 0)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        killpg.assert_not_called()

    def test_kill_stops_marked_agent_before_lock_can_be_reclaimed(self):
        root = Path(tempfile.mkdtemp())
        state = root / STATE_DIRNAME
        (state / ".owner").parent.mkdir(parents=True, exist_ok=True)
        (state / ".owner").write_text("review-loop managed state v1\n")
        run_dir = state / "history/run-01"
        run_dir.mkdir(parents=True)
        heartbeat = root / "agent-heartbeat"
        child_code = (
            "import time\n"
            "from pathlib import Path\n"
            f"p=Path({str(heartbeat)!r})\n"
            "i=0\n"
            "while True:\n"
            " i += 1\n"
            " p.write_text(str(i))\n"
            " time.sleep(0.05)\n"
        )
        env = os.environ.copy()
        env["REVIEW_LOOP_RUN_DIR"] = str(run_dir.resolve())
        agent = subprocess.Popen([sys.executable, "-c", child_code], env=env,
                                 start_new_session=True)
        orchestrator = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        (run_dir / "pid").write_text(str(orchestrator.pid))
        acquire_lock(state, new_lock_token(), orchestrator.pid, run_dir)
        deadline = time.time() + 2
        while not heartbeat.exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(heartbeat.exists())
        try:
            with patch("review_loop._resolve_run", return_value=run_dir), \
                 patch("review_loop._pid_is_review_loop", return_value=True):
                rc = cmd_stop(types.SimpleNamespace(
                    run=str(run_dir), repo=None, kill=True))
            self.assertEqual(rc, 0)
            agent.wait(timeout=3)
            orchestrator.wait(timeout=3)
            before = heartbeat.read_text()
            time.sleep(0.2)
            self.assertEqual(before, heartbeat.read_text())
            self.assertIsNone(acquire_lock(state, new_lock_token(), os.getpid()))
        finally:
            for proc in (agent, orchestrator):
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=2)


class ReviewShape(unittest.TestCase):
    """Parsing as JSON is not the same as being a review. Every permissive
    reading here normalises to zero findings, which reads as a clean review."""

    def test_well_formed_reviews_pass(self):
        self.assertIsNone(review_problem({"verdict": "approved", "findings": []}))
        self.assertIsNone(review_problem(
            {"verdict": "changes_requested",
             "findings": [{"id": "X-1", "severity": "high", "file": "a.py",
                           "line": 1, "category": "correctness", "problem": "x",
                           "impact": "i", "recommended_fix": "f"}]}))

    def test_missing_verdict_is_rejected(self):
        # `{"findings": []}` parsed, contributed nothing, and approved the run.
        self.assertIn("verdict", review_problem({"findings": []}))
        self.assertIn("verdict", review_problem({"verdict": "", "findings": []}))

    def test_every_finding_field_and_verdict_consistency_are_enforced(self):
        malformed = {"verdict": "approved", "findings": [{"severity": "low"}]}
        self.assertIn("fields did not match", review_problem(malformed))
        complete = {"id": "X", "severity": "low", "file": "a.py", "line": None,
                    "category": "correctness", "problem": "p", "impact": "i",
                    "recommended_fix": "f"}
        self.assertIn("approved verdict", review_problem(
            {"verdict": "approved", "findings": [complete]}))

    def test_findings_must_be_a_list_of_objects(self):
        self.assertIn("not a list", review_problem(
            {"verdict": "approved", "findings": "malformed"}))
        self.assertIn("not a list", review_problem(
            {"verdict": "approved", "findings": None}))
        self.assertIn("not objects", review_problem(
            {"verdict": "approved", "findings": ["a string", 42]}))

    def test_changes_requested_must_name_something(self):
        self.assertIn("listed no findings", review_problem(
            {"verdict": "changes_requested", "findings": []}))

    def test_non_object_review_is_rejected(self):
        for bad in (None, [], "text", 42):
            self.assertIsNotNone(review_problem(bad), bad)

    def test_malformed_findings_never_reach_the_gate_as_findings(self):
        out = normalise_findings("qa", {"verdict": "approved", "findings": "malformed"})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
