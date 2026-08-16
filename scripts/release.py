#!/usr/bin/env python3
"""Bump and validate review-loop release metadata."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = Path(".claude-plugin/plugin.json")
SKILL_MANIFEST = Path("skills/review-loop/SKILL.md")
CHANGELOG = Path("CHANGELOG.md")
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SKILL_VERSION = re.compile(r"(?m)^version:\s*([^\s]+)\s*$")
CHANGELOG_HEADING = re.compile(
    r"(?m)^## \[(?P<version>[^]]+)](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)
CHANGELOG_LINK = re.compile(r"(?m)^\[(?P<version>[^]]+)]:\s+(?P<url>\S+)\s*$")


class ReleaseError(ValueError):
    """Release metadata is invalid or inconsistent."""


def parse_version(value: str) -> str:
    """Return a normalized SemVer value without the release tag's v prefix."""
    version = value[1:] if value.startswith("v") else value
    if not SEMVER.fullmatch(version):
        raise ReleaseError(f"invalid semantic version: {value!r}")
    return version


def _skill_version(text: str) -> str:
    if not text.startswith("---\n"):
        raise ReleaseError("SKILL.md does not start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ReleaseError("SKILL.md has unterminated YAML frontmatter")
    match = SKILL_VERSION.search(text, 0, end)
    if not match:
        raise ReleaseError("SKILL.md frontmatter has no version")
    return match.group(1)


def read_versions(root: Path = ROOT) -> tuple[str, str]:
    try:
        plugin = json.loads((root / PLUGIN_MANIFEST).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {PLUGIN_MANIFEST}: {exc}") from exc
    plugin_version = plugin.get("version")
    if not isinstance(plugin_version, str):
        raise ReleaseError(f"{PLUGIN_MANIFEST} has no string version")

    try:
        skill_text = (root / SKILL_MANIFEST).read_text()
    except OSError as exc:
        raise ReleaseError(f"cannot read {SKILL_MANIFEST}: {exc}") from exc
    return plugin_version, _skill_version(skill_text)


def changelog_notes(version: str, root: Path = ROOT) -> str:
    version = parse_version(version)
    try:
        text = (root / CHANGELOG).read_text()
    except OSError as exc:
        raise ReleaseError(f"cannot read {CHANGELOG}: {exc}") from exc

    headings = list(CHANGELOG_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if heading.group("version") != version:
            continue
        if not heading.group("date"):
            raise ReleaseError(f"changelog entry {version} has no release date")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        notes = text[heading.end():end].strip()
        notes = re.split(r"(?m)^\[[^]]+]:\s+", notes, maxsplit=1)[0].rstrip()
        if not notes:
            raise ReleaseError(f"changelog entry {version} has no release notes")
        return notes + "\n"
    raise ReleaseError(f"CHANGELOG.md has no [{version}] release entry")


def validate_release(root: Path = ROOT, tag: Optional[str] = None) -> str:
    plugin_version, skill_version = read_versions(root)
    parse_version(plugin_version)
    parse_version(skill_version)
    if plugin_version != skill_version:
        raise ReleaseError(
            "version mismatch: "
            f"{PLUGIN_MANIFEST}={plugin_version}, {SKILL_MANIFEST}={skill_version}"
        )
    changelog_notes(plugin_version, root)
    if tag is not None and tag != f"v{plugin_version}":
        raise ReleaseError(
            f"tag {tag!r} does not match manifest version v{plugin_version}"
        )
    return plugin_version


def ensure_commit_on_default_branch(
    commit: str, default_branch: str, root: Path = ROOT
) -> None:
    """Require a release commit to be reachable from origin's default branch."""
    fetch = subprocess.run(
        [
            "git",
            "fetch",
            "--quiet",
            "origin",
            f"{default_branch}:refs/remotes/origin/{default_branch}",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if fetch.returncode != 0:
        raise ReleaseError(
            fetch.stderr.strip() or f"could not fetch origin/{default_branch}"
        )
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, f"origin/{default_branch}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseError(
            f"release commit {commit} is not reachable from origin/{default_branch}"
        )


def bump_version(version: str, root: Path = ROOT) -> None:
    version = parse_version(version)
    plugin_path = root / PLUGIN_MANIFEST
    skill_path = root / SKILL_MANIFEST

    plugin_text = plugin_path.read_text()
    plugin_data = json.loads(plugin_text)
    old_plugin = plugin_data.get("version")
    if not isinstance(old_plugin, str):
        raise ReleaseError(f"{PLUGIN_MANIFEST} has no string version")
    plugin_pattern = re.compile(
        r'(?m)^(\s*"version"\s*:\s*")' + re.escape(old_plugin) + r'("\s*,?\s*)$'
    )
    new_plugin, plugin_count = plugin_pattern.subn(
        lambda match: f"{match.group(1)}{version}{match.group(2)}", plugin_text, count=1
    )
    if plugin_count != 1:
        raise ReleaseError(f"could not update version in {PLUGIN_MANIFEST}")

    skill_text = skill_path.read_text()
    old_skill = _skill_version(skill_text)
    frontmatter_end = skill_text.find("\n---\n", 4)
    frontmatter = skill_text[:frontmatter_end]
    remainder = skill_text[frontmatter_end:]
    new_frontmatter, skill_count = re.subn(
        r"(?m)^version:\s*" + re.escape(old_skill) + r"\s*$",
        f"version: {version}",
        frontmatter,
        count=1,
    )
    if skill_count != 1:
        raise ReleaseError(f"could not update version in {SKILL_MANIFEST}")

    plugin_path.write_text(new_plugin)
    skill_path.write_text(new_frontmatter + remainder)


def roll_changelog(
    version: str, root: Path = ROOT, release_date: Optional[str] = None
) -> None:
    """Move Unreleased notes into a dated release and update compare links."""
    version = parse_version(version)
    if release_date is None:
        release_date = date.today().isoformat()
    else:
        try:
            release_date = date.fromisoformat(release_date).isoformat()
        except ValueError as exc:
            raise ReleaseError(f"invalid release date: {release_date!r}") from exc

    changelog_path = root / CHANGELOG
    text = changelog_path.read_text()
    headings = list(CHANGELOG_HEADING.finditer(text))
    target = next(
        (heading for heading in headings if heading.group("version") == version),
        None,
    )

    if target is None:
        unreleased = next(
            (
                heading
                for heading in headings
                if heading.group("version") == "Unreleased"
            ),
            None,
        )
        if unreleased is None:
            raise ReleaseError("CHANGELOG.md has no [Unreleased] section")
        next_heading = next(
            (heading for heading in headings if heading.start() > unreleased.start()),
            None,
        )
        if next_heading is None:
            raise ReleaseError("CHANGELOG.md has no previous release entry")
        notes = text[unreleased.end() : next_heading.start()].strip()
        if not notes:
            raise ReleaseError("CHANGELOG.md [Unreleased] section has no release notes")
        previous_version = parse_version(next_heading.group("version"))
        replacement = f"## [Unreleased]\n\n## [{version}] - {release_date}"
        text = text[: unreleased.start()] + replacement + text[unreleased.end() :]
    else:
        if not target.group("date"):
            raise ReleaseError(f"changelog entry {version} has no release date")
        following_release = next(
            (
                heading
                for heading in headings
                if heading.start() > target.start()
                and heading.group("version") != "Unreleased"
            ),
            None,
        )
        if following_release is None:
            raise ReleaseError(f"changelog entry {version} has no previous release")
        previous_version = parse_version(following_release.group("version"))
        if not any(heading.group("version") == "Unreleased" for heading in headings):
            text = text[: target.start()] + "## [Unreleased]\n\n" + text[target.start() :]

    unreleased_link = next(
        (
            link
            for link in CHANGELOG_LINK.finditer(text)
            if link.group("version") == "Unreleased"
        ),
        None,
    )
    if unreleased_link is None:
        raise ReleaseError("CHANGELOG.md has no [Unreleased] comparison link")
    compare_suffix = re.search(r"/compare/v[^\s]+\.\.\.HEAD$", unreleased_link.group("url"))
    if compare_suffix is None:
        raise ReleaseError("CHANGELOG.md [Unreleased] link is not a compare URL")
    compare_base = unreleased_link.group("url")[: compare_suffix.start()] + "/compare/"
    new_unreleased_link = f"[Unreleased]: {compare_base}v{version}...HEAD"
    text = (
        text[: unreleased_link.start()]
        + new_unreleased_link
        + text[unreleased_link.end() :]
    )

    target_link_text = (
        f"[{version}]: {compare_base}v{previous_version}...v{version}"
    )
    target_link = next(
        (
            link
            for link in CHANGELOG_LINK.finditer(text)
            if link.group("version") == version
        ),
        None,
    )
    if target_link is None:
        new_unreleased_link_end = text.find("\n", text.find(new_unreleased_link))
        if new_unreleased_link_end == -1:
            text += "\n" + target_link_text + "\n"
        else:
            text = (
                text[: new_unreleased_link_end + 1]
                + target_link_text
                + "\n"
                + text[new_unreleased_link_end + 1 :]
            )
    else:
        text = text[: target_link.start()] + target_link_text + text[target_link.end() :]

    changelog_path.write_text(text)


def prepare_release(
    version: str, root: Path = ROOT, release_date: Optional[str] = None
) -> str:
    """Prepare all versioned release metadata and validate the result."""
    version = parse_version(version)
    bump_version(version, root)
    roll_changelog(version, root, release_date)
    return validate_release(root, f"v{version}")


def run_release_checks(root: Path = ROOT) -> None:
    checks = (
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "scripts/release.py", "check"],
        [sys.executable, "-m", "compileall", "-q", "skills", "scripts", "tests"],
        ["bash", "-n", "install.sh", "create-release"],
    )
    for command in checks:
        print(f"+ {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode != 0:
            raise ReleaseError(
                f"release check failed ({result.returncode}): {' '.join(command)}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate release metadata")
    check.add_argument("--tag", help="also require this vX.Y.Z tag to match")
    check.add_argument(
        "--commit", help="also require this commit to be on origin's default branch"
    )
    check.add_argument("--default-branch", default="main")

    bump = subparsers.add_parser("bump", help="set both manifest versions")
    bump.add_argument("version", help="semantic version, with or without v prefix")

    create = subparsers.add_parser(
        "create", help="prepare release metadata and run all release checks"
    )
    create.add_argument("version", help="semantic version, with or without v prefix")
    create.add_argument(
        "--date", dest="release_date", help="release date (defaults to today)"
    )

    notes = subparsers.add_parser("notes", help="print one changelog section")
    notes.add_argument("version", help="semantic version, with or without v prefix")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            version = validate_release(tag=args.tag)
            if args.commit:
                ensure_commit_on_default_branch(args.commit, args.default_branch)
            print(f"release metadata is consistent: v{version}")
        elif args.command == "bump":
            version = parse_version(args.version)
            bump_version(version)
            print(f"updated manifests to v{version}")
            print("next: add the dated release section to CHANGELOG.md")
        elif args.command == "create":
            version = prepare_release(args.version, release_date=args.release_date)
            print(f"prepared release metadata for v{version}")
            run_release_checks()
            print(f"release v{version} is ready for review and commit")
        elif args.command == "notes":
            sys.stdout.write(changelog_notes(args.version))
    except (OSError, json.JSONDecodeError, ReleaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
