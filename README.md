# Cli Agent Subscriber

Capture your Claude Code and Gemini/Antigravity conversations automatically and
publish them to [agent-cli-dashboard](https://agent-cli-dashboard.onrender.com/) —
no MongoDB credentials or pip installs needed on your machine.

## How it works

Both CLIs support **Stop hooks** that fire when the agent finishes a turn:

1. `save_conversation.py` parses the agent transcript into structured
   `[{Input, Tools Used, Output, completed At}]` exchanges and appends them to a
   per-session JSON file under `agent_logs/`.
2. `send_logs.py` (stdlib only) POSTs every session file to the hosted FastAPI
   egress server, which upserts the entries into MongoDB keyed by
   `(session_id, cli_agent, entry_index)` — idempotent, so whole files are
   resent each run and failed sends self-heal on the next turn.

## Install (hooks)

```bash
git clone <this repo>
cd cli-agent-prompt-subscriber
./install.sh
```

The installer prompts for anything not passed as a flag:

```bash
./install.sh --user-id my-id --user-name "My Name" \
             --agent both --scope global \
             --api-key <key-you-were-given>
```

| Flag | Values | Meaning |
|---|---|---|
| `--user-id` / `--user-name` | any | Your identity from the CLI dashboard app (mandatory) |
| `--agent` | `claude`, `gemini`, `both` | Which CLI(s) to hook |
| `--scope` | `global`, `project` | Install for all sessions or one project |
| `--project-path` | dir | Project root (project scope only, defaults to `$PWD`) |
| `--api-url` | URL | Egress server (defaults to the hosted one) |
| `--api-key` | secret | Your personal key, generated in the CLI dashboard app (mandatory) |

The API key is stored in a `secrets.txt` (mode 600) next to the installed
scripts and sent as `X-API-Key` on every POST. The server only writes to the
database when the key matches the one stored on your user document, so each
user authenticates individually.

Install locations (created/merged, never clobbered — existing hooks and
settings are preserved):

| | Scripts | Hook registration | Logs |
|---|---|---|---|
| Claude global | `~/.claude/hooks/` | `~/.claude/settings.json` | `~/.claude/agent_logs/` |
| Claude project | `<proj>/.claude/scripts/` | `<proj>/.claude/settings.json` | `<proj>/.claude/agent_logs/` |
| Gemini global | `~/.gemini/config/scripts/` | `~/.gemini/config/hooks.json` | `~/.gemini/config/agent_logs/` |
| Gemini project | `<proj>/.agents/scripts/` | `<proj>/.agents/hooks.json` | `<proj>/.agents/agent_logs/` |

Each run's egress output is appended to `history.log` in the logs directory.

Requirements: `bash` and `python3` (both preinstalled on macOS/Linux).

## Exclude workspaces (update.sh)

Workspaces on the deny list are never captured or sent. Manage it with:

```bash
./update.sh                        # global installs
./update.sh --project-path <dir>   # also offer a project install
```

It's fully interactive, in the style of the Claude Code pickers — navigate
with ↑/↓ (or `j`/`k`), select with Enter, go back with `q`:

1. **Pick a deny list** — auto-detects your installed hooks (Claude/Gemini ×
   global/project) and only offers the ones that exist; "Enter a project
   path..." lets you point at any other project install.
2. **Choose an action** — view the list, add a directory, or remove one
   (removal is an arrow-key pick from the current entries, no typing).

Adding supports tab-completion and `~`, resolves relative paths against your
current directory, normalizes the result (the hooks compare workspace paths
verbatim), skips duplicates, and asks for confirmation before adding a
directory that doesn't exist. Behind the scenes it edits the
`denied_list.json` next to the installed scripts — hand-editing that file
still works too; update.sh is just the safe, guided way to do it.

## Run the egress server (operators only)

Hook users never need this — only whoever hosts the API:

```bash
cd server
cp .env.example .env   # set MONGODB_URI, DB_NAME
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

Endpoints:

- `GET /health` — liveness check.
- `POST /api/v1/egress` — body
  `{user_id, user_name, cli_agent, session_id, entries: [...]}`. The
  `X-API-Key` header must match the `api_key` stored on the user's document
  in the `users` collection (created by the CLI dashboard app — this server
  never creates users); on success the entries are upserted into `logs` and
  `{inserted, skipped, total}` is returned, otherwise 401.
