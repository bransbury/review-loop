import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_tool", REPO / "scripts/release.py")
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".claude-plugin").mkdir()
        (self.root / "skills/review-loop").mkdir(parents=True)
        self.write_version("0.2.0", "0.2.0")
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "## [0.2.0] - 2026-08-09\n\n"
            "### Added\n\n- Safer releases.\n\n"
            "## [0.1.0] - 2026-08-08\n\n- Initial release.\n\n"
            "[0.2.0]: https://example.test/v0.2.0\n"
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_version(self, plugin, skill):
        (self.root / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "review-loop", "version": plugin}, indent=2) + "\n"
        )
        (self.root / "skills/review-loop/SKILL.md").write_text(
            f"---\nname: review-loop\nversion: {skill}\n---\n\n# Skill\n"
        )


class ReleaseValidation(ReleaseFixture):
    def test_current_repository_metadata_is_consistent(self):
        self.assertEqual("0.2.0", release.validate_release(REPO, "v0.2.0"))

    def test_matching_metadata_and_tag_pass(self):
        self.assertEqual("0.2.0", release.validate_release(self.root, "v0.2.0"))

    def test_mismatched_manifests_fail(self):
        self.write_version("0.2.0", "0.3.0")
        with self.assertRaisesRegex(release.ReleaseError, "version mismatch"):
            release.validate_release(self.root)

    def test_mismatched_tag_fails(self):
        with self.assertRaisesRegex(release.ReleaseError, "does not match"):
            release.validate_release(self.root, "v0.2.1")

    def test_missing_changelog_entry_fails(self):
        self.write_version("0.3.0", "0.3.0")
        with self.assertRaisesRegex(release.ReleaseError, "no \[0.3.0\]"):
            release.validate_release(self.root)

    def test_invalid_semver_fails(self):
        with self.assertRaisesRegex(release.ReleaseError, "invalid semantic version"):
            release.parse_version("version-two")

    def test_installed_cli_reports_release_version(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "skills/review-loop/scripts/review_loop.py"),
                "--version",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("review_loop 0.2.0", result.stdout.strip())


class ReleaseChanges(ReleaseFixture):
    def test_bump_updates_both_manifests(self):
        release.bump_version("v0.3.0", self.root)
        self.assertEqual(("0.3.0", "0.3.0"), release.read_versions(self.root))

    def test_notes_extract_only_requested_release(self):
        notes = release.changelog_notes("v0.2.0", self.root)
        self.assertIn("Safer releases", notes)
        self.assertNotIn("Initial release", notes)
        self.assertNotIn("https://example.test", notes)


class ReleaseBranchValidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.work = self.base / "work"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True,
            capture_output=True,
        )
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Release Test")
        self.git("switch", "-c", "main")
        (self.work / "file.txt").write_text("main\n")
        self.git("add", "file.txt")
        self.git("commit", "-m", "main")
        self.main_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "-u", "origin", "main")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.work,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_commit_on_main_passes(self):
        release.ensure_commit_on_default_branch(self.main_commit, "main", self.work)

    def test_unmerged_commit_fails(self):
        self.git("switch", "-c", "feature")
        (self.work / "file.txt").write_text("feature\n")
        self.git("commit", "-am", "feature")
        feature_commit = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(release.ReleaseError, "not reachable"):
            release.ensure_commit_on_default_branch(feature_commit, "main", self.work)


if __name__ == "__main__":
    unittest.main()
