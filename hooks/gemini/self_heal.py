import os
import json
import re
from datetime import datetime, timezone

def process_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return

    if not isinstance(data, list):
        return

    modified = False
    for turn in data:
        if "completed At" not in turn:
            last_timestamp = None
            for tool in turn.get("Tools Used", []):
                result = tool.get("result")
                if isinstance(result, str):
                    match = re.search(r"Completed At:\s*([^\n]+)", result)
                    if match:
                        ts_str = match.group(1).strip()
                        try:
                            if ts_str.endswith('Z'):
                                ts_str = ts_str[:-1] + '+05:30'
                            dt = datetime.fromisoformat(ts_str)
                            if last_timestamp is None or dt > last_timestamp:
                                last_timestamp = dt
                        except Exception:
                            pass
            if last_timestamp:
                turn["completed At"] = last_timestamp.astimezone().isoformat()
                modified = True

    if modified:
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Updated {filepath}")
        except Exception as e:
            print(f"Error writing to {filepath}: {e}")

def main():
    output_dir = os.path.join(os.getcwd(), "agent_logs")
    if not os.path.exists(output_dir):
        print(f"Directory not found: {output_dir}")
        return

    for filename in os.listdir(output_dir):
        if filename.endswith(".json"):
            filepath = os.path.join(output_dir, filename)
            process_file(filepath)

if __name__ == "__main__":
    main()
