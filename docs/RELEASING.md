# Releasing review-loop

Releases follow Semantic Versioning. While the project is pre-1.0, increment
the minor version for incompatible behavior changes and the patch version for
backward-compatible fixes.

## Prepare the release

1. Start from a clean branch based on the latest `main` and confirm CI is
   green.
2. Choose the version and run:

   ```bash
   python3 scripts/release.py bump X.Y.Z
   ```

3. Move the relevant entries from `Unreleased` into a dated
   `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`. Update the comparison
   links at the bottom of the file.
4. Validate locally:

   ```bash
   python3 -m unittest discover -s tests -v
   python3 scripts/release.py check
   bash -n install.sh
   ```

5. Open and merge a release pull request titled `chore(release): vX.Y.Z`.

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
   release changes distribution behavior.
4. Announce breaking changes and migrations prominently.

Published tags are immutable. Never move or reuse a version tag. If a release
is faulty, fix it on `main` and publish a new patch release.
