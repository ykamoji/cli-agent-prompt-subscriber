#!/usr/bin/env bash
# Interactive manager for the conversation-hook deny lists.
#
# Lets you add or remove workspace directories in the denied_list.json that
# sits next to each installed hook (Claude/Gemini, global/project scope).
# Workspaces on the deny list are never captured or sent to the egress server.
#
# Usage:
#   ./update.sh [--project-path <dir>]
#
# Navigate menus with arrow keys (or j/k), Enter to select, q to go back.
set -euo pipefail

PROJECT_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-path) PROJECT_PATH="${2:-}"; shift 2 ;;
        -h|--help)      sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              echo "[error] unknown option: $1" >&2; exit 1 ;;
    esac
done

command -v python3 >/dev/null 2>&1 || { echo "[error] python3 is required" >&2; exit 1; }

trap 'tput cnorm 2>/dev/null || true' EXIT

CYAN=$'\033[36m'
DIM=$'\033[2m'
RESET=$'\033[0m'

# --------------------------------------------------------------- key reading
read_key() {
    local k rest
    IFS= read -rsn1 k || { echo quit; return; }
    if [[ "$k" == $'\x1b' ]]; then
        # -t must be an integer for macOS's bash 3.2; arrow-key bytes arrive
        # together, so this only delays a bare Esc press.
        rest=""
        IFS= read -rsn2 -t 1 rest || true
        case "$rest" in
            '[A') echo up; return ;;
            '[B') echo down; return ;;
            *)    echo quit; return ;;   # bare Esc
        esac
    fi
    case "$k" in
        "")  echo enter ;;
        k)   echo up ;;
        j)   echo down ;;
        q|Q) echo quit ;;
        *)   echo other ;;
    esac
}

# menu <title> <opt1> [opt2 ...]
# Draws an arrow-key menu on stderr; prints the selected index to stdout.
# Returns 1 if the user pressed q/Esc.
menu() {
    local title="$1"; shift
    local opts=("$@")
    local count=${#opts[@]} idx=0 i drawn=0
    tput civis 2>/dev/null || true
    while true; do
        if (( drawn )); then printf '\033[%dA' "$((count + 2))" >&2; fi
        printf '\033[K%s\n' "$title" >&2
        for ((i = 0; i < count; i++)); do
            printf '\033[K' >&2
            if (( i == idx )); then
                printf '  %s❯ %s%s\n' "$CYAN" "${opts[i]}" "$RESET" >&2
            else
                printf '    %s\n' "${opts[i]}" >&2
            fi
        done
        printf '\033[K%s(↑/↓ move, Enter select, q back)%s\n' "$DIM" "$RESET" >&2
        drawn=1
        case "$(read_key)" in
            up)    idx=$(( (idx - 1 + count) % count )) ;;
            down)  idx=$(( (idx + 1) % count )) ;;
            enter) tput cnorm 2>/dev/null || true; echo "$idx"; return 0 ;;
            quit)  tput cnorm 2>/dev/null || true; return 1 ;;
        esac
    done
}

# ---------------------------------------------------------------- json edits
# Deny-list entries are printed one per line (paths never contain newlines).
deny_list_entries() {
    TARGET_FILE="$1" python3 - <<'PYEOF'
import json, os
path = os.environ["TARGET_FILE"]
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f).get("DENY_LIST") or []
    except Exception:
        entries = []
    for entry in entries:
        print(entry)
PYEOF
}

# deny_list_edit <file> add|remove <dir>
deny_list_edit() {
    TARGET_FILE="$1" ACTION="$2" DIR_PATH="$3" python3 - <<'PYEOF'
import json, os, sys
path = os.environ["TARGET_FILE"]
action = os.environ["ACTION"]
target = os.environ["DIR_PATH"]

data = {"DENY_LIST": []}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        sys.exit(f"[error] {path} is not valid JSON, fix it first: {exc}")
entries = data.get("DENY_LIST") or []

if action == "add":
    if target in entries:
        print(f"[skip] already on the deny list: {target}")
        sys.exit(0)
    entries.append(target)
    verb = "added"
else:
    if target not in entries:
        print(f"[skip] not on the deny list: {target}")
        sys.exit(0)
    entries.remove(target)
    verb = "removed"

data["DENY_LIST"] = entries
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"[ok] {verb} {target} ({len(entries)} entries)")
PYEOF
}

