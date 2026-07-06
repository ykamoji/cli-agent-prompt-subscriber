"""POST conversation logs to the egress server.

Replaces the local run_egress.sh + egress.py + MongoDB setup: reads every
session JSON file in the agent_logs directory and sends its entries to the
hosted FastAPI egress server, which performs the MongoDB writes.

Stdlib only -- no pip installs needed on user machines.

Configuration lives in ``subscriber_config.json`` next to this script
(written by install.sh):

    {
      "user_id": "...",
      "user_name": "...",
      "api_url": "https://...",
      "cli_agent": "claude" | "antigravity",
      "log_dir": "../agent_logs"        # resolved relative to this script
    }

The per-user API key (generated in the CLI dashboard app and stored on the
user's document in the backend) lives in ``secrets.txt`` next to this script
and is sent as the X-API-Key header; the server only writes to the DB when
it matches the key stored for the user_id.

Whole files are resent every run; the server dedupes by
(session_id, cli_agent, entry_index), so this is idempotent and failed
sends self-heal on the next hook invocation.
"""

import json
import os
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "subscriber_config.json")
SECRETS_FILE = os.path.join(SCRIPT_DIR, "secrets.txt")

# Files in the log directory that are not conversation logs.
IGNORED_FILES = {"debug_payload.json", "denied_list.json", "subscriber_config.json"}

REQUIRED_KEYS = ("user_id", "user_name", "api_url", "cli_agent", "log_dir")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing:
        raise ValueError(f"subscriber_config.json is missing: {', '.join(missing)}")
    with open(SECRETS_FILE, "r", encoding="utf-8") as f:
        config["api_key"] = f.read().strip()
    if not config["api_key"]:
        raise ValueError(f"{SECRETS_FILE} is empty; put your dashboard API key in it")
    return config


def send_session(config, session_id, entries):
    """POST one session's entries; returns the server's response dict."""
    body = {
        "user_id": config["user_id"],
        "user_name": config["user_name"],
        "cli_agent": config["cli_agent"],
        "session_id": session_id,
        "entries": entries,
    }
    url = config["api_url"].rstrip("/") + "/api/v1/egress"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": config["api_key"],
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main():
    try:
        config = load_config()
    except Exception as exc:
        print(f"[error] could not load configuration: {exc}", file=sys.stderr)
        return 1

    log_dir = os.path.abspath(os.path.join(SCRIPT_DIR, config["log_dir"]))
    if not os.path.isdir(log_dir):
        print(f"[warn] missing log directory, nothing to send: {log_dir}")
        return 0

    failures = 0
    sent_any = False
    for name in sorted(os.listdir(log_dir)):
        if not name.endswith(".json") or name in IGNORED_FILES:
            continue
        path = os.path.join(log_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] could not read {name}: {exc}")
            continue

        # A file is normally an array of entries; tolerate a single object.
        entries = payload if isinstance(payload, list) else [payload]
        entries = [e if isinstance(e, dict) else {"value": e} for e in entries]
        if not entries:
            continue

        session_id = os.path.splitext(name)[0]
        try:
            result = send_session(config, session_id, entries)
            sent_any = True
            print(
                f"[ok] {name}: {result.get('inserted', 0)} new, "
                f"{result.get('skipped', 0)} already present "
                f"(cli_agent={config['cli_agent']})"
            )
        except urllib.error.HTTPError as exc:
            failures += 1
            print(f"[error] {name}: server returned {exc.code} {exc.reason}")
        except Exception as exc:
            failures += 1
            print(f"[error] {name}: {exc}")

    if not sent_any and not failures:
        print("[info] no conversation logs to send")
    print(f"[done] egress complete ({failures} failures)")
    # Always exit 0: this is the last hook step and errors are already logged;
    # unsent files are retried on the next Stop event.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
