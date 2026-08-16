# Releasing review-loop

Releases follow Semantic Versioning. While the project is pre-1.0, increment
the minor version for incompatible behavior changes and the patch version for
backward-compatible fixes.

## Prepare the release

1. Start from a clean branch based on the latest `main` and confirm CI is
   green.
2. Choose the version and run the release preparation command from the
   repository root:

   ```bash
   ./create-release vX.Y.Z
   ```

   The command updates both manifests, moves the `Unreleased` notes into a
   dated release section, updates the comparison links, and runs the complete
   local release validation suite. It is safe to rerun if release preparation
   was interrupted.

3. Review the generated changes, then open and merge a release pull request
   titled `chore(release): vX.Y.Z`.

## Publish the release

After the release pull request is merged, update local `main`, verify the exact
commit, then create an annotated tag. Use a signed tag when signing is
configured:

```bash
git switch main
git pull --ff-only
python3 scripts/release.py check --tag vX.Y.Z
git tag -s vX.Y.Z -m "review-loop vX.Y.Z"
git push origin vX.Y.Z
```

If signed tags are unavailable, use `git tag -a` instead of `git tag -s`.

Pushing the tag starts `.github/workflows/release.yml`. The workflow reruns the
test suite, verifies that the tagged commit is reachable from `main`, checks
that the tag, both manifests, and changelog agree, and creates the GitHub
Release from that changelog section. GitHub automatically attaches source
archives.

## Verify and announce

1. Confirm the Release workflow completed successfully.
2. Check the GitHub Release notes and source archives.
3. Test one clean plugin installation and update the installation if the
   release changes distribution behavior. For installer changes, exercise both
   symlink and copy updates and confirm an unknown destination is refused.
4. For adapter compatibility changes, compare contract tests with the current
   Claude, Codex, and Copilot help output. For process changes, confirm the CI
   matrix covers supported macOS and Linux runners.
5. Announce breaking changes and migrations prominently.

Published tags are immutable. Never move or reuse a version tag. If a release
is faulty, fix it on `main` and publish a new patch release.
