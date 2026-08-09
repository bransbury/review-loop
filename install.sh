#!/usr/bin/env bash
# review-loop universal installer.
#
# Installs the skill into every agent CLI found on this machine. Works for
# Claude Code, GitHub Copilot CLI and Codex, which all read SKILL.md from a
# per-user skills directory.
#
#   ./install.sh              install (symlink where possible)
#   ./install.sh --copy       install as copies instead of symlinks
#   ./install.sh --uninstall  remove
#
# Symlinks are the default so that `git pull` updates every harness at once.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/review-loop"
NAME="review-loop"
MODE="link"

for arg in "$@"; do
  case "$arg" in
    --copy) MODE="copy" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [ ! -d "$SRC" ]; then
  echo "error: cannot find $SRC" >&2
  exit 1
fi

candidates=()
[ -d "${HOME}/.claude" ]  && candidates+=("${HOME}/.claude/skills")
[ -d "${HOME}/.copilot" ] && candidates+=("${HOME}/.copilot/skills")
[ -d "${HOME}/.codex" ]   && candidates+=("${HOME}/.codex/skills")

# Honour a non-default Claude config dir if one is set. This commonly points at
# the default location, so the list is deduplicated below.
if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -d "${CLAUDE_CONFIG_DIR}" ]; then
  candidates+=("${CLAUDE_CONFIG_DIR}/skills")
fi

targets=()
for c in ${candidates+"${candidates[@]}"}; do
  dup=""
  for t in ${targets+"${targets[@]}"}; do
    [ "$c" = "$t" ] && dup=1 && break
  done
  [ -z "$dup" ] && targets+=("$c")
done

if [ ${#targets[@]} -eq 0 ]; then
  echo "No agent CLI directories found (~/.claude, ~/.copilot, ~/.codex)."
  echo "Install at least one of Claude Code, GitHub Copilot CLI or Codex first."
  exit 1
fi

for dir in "${targets[@]}"; do
  dest="${dir}/${NAME}"
  case "$MODE" in
    uninstall)
      if [ -e "$dest" ] || [ -L "$dest" ]; then
        rm -rf "$dest"
        echo "  removed  $dest"
      fi
      ;;
    link)
      mkdir -p "$dir"
      rm -rf "$dest"
      ln -s "$SRC" "$dest"
      echo "  linked   $dest"
      ;;
    copy)
      mkdir -p "$dir"
      rm -rf "$dest"
      cp -R "$SRC" "$dest"
      echo "  copied   $dest"
      ;;
  esac
done

if [ "$MODE" = "uninstall" ]; then
  echo
  echo "review-loop uninstalled."
  exit 0
fi

echo
echo "review-loop installed. Restart your agent CLI, then run:"
echo
echo "    /review-loop  \"your task here\""
echo
python3 "${SRC}/scripts/review_loop.py" detect >/dev/null 2>&1 \
  && echo "Detected agents:" \
  && python3 "${SRC}/scripts/review_loop.py" detect \
     | python3 -c "import json,sys; d=json.load(sys.stdin); [print(f'    {k:8} {len(v[\"models\"])} models') for k,v in d['agents'].items()]"
