# Contributing

Thanks for helping improve review-loop.

## Development

review-loop requires Python 3.9 or newer and has no third-party runtime
dependencies.

```bash
git clone https://github.com/bransbury/review-loop.git
cd review-loop
python3 -m unittest discover -s tests -v
python3 scripts/release.py check
bash -n install.sh
```

Keep changes focused, add tests for behavioral changes, and update the README
or skill instructions when user-facing behavior changes. Do not weaken the
review, validation, or repository-safety gates to accommodate a model or CLI.

## Pull requests

Before opening a pull request:

1. Run the validation commands above.
2. Add an entry beneath `Unreleased` in `CHANGELOG.md` for a user-visible
   change.
3. Explain the behavior changed, how it was tested, and any compatibility or
   security implications.
4. Keep version bumps out of ordinary pull requests; versions are bumped by
   the release pull request.

By contributing, you agree that your contribution is licensed under the
project's MIT License.

## Releases

Maintainer instructions are in [docs/RELEASING.md](docs/RELEASING.md). Releases
use Semantic Versioning and are published from immutable `vX.Y.Z` tags after
CI passes on `main`.
