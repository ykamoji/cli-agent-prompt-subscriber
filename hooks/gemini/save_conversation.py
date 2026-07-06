import sys
import json
import os
from datetime import datetime, timezone
from self_heal import main as heal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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

    return any(path in deny_list for path in workspacePaths or [])


def parse_transcript(transcript_path):
    turns = []
    current_turn = None

    if not os.path.exists(transcript_path):
        return turns

    with open(transcript_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue

            step_type = step.get("type")
            source = step.get("source")
            content = step.get("content", "")

            # Start of a new turn when USER_INPUT is seen
            if step_type == "USER_INPUT" or (source == "USER_EXPLICIT" and step_type == "USER_INPUT"):
                if current_turn:
                    turns.append(current_turn)
                current_turn = {
                    "Input": content,
                    "Tools Used": [],
                    "Output": ""
                }
            elif current_turn:
                # Check for tool calls in PLANNER_RESPONSE
                if step_type == "PLANNER_RESPONSE" and "tool_calls" in step:
                    tool_calls = step["tool_calls"]
                    if tool_calls:
                        for tc in tool_calls:
                            current_turn["Tools Used"].append({
                                "tool": tc.get("name"),
                                "arguments": tc.get("args"),
                                "result": None
                            })
                # Check for tool results
                elif source == "MODEL" and step_type not in ("PLANNER_RESPONSE", "CONVERSATION_HISTORY", "KNOWLEDGE_ARTIFACTS"):
                    # Find the last tool call without a result and associate it
                    assigned = False
                    for tool_use in reversed(current_turn["Tools Used"]):
                        if tool_use["result"] is None:
                            tool_use["result"] = content
                            assigned = True
                            break
                    if not assigned:
                        current_turn["Tools Used"].append({
                            "tool": step_type,
                            "arguments": None,
                            "result": content
                        })
                # Check for final model output
                elif source == "MODEL" and step_type == "PLANNER_RESPONSE" and content:
                    current_turn["Output"] = content

    if current_turn:
        # Stamp each exchange with the completion time (local tz, ISO 8601 w/ offset).
        completed_at = datetime.now(timezone.utc).astimezone().isoformat()
        current_turn["completed At"] = completed_at
        turns.append(current_turn)

    return turns

def main():
    # 1. Antigravity pipes the event payload to stdin
    output_dir = os.path.join(os.getcwd(), "agent_logs")
    try:
        input_data = sys.stdin.read()

        # DEBUG: Log the input data
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "debug_payload.json"), "w") as f:
            f.write(input_data)

        payload = json.loads(input_data)
    except Exception as e:
        with open(os.path.join(output_dir, "debug_error.log"), "w") as f:
            f.write(str(e))
        # Failsafe: always return empty JSON so the IDE doesn't hang
        print("{}")
        return

    # Extract metadata provided by Antigravity
    transcript_path = payload.get("transcriptPath")
    conversation_id = payload.get("conversationId")

    workspacePaths = payload.get("workspacePaths")

    # denied_list.json lives next to this script so the same code works for
    # both global (~/.gemini/config/scripts) and project (.agents/scripts) installs.
    if check_denied_workspace(workspacePaths, SCRIPT_DIR):
        warn_log = os.path.join(output_dir, "permissions.log")
        with open(warn_log, 'a', encoding='utf-8') as warn_f:
            warn_f.write(f"Permission not allowed for {str(workspacePaths)}. Skipping the hook.\n")
        print("{}")
        return

    if not conversation_id and "id" in payload:
        conversation_id = payload.get("id")
    if not conversation_id:
        conversation_id = "unknown_session"

    if not transcript_path and conversation_id != "unknown_session":
        app_data_dir = os.path.expanduser("~/.gemini/antigravity-ide")
        transcript_path = os.path.join(app_data_dir, "brain", conversation_id, ".system_generated", "logs", "transcript.jsonl")

    # 2. Parse and save the structured conversation
    if transcript_path:
        os.makedirs(output_dir, exist_ok=True)

        destination = os.path.join(output_dir, f"{conversation_id}.json")

        try:
            existing_data = []
            if os.path.exists(destination):
                try:
                    with open(destination, 'r', encoding='utf-8') as in_f:
                        existing_data = json.load(in_f)
                        if not isinstance(existing_data, list):
                            existing_data = []
                except Exception:
                    existing_data = []

            structured_data = parse_transcript(transcript_path)

            if existing_data:
                last_existing = existing_data[-1]
                match_idx = -1
                # Find the latest turn in structured_data that matches the last existing turn's Input and Output
                for i in range(len(structured_data) - 1, -1, -1):
                    cand = structured_data[i]
                    if cand.get("Input") == last_existing.get("Input") and cand.get("Output") == last_existing.get("Output"):
                        match_idx = i
                        break

                if match_idx != -1:
                    new_turns = structured_data[match_idx + 1:]
                else:
                    new_turns = structured_data
            else:
                new_turns = structured_data

            existing_data.extend(new_turns)

            with open(destination, 'w', encoding='utf-8') as out_f:
                json.dump(existing_data, out_f, indent=2, ensure_ascii=False)
        except Exception as e:
            # You can log this to a local debug file if needed
            debug_log = os.path.join(output_dir, "error.log")
            with open(debug_log, 'a', encoding='utf-8') as err_f:
                err_f.write(f"Error parsing/saving transcript: {str(e)}\n")

    # 3. Contract requirement: Must return JSON to stdout
    print(json.dumps({}))

if __name__ == "__main__":
    main()
    heal()
