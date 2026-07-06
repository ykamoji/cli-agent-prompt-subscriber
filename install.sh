#!/usr/bin/env bash
# Installer for the CLI agent conversation hooks (Claude Code + Gemini/Antigravity).
#
# Copies the Stop-hook scripts into the standard locations, registers the hook
# in the agent's settings file (merging with whatever is already there), and
# writes a subscriber_config.json so the hooks can POST conversation logs to
# the hosted egress server -- no MongoDB access or pip installs needed.
#
# Usage:
#   ./install.sh --user-id <id> --user-name <name> \
#                --agent claude|gemini|both --scope global|project \
#                [--project-path <dir>] [--api-url <url>] [--api-key <key>]
#
# Any missing value is prompted for interactively.
set -euo pipefail

# Default egress server. Override with --api-url if you host your own.
DEFAULT_API_URL="https://agent-cli-dashboard.onrender.com"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_SRC="$REPO_DIR/hooks"

USER_ID=""
USER_NAME=""
AGENT=""
SCOPE=""
PROJECT_PATH=""
API_URL=""
API_KEY=""

usage() {
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

err() { echo "[error] $*" >&2; exit 1; }
info() { echo "[ok] $*"; }

# ---------------------------------------------------------------- arg parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user-id)      USER_ID="${2:-}"; shift 2 ;;
        --user-name)    USER_NAME="${2:-}"; shift 2 ;;
        --agent)        AGENT="${2:-}"; shift 2 ;;
        --scope)        SCOPE="${2:-}"; shift 2 ;;
        --project-path) PROJECT_PATH="${2:-}"; shift 2 ;;
        --api-url)      API_URL="${2:-}"; shift 2 ;;
        --api-key)      API_KEY="${2:-}"; shift 2 ;;
        -h|--help)      usage ;;
        *)              echo "[error] unknown option: $1" >&2; usage 1 ;;
    esac
done

command -v python3 >/dev/null 2>&1 || err "python3 is required but not found on PATH"
[[ -d "$HOOKS_SRC" ]] || err "hooks directory not found at $HOOKS_SRC (run from a full clone)"

# ------------------------------------------------------------------- prompts
while [[ -z "$USER_ID" ]]; do
    read -r -p "User id: " USER_ID
done
while [[ -z "$USER_NAME" ]]; do
    read -r -p "User name: " USER_NAME
done
while [[ "$AGENT" != "claude" && "$AGENT" != "gemini" && "$AGENT" != "both" ]]; do
    read -r -p "Agent to install for [claude/gemini/both]: " AGENT
done
while [[ "$SCOPE" != "global" && "$SCOPE" != "project" ]]; do
    read -r -p "Install scope [global/project]: " SCOPE
done
if [[ "$SCOPE" == "project" && -z "$PROJECT_PATH" ]]; then
    read -r -p "Project path [$PWD]: " PROJECT_PATH
    PROJECT_PATH="${PROJECT_PATH:-$PWD}"
fi
if [[ -z "$API_URL" ]]; then
    read -r -p "Egress server URL [$DEFAULT_API_URL]: " API_URL
    API_URL="${API_URL:-$DEFAULT_API_URL}"
fi
while [[ -z "$API_KEY" ]]; do
    read -r -p "Your API key (generated for you in the CLI dashboard app): " API_KEY
done

if [[ "$SCOPE" == "project" ]]; then
    PROJECT_PATH="$(cd "$PROJECT_PATH" 2>/dev/null && pwd)" \
        || err "project path does not exist: $PROJECT_PATH"
fi

# ------------------------------------------------------------------- helpers

# copy_hook_files <src_agent_dir> <dest_dir> <extra files...>
copy_hook_files() {
    local src="$1" dest="$2"
    mkdir -p "$dest"
    cp "$src/save_conversation.py" "$dest/"
    cp "$HOOKS_SRC/send_logs.py" "$dest/"
    if [[ ! -f "$dest/denied_list.json" ]]; then
        cp "$HOOKS_SRC/denied_list.json" "$dest/"
    fi
}

# write_subscriber_config <dest_dir> <cli_agent_label>
write_subscriber_config() {
    DEST_DIR="$1" CLI_AGENT="$2" \
    SUB_USER_ID="$USER_ID" SUB_USER_NAME="$USER_NAME" \
    SUB_API_URL="$API_URL" \
    python3 - <<'PYEOF'
import json, os

config = {
    "user_id": os.environ["SUB_USER_ID"],
    "user_name": os.environ["SUB_USER_NAME"],
    "api_url": os.environ["SUB_API_URL"],
    "cli_agent": os.environ["CLI_AGENT"],
    "log_dir": "../agent_logs",
}
path = os.path.join(os.environ["DEST_DIR"], "subscriber_config.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PYEOF
    # The per-user API key is kept out of the config in its own secrets file.
    printf '%s\n' "$API_KEY" > "$1/secrets.txt"
    chmod 600 "$1/secrets.txt"
}

# merge_claude_settings <settings_file> <hook_command>
merge_claude_settings() {
    TARGET_FILE="$1" HOOK_CMD="$2" python3 - <<'PYEOF'
import json, os, sys

path = os.environ["TARGET_FILE"]
cmd = os.environ["HOOK_CMD"]

data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"[error] {path} is not valid JSON, fix it first: {exc}")

stop = data.setdefault("hooks", {}).setdefault("Stop", [])
for block in stop:
    for hook in block.get("hooks", []):
        if "send_logs.py" in hook.get("command", ""):
            print(f"[skip] hook already registered in {path}")
            sys.exit(0)

stop.append({
    "matcher": "*",
    "hooks": [{
        "type": "command",
        "command": cmd,
        "async": True,
        "statusMessage": "Capturing transcript data...",
    }],
})
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"[ok] registered Stop hook in {path}")
PYEOF
}

