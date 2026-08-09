#!/usr/bin/env python3
"""Tests for review-loop's parsing and merge logic.

These cover the parts that decide whether the loop keeps going, which is where
a silent bug would be most expensive: a dropped finding looks identical to a
clean review.

Run with `python3 tests/test_core.py` — no pytest required.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "review-loop" / "scripts"))

from review_loop import (  # noqa: E402
    STATE_DIRNAME, Run, dedupe, extract_json, normalise_findings, validate_config,
    _keyword_hit, _signal_hit, _write_final, collect_diff, base_commit, diff_note,
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

    def _write(self, findings, outcome="approved", validation=True):
        tmp = Path(tempfile.mkdtemp())
        (tmp / STATE_DIRNAME).mkdir()
        run_dir = tmp / STATE_DIRNAME / "history" / "run-01"
        run_dir.mkdir(parents=True)
        run = Run(tmp, run_dir, {"reviewers": [{"persona": "security",
                                                "label": "Security Engineer"}]})
        path = _write_final(run, "abc123", outcome, findings, 2, validation, self.BLOCKING)
        return path.read_text()

    def test_blocking_and_advisory_are_separated(self):
        body = self._write(finding("qa", severity="high", file="a.py", problem="blocking one")
                           + finding("qa", severity="low", file="b.py", problem="advisory one"))
        self.assertIn("Blocking findings still open", body)
        self.assertIn("Advisory findings", body)
        self.assertIn("Blocking findings open: 1", body)
        self.assertIn("Advisory findings recorded: 1", body)

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
        self.assertIn("None outstanding.", body)

    def test_records_corroboration_and_failing_validation(self):
        f = finding("qa", severity="high", file="a.py", problem="x")
        f[0]["corroborated_by"] = ["architect"]
        body = self._write(f, outcome="max_iterations_reached", validation=False)
        self.assertIn("independently raised by architect", body)
        self.assertIn("Validation: FAILING", body)
        self.assertIn("needs a human", body)

    def test_every_outcome_renders(self):
        for outcome in ("approved", "max_iterations_reached",
                        "stopped_by_user", "implementer_failed", "unexpected_value"):
            body = self._write([], outcome=outcome)
            self.assertIn(f"**Outcome: {outcome}**", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
