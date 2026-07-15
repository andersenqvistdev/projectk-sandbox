# /// script
# requires-python = ">=3.10"
# ///
"""
PreCompact Hook: Back up conversation transcript before compression.
Preserves full context history for debugging and analysis.
"""

import json
import os
import sys
from datetime import datetime, timezone


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    transcript = input_data.get("transcript", [])
    session_id = input_data.get("session_id", "unknown")

    if not transcript:
        sys.exit(0)

    backup_dir = os.path.join(os.getcwd(), "logs", "transcripts")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"transcript_{session_id}_{timestamp}.json"

    backup = {
        "session_id": session_id,
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "message_count": len(transcript),
        "transcript": transcript,
    }

    with open(os.path.join(backup_dir, filename), "w") as f:
        json.dump(backup, f, indent=2)

    print(f"Transcript backed up: {filename} ({len(transcript)} messages)")
    sys.exit(0)


if __name__ == "__main__":
    main()
