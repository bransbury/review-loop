"""Isolated integration tests for the universal shell installer."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "install.sh"
SOURCE = REPO / "skills/review-loop"


class InstallerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.tmpdir = self.root / "installer tmp"
        self.tmpdir.mkdir()
        self.env["TMPDIR"] = str(self.tmpdir)

    def tearDown(self):
        self.temp.cleanup()

    def harness(self, name=".claude"):
        path = self.home / name
        path.mkdir(parents=True, exist_ok=True)
        return path / "skills/review-loop"

    def run_install(self, *args, env=None):
        return subprocess.run(
            ["bash", str(INSTALLER), *args], cwd=REPO,
            env=env or self.env, text=True, capture_output=True,
        )

    def test_symlink_install_and_reinstall(self):
        dest = self.harness()
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), SOURCE.resolve())
        self.assertTrue((dest.parent / ".review-loop.owner").exists())
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(dest.is_symlink())

    def test_symlink_install_can_be_uninstalled(self):
        dest = self.harness()
        self.assertEqual(self.run_install().returncode, 0)
        removed = self.run_install("--uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.is_symlink())
        self.assertFalse((dest.parent / ".review-loop.owner").exists())

    def test_symlink_install_can_switch_to_copy(self):
        dest = self.harness()
        self.assertEqual(self.run_install().returncode, 0)
        copied = self.run_install("--copy")
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertTrue(dest.is_dir())
        self.assertFalse(dest.is_symlink())
        self.assertTrue((dest / ".review-loop-install").exists())
        self.assertFalse((dest.parent / ".review-loop.owner").exists())

    def test_copy_install_update_and_switch_back_to_symlink(self):
        dest = self.harness(".copilot")
        copied = self.run_install("--copy")
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertTrue(dest.is_dir())
        self.assertFalse(dest.is_symlink())
        marker = (dest / ".review-loop-install").read_text()
        self.assertIn("review-loop managed installation\n", marker)
        self.assertIn(f"destination={dest}\n", marker)
        (dest / "local-drift.txt").write_text("remove on update")
        updated = self.run_install("--copy")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertFalse((dest / "local-drift.txt").exists())
        linked = self.run_install()
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertTrue(dest.is_symlink())

    def test_uninstall_removes_only_a_recognized_installation(self):
        dest = self.harness(".codex")
        self.assertEqual(self.run_install("--copy").returncode, 0)
        removed = self.run_install("--uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(dest.exists())

    def test_unknown_directory_is_never_deleted_or_modified(self):
        dest = self.harness()
        dest.mkdir(parents=True)
        precious = dest / "precious.txt"
        precious.write_text("keep me")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized destination", result.stderr)
        self.assertEqual(precious.read_text(), "keep me")

    def test_marker_alone_does_not_make_an_unknown_directory_owned(self):
        dest = self.harness()
        dest.mkdir(parents=True)
        (dest / ".review-loop-install").write_text(
            "review-loop managed installation\n")
        precious = dest / "precious.txt"
        precious.write_text("still here")
        result = self.run_install("--uninstall")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(precious.read_text(), "still here")

    def test_unknown_symlink_is_not_replaced(self):
        target = self.root / "unrelated"
        target.mkdir()
        dest = self.harness()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(target)
        result = self.run_install("--uninstall")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), target.resolve())

    def test_unknown_owner_sidecar_is_never_overwritten_or_deleted(self):
        dest = self.harness()
        owner = dest.parent / ".review-loop.owner"
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.write_text("precious unrelated data\n")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized ownership metadata", result.stderr)
        self.assertEqual(owner.read_text(), "precious unrelated data\n")
        self.assertFalse(dest.exists())

    def test_managed_dangling_symlink_can_be_uninstalled(self):
        dest = self.harness()
        self.assertEqual(self.run_install().returncode, 0)
        missing = self.root / "moved checkout/skills/review-loop"
        dest.unlink()
        dest.symlink_to(missing)
        (dest.parent / ".review-loop.owner").write_text(
            "review-loop managed symlink\n"
            f"destination={dest}\n"
            f"target={missing}\n"
        )
        result = self.run_install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.is_symlink())

    def test_uninstall_removes_stale_recognized_sidecar(self):
        dest = self.harness()
        self.assertEqual(self.run_install().returncode, 0)
        owner = dest.parent / ".review-loop.owner"
        dest.unlink()
        result = self.run_install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(owner.exists())

    def test_link_install_never_replaces_its_own_source(self):
        checkout = self.home / ".claude"
        shutil.copytree(REPO / "skills", checkout / "skills")
        shutil.copy2(INSTALLER, checkout / "install.sh")
        source = checkout / "skills/review-loop"
        result = subprocess.run(
            ["bash", str(checkout / "install.sh")], cwd=checkout,
            env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(source.is_dir())
        self.assertFalse(source.is_symlink())
        self.assertTrue((source / "SKILL.md").exists())

    def test_custom_claude_config_dir_is_created_and_handles_spaces(self):
        custom = self.root / "custom claude config"
        env = dict(self.env)
        env["CLAUDE_CONFIG_DIR"] = str(custom)
        result = self.run_install("--copy", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = custom / "skills/review-loop"
        self.assertTrue(dest.is_dir())
        self.assertTrue((dest / ".review-loop-install").exists())

    def test_markerless_customized_lookalike_is_never_adopted_or_deleted(self):
        dest = self.harness()
        (dest / "scripts").mkdir(parents=True)
        (dest / "SKILL.md").write_text("---\nname: review-loop\n---\ncustom fork\n")
        (dest / "scripts/review_loop.py").write_text(
            '"""review-loop: deterministic implement -> review."""\n')
        precious = dest / "untracked-local-work.txt"
        precious.write_text("keep")
        for mode in ((), ("--copy",), ("--uninstall",)):
            with self.subTest(mode=mode):
                result = self.run_install(*mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(precious.read_text(), "keep")

    def test_all_targets_are_prevalidated_before_any_change(self):
        recognized = self.harness(".claude")
        self.assertEqual(self.run_install("--copy").returncode, 0)
        before_marker = (recognized / ".review-loop-install").read_bytes()
        before_skill = (recognized / "SKILL.md").read_bytes()
        unknown = self.harness(".copilot")
        unknown.mkdir(parents=True)
        precious = unknown / "precious.txt"
        precious.write_text("keep")
        for mode in ((), ("--copy",), ("--uninstall",)):
            with self.subTest(mode=mode):
                result = self.run_install(*mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((recognized / ".review-loop-install").read_bytes(),
                                 before_marker)
                self.assertEqual((recognized / "SKILL.md").read_bytes(), before_skill)
                self.assertEqual(precious.read_text(), "keep")

    def test_installer_lock_serializes_the_whole_target_set(self):
        dest = self.harness()
        key = subprocess.run(
            ["bash", "-c", "printf '%s' \"$HOME\" | cksum | awk '{print $1}'"],
            env=self.env, text=True, capture_output=True, check=True,
        ).stdout.strip()
        lock = self.tmpdir / f"review-loop-installer-{os.getuid()}-{key}.lock"
        lock.mkdir()
        result = self.run_install("--copy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another review-loop installer", result.stderr)
        self.assertFalse(dest.exists())

    def test_failed_update_restores_quarantined_installation(self):
        dest = self.harness()
        self.assertEqual(self.run_install("--copy").returncode, 0)
        precious = dest / "local-only.txt"
        precious.write_text("restore me")
        fake_bin = self.root / "failing tools"
        fake_bin.mkdir()
        fake_cp = fake_bin / "cp"
        fake_cp.write_text("#!/bin/sh\nexit 73\n")
        fake_cp.chmod(0o755)
        env = dict(self.env)
        env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
        result = self.run_install("--copy", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(dest.is_dir())
        self.assertEqual(precious.read_text(), "restore me")
        self.assertFalse(list(dest.parent.glob(".review-loop.quarantine.*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
