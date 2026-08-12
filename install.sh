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
OWNER_MARKER=".review-loop-install"
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
if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
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

# Serialize all destinations for this HOME. mkdir is atomic and is available
# on both supported macOS and Linux systems (unlike the flock command).
lock_key="$(printf '%s' "$HOME" | cksum | awk '{print $1}')"
installer_lock="${TMPDIR:-/tmp}/review-loop-installer-$(id -u)-${lock_key}.lock"
if ! mkdir "$installer_lock" 2>/dev/null; then
  echo "error: another review-loop installer is active for this HOME" >&2
  exit 1
fi
trap 'rmdir "$installer_lock" 2>/dev/null || true' EXIT HUP INT TERM

if [ ${#targets[@]} -eq 0 ]; then
  echo "No agent CLI directories found (~/.claude, ~/.copilot, ~/.codex)."
  echo "Install at least one of Claude Code, GitHub Copilot CLI or Codex first."
  exit 1
fi

is_owned_installation() {
  local actual="$1"
  local expected="$2"
  local owner="$3"
  local target
  if [ -L "$actual" ] && [ -f "$owner" ] && [ ! -L "$owner" ]; then
    target="$(readlink "$actual")"
    if grep -qx "review-loop managed symlink" "$owner" \
       && grep -Fqx "destination=${expected}" "$owner" \
       && grep -Fqx "target=${target}" "$owner" \
       && [ "$(wc -l < "$owner" | tr -d ' ')" = "3" ]; then
      return 0
    fi
  fi
  if [ -d "$actual" ] && [ ! -L "$actual" ] \
     && [ -f "${actual}/${OWNER_MARKER}" ] \
     && [ ! -L "${actual}/${OWNER_MARKER}" ] \
     && grep -qx "review-loop managed installation" "${actual}/${OWNER_MARKER}" \
     && grep -Fqx "destination=${expected}" "${actual}/${OWNER_MARKER}" \
     && [ "$(wc -l < "${actual}/${OWNER_MARKER}" | tr -d ' ')" = "2" ]; then
    return 0
  fi
  return 1
}

is_owned_sidecar() {
  local owner="$1"
  local expected="$2"
  local first
  local second
  local third
  if [ ! -f "$owner" ] || [ -L "$owner" ]; then
    return 1
  fi
  first="$(sed -n '1p' "$owner")"
  second="$(sed -n '2p' "$owner")"
  third="$(sed -n '3p' "$owner")"
  [ "$first" = "review-loop managed symlink" ] || return 1
  [ "$second" = "destination=${expected}" ] || return 1
  [ -n "${third#target=}" ] && [ "$third" != "${third#target=}" ] || return 1
  if [ "$(wc -l < "$owner" | tr -d ' ')" != "3" ]; then
    return 1
  fi
}

remove_owned_installation() {
  local dest="$1"
  if [ -L "$dest" ]; then
    rm -- "$dest"
  else
    rm -rf -- "$dest"
  fi
}

write_symlink_owner() {
  local owner="$1"
  local dest="$2"
  local target="$3"
  local staged_owner="${owner}.install.$$"
  if [ -e "$owner" ] || [ -L "$owner" ] \
     || [ -e "$staged_owner" ] || [ -L "$staged_owner" ]; then
    echo "error: refusing unexpected ownership path: $owner" >&2
    return 1
  fi
  (set -C; printf '%s\n%s\n%s\n' "review-loop managed symlink" \
    "destination=${dest}" "target=${target}" > "$staged_owner")
  if ! ln "$staged_owner" "$owner"; then
    rm -f -- "$staged_owner"
    echo "error: ownership path changed during install: $owner" >&2
    return 1
  fi
  rm -f -- "$staged_owner"
}

# Validate every existing destination before changing any of them. Otherwise a
# later unknown directory could leave only half the configured harnesses
# updated — or, historically, be recursively deleted without recognition.
unsafe=0
for dir in "${targets[@]}"; do
  dest="${dir}/${NAME}"
  owner="${dir}/.${NAME}.owner"
  if { [ -e "$dest" ] || [ -L "$dest" ]; } \
     && ! { [ "$MODE" = "link" ] && [ -e "$dest" ] && [ ! -L "$dest" ] \
            && [ "$SRC" -ef "$dest" ]; } \
     && ! is_owned_installation "$dest" "$dest" "$owner"; then
    echo "error: refusing to replace unrecognized destination: $dest" >&2
    unsafe=1
  fi
  if { [ -e "$owner" ] || [ -L "$owner" ]; } \
     && ! is_owned_sidecar "$owner" "$dest"; then
    echo "error: refusing to replace unrecognized ownership metadata: $owner" >&2
    unsafe=1
  fi
  if [ -e "$dest" ] && [ ! -L "$dest" ] \
     && [ "$SRC" -ef "$dest" ] && [ "$MODE" != "link" ]; then
    echo "error: refusing to ${MODE} the installer source itself: $dest" >&2
    unsafe=1
  fi
done
if [ "$unsafe" -ne 0 ]; then
  exit 1
fi

# Atomically remove every destination from its public name, then validate the
# moved objects. Deletion is authorized only for the exact object that was
# moved; a racing replacement is never passed to rm -rf.
quarantines=()
owner_quarantines=()
transaction_active=1

installer_cleanup() {
  cleanup_rc=$?
  if [ "$transaction_active" -eq 1 ]; then
    cleanup_i=0
    for cleanup_dir in "${targets[@]}"; do
      cleanup_dest="${cleanup_dir}/${NAME}"
      cleanup_owner="${cleanup_dir}/.${NAME}.owner"
      cleanup_q="${quarantines[$cleanup_i]:-}"
      cleanup_oq="${owner_quarantines[$cleanup_i]:-}"
      if { [ -e "$cleanup_dest" ] || [ -L "$cleanup_dest" ]; } \
         && is_owned_installation "$cleanup_dest" "$cleanup_dest" "$cleanup_owner"; then
        remove_owned_installation "$cleanup_dest"
        rm -f -- "$cleanup_owner"
      elif [ -L "$cleanup_dest" ] && [ "$(readlink "$cleanup_dest")" = "$SRC" ] \
           && [ ! -e "$cleanup_owner" ]; then
        rm -- "$cleanup_dest"
      fi
      if [ -n "$cleanup_q" ] && { [ -e "$cleanup_q" ] || [ -L "$cleanup_q" ]; } \
         && [ ! -e "$cleanup_dest" ] && [ ! -L "$cleanup_dest" ]; then
        mv "$cleanup_q" "$cleanup_dest"
      fi
      if [ -n "$cleanup_oq" ] && { [ -e "$cleanup_oq" ] || [ -L "$cleanup_oq" ]; } \
         && [ ! -e "$cleanup_owner" ]; then
        mv "$cleanup_oq" "$cleanup_owner"
      fi
      cleanup_i=$((cleanup_i + 1))
    done
  fi
  rmdir "$installer_lock" 2>/dev/null || true
  return "$cleanup_rc"
}
trap installer_cleanup EXIT HUP INT TERM
for dir in "${targets[@]}"; do
  dest="${dir}/${NAME}"
  owner="${dir}/.${NAME}.owner"
  quarantine="${dir}/.${NAME}.quarantine.$$"
  owner_quarantine="${dir}/.${NAME}.owner.quarantine.$$"
  if [ -e "$quarantine" ] || [ -L "$quarantine" ] \
     || [ -e "$owner_quarantine" ] || [ -L "$owner_quarantine" ]; then
    echo "error: refusing unexpected quarantine path in $dir" >&2
    exit 1
  fi
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ "$MODE" = "link" ] && [ -e "$dest" ] && [ ! -L "$dest" ] \
       && [ "$SRC" -ef "$dest" ]; then
      quarantines+=("")
      owner_quarantines+=("")
      continue
    fi
    mv "$dest" "$quarantine"
    if [ -e "$owner" ] || [ -L "$owner" ]; then
      mv "$owner" "$owner_quarantine"
    fi
    quarantines+=("$quarantine")
    owner_quarantines+=("$owner_quarantine")
    if ! is_owned_installation "$quarantine" "$dest" "$owner_quarantine"; then
      restore_i=0
      for restore_dir in "${targets[@]}"; do
        restore_dest="${restore_dir}/${NAME}"
        restore_owner="${restore_dir}/.${NAME}.owner"
        restore_q="${quarantines[$restore_i]:-}"
        restore_oq="${owner_quarantines[$restore_i]:-}"
        if [ -n "$restore_q" ] && [ ! -e "$restore_dest" ] \
           && [ ! -L "$restore_dest" ]; then mv "$restore_q" "$restore_dest"; fi
        if [ -n "$restore_oq" ] && { [ -e "$restore_oq" ] || [ -L "$restore_oq" ]; } \
           && [ ! -e "$restore_owner" ]; then mv "$restore_oq" "$restore_owner"; fi
        restore_i=$((restore_i + 1))
      done
      transaction_active=0
      echo "error: destination changed during install; restored without deletion: $dest" >&2
      exit 1
    fi
  else
    quarantines+=("")
    if [ -e "$owner" ]; then
      mv "$owner" "$owner_quarantine"
      owner_quarantines+=("$owner_quarantine")
    else
      owner_quarantines+=("")
    fi
  fi
done

i=0
for dir in "${targets[@]}"; do
  dest="${dir}/${NAME}"
  owner="${dir}/.${NAME}.owner"
  case "$MODE" in
    uninstall)
      if [ -n "${quarantines[$i]:-}" ]; then
        echo "  removed  $dest"
      fi
      ;;
    link)
      mkdir -p "$dir"
      if [ -e "$dest" ] && [ "$SRC" -ef "$dest" ]; then
        if [ ! -e "$owner" ]; then
          write_symlink_owner "$owner" "$dest" "$SRC"
        fi
        echo "  source   $dest"
        i=$((i + 1))
        continue
      fi
      # Creating the public symlink itself is the atomic no-replace claim.
      # A racing unknown destination makes ln fail; it is never overwritten.
      ln -s "$SRC" "$dest"
      write_symlink_owner "$owner" "$dest" "$SRC"
      echo "  linked   $dest"
      ;;
    copy)
      mkdir -p "$dir"
      # mkdir is an atomic no-replace claim for a copied installation. Mark it
      # before copying so cleanup can recognize a partial copy after failure.
      mkdir "$dest"
      printf '%s\n%s\n' "review-loop managed installation" \
        "destination=${dest}" > "${dest}/${OWNER_MARKER}"
      cp -R "${SRC}/." "$dest"
      echo "  copied   $dest"
      ;;
  esac
  i=$((i + 1))
done

# Every recursively deleted directory was first moved and revalidated using
# destination-bound ownership metadata. Public destinations are already the
# new installs (or absent for uninstall), so races cannot swap deletion targets.
for quarantine in "${quarantines[@]}"; do
  [ -z "$quarantine" ] || remove_owned_installation "$quarantine"
done
for owner_quarantine in "${owner_quarantines[@]}"; do
  [ -z "$owner_quarantine" ] || rm -f -- "$owner_quarantine"
done
transaction_active=0

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