# merge_gemini_hooks <hooks_file> <hook_command>
merge_gemini_hooks() {
    TARGET_FILE="$1" HOOK_CMD="$2" python3 - <<'PYEOF'
import json, os, sys

path = os.environ["TARGET_FILE"]
cmd = os.environ["HOOK_CMD"]

data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"[error] {path} is not valid JSON, fix it first: {exc}")

stop = data.setdefault("chat-grabber", {}).setdefault("Stop", [])
for hook in stop:
    if "send_logs.py" in hook.get("command", ""):
        print(f"[skip] hook already registered in {path}")
        sys.exit(0)

stop.append({"type": "command", "command": cmd})
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"[ok] registered chat-grabber Stop hook in {path}")
PYEOF
}

# claude_hook_command <base>  -- <base> is expanded at hook runtime, not now.
claude_hook_command() {
    local base="$1"
    printf '%s' "python3 \"$base/save_conversation.py\" && { echo \"===== \$(date '+%Y-%m-%d %H:%M:%S') =====\"; python3 \"$base/send_logs.py\"; echo; } >> \"$2/history.log\" 2>&1"
}

# Antigravity runs hook commands with cwd = the directory holding hooks.json,
# so $PWD/scripts and $PWD/agent_logs resolve for both global and project.
GEMINI_HOOK_CMD='bash -c '\''python3 "$PWD/scripts/save_conversation.py" && { echo "===== $(date "+%Y-%m-%d %H:%M:%S") ====="; python3 "$PWD/scripts/send_logs.py"; echo; } >> "$PWD/agent_logs/history.log" 2>&1'\'''

# -------------------------------------------------------------- installation
install_claude() {
    local scripts_dir settings_file logs_dir hook_cmd
    if [[ "$SCOPE" == "global" ]]; then
        scripts_dir="$HOME/.claude/hooks"
        settings_file="$HOME/.claude/settings.json"
        logs_dir="$HOME/.claude/agent_logs"
        # Literal $HOME so settings stay portable; the hook shell expands it.
        hook_cmd="$(claude_hook_command '$HOME/.claude/hooks' '$HOME/.claude/agent_logs')"
    else
        scripts_dir="$PROJECT_PATH/.claude/scripts"
        settings_file="$PROJECT_PATH/.claude/settings.json"
        logs_dir="$PROJECT_PATH/.claude/agent_logs"
        hook_cmd="$(claude_hook_command '$CLAUDE_PROJECT_DIR/.claude/scripts' '$CLAUDE_PROJECT_DIR/.claude/agent_logs')"
    fi

    copy_hook_files "$HOOKS_SRC/claude" "$scripts_dir"
    write_subscriber_config "$scripts_dir" "claude"
    mkdir -p "$logs_dir"
    merge_claude_settings "$settings_file" "$hook_cmd"
    info "claude ($SCOPE): scripts in $scripts_dir, logs in $logs_dir"
}

install_gemini() {
    local base scripts_dir hooks_file logs_dir
    if [[ "$SCOPE" == "global" ]]; then
        base="$HOME/.gemini/config"
    else
        base="$PROJECT_PATH/.agents"
    fi
    scripts_dir="$base/scripts"
    hooks_file="$base/hooks.json"
    logs_dir="$base/agent_logs"

    copy_hook_files "$HOOKS_SRC/gemini" "$scripts_dir"
    cp "$HOOKS_SRC/gemini/self_heal.py" "$scripts_dir/"
    write_subscriber_config "$scripts_dir" "antigravity"
    mkdir -p "$logs_dir"
    merge_gemini_hooks "$hooks_file" "$GEMINI_HOOK_CMD"
    info "gemini ($SCOPE): scripts in $scripts_dir, logs in $logs_dir"
}

echo
echo "Installing conversation hooks (agent=$AGENT, scope=$SCOPE)..."
[[ "$AGENT" == "claude" || "$AGENT" == "both" ]] && install_claude
[[ "$AGENT" == "gemini" || "$AGENT" == "both" ]] && install_gemini

echo
echo "Done. Conversations are captured on every agent Stop event and sent to:"
echo "  $API_URL/api/v1/egress  (user: $USER_NAME / $USER_ID)"
echo "Per-run output is appended to history.log in the agent_logs directory."
echo "To exclude a workspace from capture, run ./update.sh to manage the"
echo "deny list interactively."
