#!/usr/bin/env python3
"""Tests for review-loop's parsing and merge logic.

These cover the parts that decide whether the loop keeps going, which is where
a silent bug would be most expensive: a dropped finding looks identical to a
clean review.

Run with `python3 tests/test_core.py` — no pytest required.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "review-loop" / "scripts"))

from review_loop import (  # noqa: E402
    STATE_DIRNAME, VALIDATION_FAILED, VALIDATION_NOT_CONFIGURED, VALIDATION_PASSED,
    Ledger, Run, dedupe, extract_json, normalise_findings, validate_config,
    _keyword_hit, _signal_hit, _verdict, _write_final, acquire_lock, release_lock,
    collect_diff, base_commit, diff_note, preflight_repo, run_validation,
    _copilot_argv, _new_run_dir, _porcelain_entries, exit_code, handoff_lock,
    new_lock_token, review_problem, _locked_run_pid,
)


def finding(reviewer: str, **kw):
    base = {"id": "X-1", "severity": "medium", "file": "a.py", "line": 1,
            "category": "correctness", "problem": "p", "impact": "i",
            "recommended_fix": "f"}
    base.update(kw)
    return normalise_findings(reviewer, {"findings": [base]})


class ExtractJson(unittest.TestCase):
    """Only Codex can be schema-constrained; everything else needs parsing."""

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

    def base(self, **over):
        cfg = {"task": "do a thing",
               "validation": {"commands": ["true"]},
               "implementer": {"cli": "claude", "model": "opus", "effort": "high"},
               "reviewers": [{"persona": "security", "cli": "claude", "effort": "max"}]}
        cfg.update(over)
        return cfg

    def test_valid_config_passes(self):
        self.assertEqual(validate_config(self.base()), [])

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

    def test_unknown_severity_is_rejected(self):
        cfg = self.base(blocking_severities=["blocker", "urgent"])
        self.assertTrue(any("urgent" in e for e in validate_config(cfg)))

    def test_zero_iterations_is_rejected(self):
        self.assertTrue(any("max_iterations" in e
                            for e in validate_config(self.base(max_iterations=0))))

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

    def test_untracked_symlink_is_not_followed_outside_the_repo(self):
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("DO-NOT-EXFILTRATE\n")
        (self.repo / "linked-secret").symlink_to(outside)

        diff, _ = collect_diff(self.repo, base_commit(self.repo), {})

        self.assertIn("new symlink: linked-secret", diff)
        self.assertNotIn("DO-NOT-EXFILTRATE", diff)

    def test_state_directory_is_never_reviewed(self):
        state = self.repo / STATE_DIRNAME
        state.mkdir()
        (state / "task.md").write_text("the task\n")
        diff, _ = collect_diff(self.repo, base_commit(self.repo), {})
        self.assertNotIn("the task", diff)


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

    def test_copilot_write_mode_is_unrestricted(self):
        argv, _ = _copilot_argv("p", "auto", "high", False, "acceptEdits", Path("/tmp/o"))
        self.assertNotIn("--deny-tool", argv)


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


class RunLock(unittest.TestCase):
    """Two runs in one worktree would edit the same files and race on
    `current-run`; the second must be told, not silently allowed."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / STATE_DIRNAME
        self.state.mkdir()

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


class ReviewShape(unittest.TestCase):
    """Parsing as JSON is not the same as being a review. Every permissive
    reading here normalises to zero findings, which reads as a clean review."""

    def test_well_formed_reviews_pass(self):
        self.assertIsNone(review_problem({"verdict": "approved", "findings": []}))
        self.assertIsNone(review_problem(
            {"verdict": "changes_requested",
             "findings": [{"severity": "high", "file": "a.py", "problem": "x"}]}))

    def test_missing_verdict_is_rejected(self):
        # `{"findings": []}` parsed, contributed nothing, and approved the run.
        self.assertEqual(review_problem({"findings": []}), "no verdict field")
        self.assertEqual(review_problem({"verdict": "", "findings": []}),
                         "empty verdict field")

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
