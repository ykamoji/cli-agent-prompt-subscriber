import sys
import os
import json
from datetime import datetime, timezone

# Directory where per-session conversation logs are written. Each session is
# saved as <session_id>.json. Override the base dir with CONVO_LOG_DIR if desired.
DEFAULT_LOG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "agent_logs"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# Markers that identify slash-command output / harness caveats rather than real
# user input. Exchanges whose input is one of these are skipped.
META_INPUT_MARKERS = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)

def check_denied_workspace(workspacePaths, list_dir):
    list_file = os.path.join(list_dir, "denied_list.json")

    deny_list = []
    if os.path.exists(list_file):
        try:
            with open(list_file, 'r', encoding='utf-8') as in_f:
                deny = json.load(in_f)

            deny_list = deny.get("DENY_LIST") or []
        except Exception:
            deny_list = []

    return any(path in deny_list for path in workspacePaths)

def is_meta_input(text):
    stripped = text.lstrip()
    return any(stripped.startswith(marker) for marker in META_INPUT_MARKERS)


def text_from_content(content):
    """Flatten an assistant/user message 'content' field into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def stringify_result(content):
    """Normalize a tool_result content field into a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(json.dumps(block))
            else:
                parts.append(str(block))
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return json.dumps(content)


def parse_transcript(transcript_path):
    """Read a JSONL transcript and group it into Input / Tools Used / Output exchanges."""
    events = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # First pass: map tool_use_id -> result string (tool results arrive in later user turns).
    tool_results = {}
    for event in events:
        message = event.get("message", {})
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_results[block.get("tool_use_id")] = stringify_result(
                    block.get("content")
                )

    exchanges = []
    current = None

    def flush():
        if current is not None and (
            current["Input"] or current["Tools Used"] or current["Output"]
        ):
            exchanges.append(current)

    for event in events:
        etype = event.get("type")
        message = event.get("message", {})
        content = message.get("content")

        if etype == "user":
            user_text = text_from_content(content)
            # Skip pure tool-result turns (no real user text); they belong to the
            # in-progress exchange, not a new one.
            is_tool_result_only = isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if user_text and not is_tool_result_only and not is_meta_input(user_text):
                flush()
                current = {"Input": user_text, "Tools Used": [], "Output": ""}

        elif etype == "assistant":
            if current is None:
                current = {"Input": "", "Tools Used": [], "Output": ""}
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        current["Output"] = (
                            (current["Output"] + "\n" + text).strip()
                            if current["Output"]
                            else text
                        )
                elif btype == "tool_use":
                    current["Tools Used"].append(
                        {
                            "tool": block.get("name", ""),
                            "arguments": block.get("input", {}),
                            "result": tool_results.get(block.get("id"), ""),
                        }
                    )

    flush()
    return exchanges


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)

        payload = json.loads(raw)

        transcript_path = payload.get("transcript_path")
        if not transcript_path or not os.path.isfile(transcript_path):
            sys.exit(0)

        exchanges = parse_transcript(transcript_path)
        if not exchanges:
            sys.exit(0)

        # Name the log file after the session id so each session gets its own file.
        session_id = payload.get("session_id") or os.path.splitext(
            os.path.basename(transcript_path)
        )[0]

        log_dir = os.path.abspath(os.environ.get("CONVO_LOG_DIR", DEFAULT_LOG_DIR))
        log_file = os.path.join(log_dir, f"{session_id}.json")


        workspace = payload.get("cwd")

        # denied_list.json lives next to this script so the same code works for
        # both global (~/.claude/hooks) and project (.claude/scripts) installs.
        denied = check_denied_workspace([workspace], SCRIPT_DIR)

        os.makedirs(log_dir, exist_ok=True)
        warn_log = os.path.join(log_dir, "permissions.log")
        with open(warn_log, 'a', encoding='utf-8') as warn_f:
            warn_f.write(f"Permission{' not' if denied else ''} allowed for {str(workspace)}.\n")

        if denied:
            sys.exit(0)

        # Append-only: load whatever is already saved and keep it untouched so old
        # exchanges (and their timestamps) stay frozen forever -- even if the
        # transcript is later deleted, the log retains the full history.
        saved = []
        if os.path.isfile(log_file):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    saved = loaded
            except (json.JSONDecodeError, OSError):
                saved = []

        # Find where the saved history ends within the freshly-parsed transcript by
        # matching the last saved exchange's Input/Output, then append only what
        # comes after it. If no match (e.g. transcript was reset), append all parsed
        # exchanges that aren't already the saved tail.
        new_exchanges = exchanges
        if saved:
            last = saved[-1]
            last_key = (last.get("Input"), last.get("Output"))
            match_index = None
            for i in range(len(exchanges) - 1, -1, -1):
                if (exchanges[i].get("Input"), exchanges[i].get("Output")) == last_key:
                    match_index = i
                    break
            new_exchanges = (
                exchanges[match_index + 1 :] if match_index is not None else exchanges
            )

        if not new_exchanges:
            sys.exit(0)

        # Only the newly-appended exchanges get a completion timestamp.
        completed_at = datetime.now(timezone.utc).astimezone().isoformat()
        for exchange in new_exchanges:
            exchange["completed At"] = completed_at

        saved.extend(new_exchanges)

        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=2, ensure_ascii=False)

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except Exception as e:
        print(f"Hook Error: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
