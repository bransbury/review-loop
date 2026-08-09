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
    STATE_DIRNAME, Run, dedupe, extract_json, normalise_findings, _write_final,
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