# ------------------------------------------------------------ target lookup
TARGET_LABELS=()
TARGET_FILES=()

# add_target <label> <scripts_dir> -- only offered when the hook is installed.
add_target() {
    if [[ -f "$2/save_conversation.py" ]]; then
        TARGET_LABELS+=("$1")
        TARGET_FILES+=("$2/denied_list.json")
    fi
}

collect_targets() {
    TARGET_LABELS=()
    TARGET_FILES=()
    add_target "Claude (global)" "$HOME/.claude/hooks"
    add_target "Gemini (global)" "$HOME/.gemini/config/scripts"
    if [[ -n "$PROJECT_PATH" ]]; then
        add_target "Claude (project: $PROJECT_PATH)" "$PROJECT_PATH/.claude/scripts"
        add_target "Gemini (project: $PROJECT_PATH)" "$PROJECT_PATH/.agents/scripts"
    fi
}

pick_target() {
    collect_targets
    local labels=("${TARGET_LABELS[@]+"${TARGET_LABELS[@]}"}")
    labels+=("Enter a project path...")
    local sel
    sel="$(menu "Which deny list do you want to update?" "${labels[@]}")" || return 1

    if (( sel == ${#TARGET_FILES[@]} )); then
        local dir
        read -e -r -p "Project path: " dir >&2
        [[ -n "$dir" ]] || { echo "[error] no path given" >&2; return 1; }
        dir="$(cd "${dir/#\~/$HOME}" 2>/dev/null && pwd)" \
            || { echo "[error] directory does not exist" >&2; return 1; }
        PROJECT_PATH="$dir"
        pick_target
        return
    fi
    CURRENT_LABEL="${TARGET_LABELS[$sel]}"
    CURRENT_FILE="${TARGET_FILES[$sel]}"
}

# -------------------------------------------------------------------- actions
show_list() {
    local entries
    entries="$(deny_list_entries "$CURRENT_FILE")"
    echo
    if [[ -z "$entries" ]]; then
        echo "  ${DIM}(deny list is empty -- all workspaces are captured)${RESET}"
    else
        echo "  Denied workspaces in $CURRENT_LABEL:"
        while IFS= read -r line; do echo "    - $line"; done <<< "$entries"
    fi
    echo
}

add_entry() {
    local dir
    read -e -r -p "Directory to deny: " dir
    [[ -n "$dir" ]] || { echo "[skip] no path given"; return 0; }
    dir="${dir/#\~/$HOME}"
    [[ "$dir" == /* ]] || dir="$PWD/$dir"
    # Normalise (.., trailing slashes); hooks compare workspace paths verbatim.
    dir="$(python3 -c 'import os, sys; print(os.path.normpath(sys.argv[1]))' "$dir")"
    if [[ ! -d "$dir" ]]; then
        local yn
        read -r -p "[warn] $dir does not exist. Add anyway? [y/N] " yn
        [[ "$yn" == "y" || "$yn" == "Y" ]] || { echo "[skip] not added"; return 0; }
    fi
    deny_list_edit "$CURRENT_FILE" add "$dir"
}

remove_entry() {
    local entries=()
    while IFS= read -r line; do [[ -n "$line" ]] && entries+=("$line"); done \
        < <(deny_list_entries "$CURRENT_FILE")
    if (( ${#entries[@]} == 0 )); then
        echo "[info] deny list is already empty"
        return 0
    fi
    local sel
    sel="$(menu "Which directory should be removed from the deny list?" "${entries[@]}")" \
        || { echo "[skip] nothing removed"; return 0; }
    deny_list_edit "$CURRENT_FILE" remove "${entries[$sel]}"
}

# ----------------------------------------------------------------------- main
collect_targets
if (( ${#TARGET_FILES[@]} == 0 )) && [[ -z "$PROJECT_PATH" ]]; then
    echo "[info] no global hook installation found; you'll be asked for a project path."
fi

pick_target || exit 0

while true; do
    sel="$(menu "Deny list: $CURRENT_LABEL" \
        "View deny list" \
        "Add a directory" \
        "Remove a directory" \
        "Switch deny list" \
        "Quit")" || break
    case "$sel" in
        0) show_list ;;
        1) add_entry ;;
        2) remove_entry ;;
        3) pick_target || break ;;
        4) break ;;
    esac
done
echo "Bye."
