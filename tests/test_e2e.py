"""Credential-free end-to-end tests for the orchestration lifecycle."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/review-loop/scripts/review_loop.py"

FAKE_AGENT = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if "--help" in args:
    if os.environ.get("FAKE_CLAUDE_SCHEMA", "1") == "1":
        print("--json-schema <schema>")
    else:
        print("older claude help")
    sys.exit(0)
if name == "claude" and os.environ.get("FAKE_CLAUDE_SCHEMA", "1") == "0" \
        and "--json-schema" in args:
    print("unsupported --json-schema", file=sys.stderr)
    sys.exit(64)
prompt = ""
if "--prompt" in args:
    prompt = args[args.index("--prompt") + 1]
else:
    prompt = sys.stdin.read()
pairs = list(zip(args, args[1:]))
readonly = ("plan" in args or "read-only" in args
            or ("--deny-tool", "write") in pairs)
log = Path(os.environ["FAKE_CLI_LOG"])
with log.open("a") as fh:
    fh.write(json.dumps({"cli": name, "args": args, "readonly": readonly,
                         "cwd": str(Path.cwd()),
                         "copilot_allow_all": os.environ.get("COPILOT_ALLOW_ALL")}) + "\n")

scenario = os.environ.get("FAKE_SCENARIO", "clean")
if readonly:
    if scenario == "malformed_reviewer":
        answer = "this is not a review"
    elif scenario == "malformed_finding":
        answer = json.dumps({"verdict": "approved", "findings": [{"severity": "low"}]})
    elif scenario == "reviewer_repair" and Path("app.txt").read_text().strip() != "reviewer-fixed":
        answer = json.dumps({"verdict": "changes_requested", "findings": [{
            "id": "REVIEW-001", "severity": "high", "file": "app.txt", "line": 1,
            "category": "correctness", "problem": "app value is not reviewer-fixed",
            "impact": "the requested repair is incomplete",
            "recommended_fix": "write reviewer-fixed to app.txt"}]})
    elif scenario == "reviewer_mutates":
        Path("app.txt").write_text("changed-during-review\n")
        answer = json.dumps({"verdict": "approved", "findings": []})
    else:
        answer = json.dumps({"verdict": "approved", "findings": []})
    failed = scenario == "failed_reviewer"
else:
    counter = Path(os.environ["FAKE_IMPLEMENT_COUNT"])
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    target = Path.cwd() / "app.txt"
    if scenario == "hold":
        import time
        release = Path(os.environ["FAKE_RELEASE"])
        while not release.exists():
            time.sleep(0.02)
    if scenario == "validation_repair" and count == 1:
        target.write_text("broken\n")
    elif scenario == "reviewer_repair" and count > 1:
        target.write_text("reviewer-fixed\n")
    else:
        target.write_text("fixed-by-agent\n")
    answer = "implemented"
    failed = False

if name == "codex":
    if "-o" in args:
        Path(args[args.index("-o") + 1]).write_text(answer)
    print("fake codex transcript")
elif name == "claude":
    print(json.dumps({"result": answer, "permission_denials": []}))
else:
    print(answer)
sys.exit(7 if failed else 0)
'''

FAKE_GIT = r'''#!/usr/bin/env python3
import os
import subprocess
import sys

match = os.environ.get("FAKE_GIT_FAIL_MATCH")
joined = " ".join(sys.argv[1:])
if match == "rev-list --all --count" and joined.startswith("rev-parse --verify HEAD"):
    print("injected HEAD failure before rev-list", file=sys.stderr)
    sys.exit(9)
if match and joined.startswith(match):
    print("injected git failure: " + match, file=sys.stderr)
    sys.exit(9)
result = subprocess.run([os.environ["REAL_GIT"], *sys.argv[1:]])
sys.exit(result.returncode)
'''


class FakeCliEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.bin = self.root / "fake bin"
        self.repo.mkdir()
        self.bin.mkdir()
        self.real_git = shutil.which("git")
        assert self.real_git
        for name in ("claude", "copilot", "codex"):
            path = self.bin / name
            path.write_text(FAKE_AGENT)
            path.chmod(0o755)
        git = self.bin / "git"
        git.write_text(FAKE_GIT)
        git.chmod(0o755)

        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.repo / "app.txt").write_text("fixed\n")
        (self.repo / "check.py").write_text(
            "from pathlib import Path\n"
            "raise SystemExit(Path('app.txt').read_text().strip() == 'broken')\n"
        )
        self.git("add", "-A")
        self.git("commit", "-m", "baseline")
        self.log = self.root / "fake-cli.jsonl"
        self.count = self.root / "implement-count"
        self.state_root = self.root / "external state"

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run([self.real_git, *args], cwd=self.repo,
                              check=True, capture_output=True, text=True)

    def config(self, cli="claude", permission_mode="acceptEdits", **overrides):
        cfg = {
            "task": "change app.txt safely",
            "repo": str(self.repo),
            "max_iterations": 3,
            "permission_mode": permission_mode,
            "validation": {"commands": [f"{sys.executable} check.py"]},
            "implementer": {"cli": cli, "model": "fake", "effort": "high"},
            "reviewers": [{"persona": "security", "cli": cli,
                           "model": "fake", "effort": "high"}],
        }
        cfg.update(overrides)
        path = self.root / f"config-{cli}-{permission_mode}.json"
        path.write_text(json.dumps(cfg))
        return path

    def env(self, scenario="clean", **extra):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "REAL_GIT": self.real_git,
            "FAKE_CLI_LOG": str(self.log),
            "FAKE_IMPLEMENT_COUNT": str(self.count),
            "FAKE_SCENARIO": scenario,
            "REVIEW_LOOP_STATE_ROOT": str(self.state_root),
            "FAKE_RELEASE": str(self.root / "release-agent"),
        })
        env.update(extra)
        return env

    def run_loop(self, config, scenario="clean", command="run", **env):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, "--config", str(config)],
            cwd=self.repo, env=self.env(scenario, **env), text=True,
            capture_output=True, timeout=20,
        )

    def current_run(self):
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        return Path((git_dir / "review-loop/current-run").read_text().strip())

    def events(self, run_dir=None):
        path = (run_dir or self.current_run()) / "progress.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_clean_implementation_validation_review_and_report(self):
        result = self.run_loop(self.config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "approved")
        self.assertIn("Outcome: approved", (self.current_run() / "final.md").read_text())
        self.assertEqual((self.repo / "app.txt").read_text(), "fixed-by-agent\n")

    def test_validation_failure_is_sent_back_for_repair(self):
        result = self.run_loop(self.config(), "validation_repair")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "approved")
        self.assertEqual(self.count.read_text(), "2")
        validations = [e for e in self.events() if e["event"] == "validation_done"
                       and e.get("stage") == "round"]
        self.assertEqual([e["passed"] for e in validations], [False, True])

    def test_failing_baseline_stops_before_the_implementer_by_default(self):
        (self.repo / "app.txt").write_text("broken\n")
        self.git("commit", "-am", "broken baseline")
        result = self.run_loop(self.config())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "baseline_failed")
        self.assertFalse(self.count.exists())

    def test_explicit_override_allows_a_baseline_repair_task(self):
        (self.repo / "app.txt").write_text("broken\n")
        self.git("commit", "-am", "broken baseline")
        result = self.run_loop(self.config(require_clean_baseline=False))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "approved")
        self.assertEqual((self.repo / "app.txt").read_text(), "fixed-by-agent\n")
        run = self.current_run()
        baseline = json.loads((run / "validation-00.json").read_text())
        self.assertEqual(baseline["status"], "failed")
        self.assertIn("check.py", baseline["results"][0]["command"])
        implement_prompt = (run / "work/iter00-implementer.prompt.md").read_text()
        review_prompt = next((run / "work").glob("iter01-*.prompt.md")).read_text()
        self.assertIn("ALREADY FAILING", implement_prompt)
        self.assertIn("app.txt", implement_prompt)
        self.assertIn("ALREADY FAILING", review_prompt)
        self.assertIn("FAILING", (run / "final.md").read_text())

    def test_blocking_reviewer_finding_is_repaired_and_resolved(self):
        result = self.run_loop(self.config(), "reviewer_repair")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "approved")
        self.assertEqual(self.events()[-1]["rounds"], 2)
        fix_prompt = (self.current_run() / "work/iter01-fix.prompt.md").read_text()
        self.assertIn("REVIEW-001", fix_prompt)
        ledger = json.loads((self.current_run() / "ledger.json").read_text())
        self.assertEqual(ledger[0]["state"], "resolved")
        self.assertIn("resolved in round 2", (self.current_run() / "final.md").read_text())

    def test_change_during_review_fails_closed(self):
        result = self.run_loop(self.config(), "reviewer_mutates")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "run_failed")
        final = (self.current_run() / "final.md").read_text()
        self.assertIn("changed after validation", final)
        self.assertIn("Review rounds: 1", final)
        self.assertIn("Validation: passing", final)
        self.assertIn("Stage: review", final)
        self.assertNotIn("Base SHA: `(no commits)`", final)

    def test_change_during_validation_fails_closed_before_review(self):
        (self.repo / "check.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "count = Path(os.environ['FAKE_IMPLEMENT_COUNT'])\n"
            "marker = count.with_name('validation-mutated')\n"
            "if count.exists() and not marker.exists():\n"
            "    Path('app.txt').write_text('changed-during-validation\\n')\n"
            "    marker.write_text('yes')\n"
        )
        self.git("add", "check.py")
        self.git("commit", "-m", "validation mutation fixture")
        result = self.run_loop(self.config())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.events()[-1]["outcome"], "run_failed")
        final = (self.current_run() / "final.md").read_text()
        self.assertIn("changed while validation was running", final)
        self.assertIn("Stage: validation", final)
        self.assertFalse(any(call.get("readonly") for call in (
            json.loads(line) for line in self.log.read_text().splitlines())))

    def test_malformed_and_failed_reviewers_never_approve(self):
        for scenario in ("malformed_reviewer", "malformed_finding", "failed_reviewer"):
            with self.subTest(scenario=scenario):
                (self.repo / "app.txt").write_text("fixed\n")
                self.log.write_text("")
                if self.count.exists():
                    self.count.unlink()
                result = self.run_loop(self.config(), scenario)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.events()[-1]["outcome"], "review_incomplete")
                self.assertTrue(any(e["event"] == "review_unparsed" for e in self.events()))

    def test_permission_modes_reach_each_adapter_and_reviewers_stay_readonly(self):
        for cli in ("claude", "copilot", "codex"):
            for mode in ("acceptEdits", "bypassPermissions"):
                with self.subTest(cli=cli, mode=mode):
                    (self.repo / "app.txt").write_text("fixed\n")
                    self.log.write_text("")
                    if self.count.exists():
                        self.count.unlink()
                    result = self.run_loop(self.config(cli, mode),
                                           COPILOT_ALLOW_ALL="1")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = [json.loads(line) for line in self.log.read_text().splitlines()]
                    write_call = next(call for call in calls if not call["readonly"])
                    review_call = next(call for call in calls if call["readonly"])
                    args = write_call["args"]
                    if cli == "claude":
                        self.assertIn(mode, args)
                    elif cli == "copilot":
                        self.assertIn("--allow-all" if mode == "bypassPermissions"
                                      else "shell", args)
                    else:
                        self.assertIn("--dangerously-bypass-approvals-and-sandbox"
                                      if mode == "bypassPermissions"
                                      else "workspace-write", args)
                    self.assertTrue(review_call["readonly"])
                    self.assertEqual(Path(review_call["cwd"]).resolve(), self.repo.resolve())
                    if cli == "copilot":
                        self.assertIsNone(write_call["copilot_allow_all"])
                        self.assertIsNone(review_call["copilot_allow_all"])
                    if cli == "claude":
                        self.assertIn("--json-schema", review_call["args"])

    def test_older_claude_fallback_omits_unsupported_schema_flag(self):
        result = self.run_loop(self.config("claude"), FAKE_CLAUDE_SCHEMA="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        reviews = [json.loads(line) for line in self.log.read_text().splitlines()
                   if json.loads(line)["readonly"]]
        self.assertTrue(reviews)
        self.assertTrue(all("--json-schema" not in call["args"] for call in reviews))

    def test_git_diff_failure_produces_terminal_failure(self):
        result = self.run_loop(self.config(), FAKE_GIT_FAIL_MATCH="diff")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.events()[-2]["event"], "run_failed")
        self.assertEqual(self.events()[-1]["outcome"], "run_failed")
        self.assertFalse((self.current_run().parent.parent / "lock").exists())

    def test_safety_critical_git_failures_fail_closed(self):
        preflight = ("rev-parse --show-toplevel", "rev-parse --absolute-git-dir",
                     "status --porcelain -z")
        for match in preflight:
            with self.subTest(stage="preflight", match=match):
                self.log.write_text("")
                result = self.run_loop(self.config(), FAKE_GIT_FAIL_MATCH=match)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.count.exists())
        postlaunch = ("rev-parse --verify HEAD", "rev-list --all --count",
                      "ls-files -z --cached", "ls-files -z --others")
        for match in postlaunch:
            with self.subTest(stage="postlaunch", match=match):
                self.log.write_text("")
                if self.count.exists():
                    self.count.unlink()
                result = self.run_loop(self.config(), FAKE_GIT_FAIL_MATCH=match)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.events()[-1]["outcome"], "run_failed")
                self.assertTrue((self.current_run() / "final.md").exists())
                self.assertFalse((self.current_run().parent.parent / "lock").exists())

    def test_same_worktree_subdirectory_launch_is_serialized(self):
        one, two = self.repo / "one", self.repo / "two"
        one.mkdir(); two.mkdir()
        (one / ".keep").write_text("")
        (two / ".keep").write_text("")
        self.git("add", "one/.keep", "two/.keep")
        self.git("commit", "-m", "add subdirectories")
        first = json.loads(self.config().read_text())
        first["repo"] = str(one)
        first_path = self.root / "first.json"
        first_path.write_text(json.dumps(first))
        started = self.run_loop(first_path, "hold", command="start")
        self.assertEqual(started.returncode, 0, started.stderr)
        deadline = time.time() + 5
        while not self.count.exists() and time.time() < deadline:
            time.sleep(0.02)
        run_dir = Path(json.loads(started.stdout)["run_dir"])
        lock = run_dir.parent.parent / "lock"
        self.assertTrue(lock.exists())
        self.git("clean", "-fdx")
        self.assertTrue(lock.exists(), "git clean removed the live external lock")
        second = dict(first); second["repo"] = str(two)
        second_path = self.root / "second.json"
        second_path.write_text(json.dumps(second))
        refused = self.run_loop(second_path, "clean", command="start")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already active", refused.stderr)
        (self.root / "release-agent").write_text("go")
        deadline = time.time() + 10
        while time.time() < deadline:
            if (run_dir / "progress.jsonl").exists() and self.events(run_dir)[-1].get(
                    "event") == "run_complete":
                break
            time.sleep(0.05)
        self.assertEqual(self.events(run_dir)[-1]["outcome"], "approved")
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(all(Path(call["cwd"]).resolve() == self.repo.resolve()
                            for call in calls))

    def test_explicit_non_git_run_completes(self):
        non_git = self.root / "plain project"
        non_git.mkdir()
        (non_git / "app.txt").write_text("fixed\n")
        (non_git / "check.py").write_text(
            "from pathlib import Path\n"
            "raise SystemExit(Path('app.txt').read_text().strip() == 'broken')\n")
        config = json.loads(self.config().read_text())
        config.update({"repo": str(non_git), "allow_non_git": True})
        path = self.root / "non-git.json"
        path.write_text(json.dumps(config))
        result = self.run_loop(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        key = hashlib.sha256(str(non_git.resolve()).encode()).hexdigest()[:24]
        state = self.state_root / "non-git" / key
        run_dir = Path((state / "current-run").read_text().strip())
        self.assertEqual(self.events(run_dir)[-1]["outcome"], "approved")

    def test_detached_start_reaches_terminal_state(self):
        result = self.run_loop(self.config(), command="start")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = Path(json.loads(result.stdout)["run_dir"])
        deadline = time.time() + 15
        while time.time() < deadline:
            if (run_dir / "progress.jsonl").exists():
                events = self.events(run_dir)
                if events and events[-1]["event"] == "run_complete":
                    break
            time.sleep(0.05)
        else:
            self.fail("detached run did not reach run_complete")
        self.assertEqual(events[-1]["outcome"], "approved")
        self.assertFalse((self.repo / ".review-loop/lock").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
