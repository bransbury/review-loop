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

NAME="review-loop"
MODE="link"
REPO_URL="${REVIEW_LOOP_REPO:-https://github.com/bransbury/review-loop.git}"
CHECKOUT="${HOME}/.review-loop/src"

# Resolve the source tree. When this file is run from a clone, that clone is
# the source. When it is piped straight from curl there is no surrounding
# checkout, so fetch one into ~/.review-loop/src and install from there.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  ROOT=""
fi

if [ -n "$ROOT" ] && [ -d "${ROOT}/skills/${NAME}" ]; then
  SRC="${ROOT}/skills/${NAME}"
else
  echo "Fetching ${NAME} into ${CHECKOUT}…"
  if [ -d "${CHECKOUT}/.git" ]; then
    git -C "$CHECKOUT" pull --quiet --ff-only || {
      echo "error: could not update ${CHECKOUT}" >&2; exit 1; }
  else
    mkdir -p "$(dirname "$CHECKOUT")"
    git clone --quiet --depth 1 "$REPO_URL" "$CHECKOUT" || {
      echo "error: could not clone ${REPO_URL}" >&2; exit 1; }
  fi
  SRC="${CHECKOUT}/skills/${NAME}"
fi

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
