# /// script
# requires-python = ">=3.10"
# ///
"""
Stop Hook: Validate that the agent actually completed its task.
Can force continuation if completion criteria aren't met.

Returns JSON with "continue": true to keep the agent working.

Also displays a session summary when the session ends, including:
- Duration
- Tool call counts
- Tasks completed
- Commits created
- Health score and change
- Alert summary
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def find_project_root() -> Path | None:
    """Find project root by looking for .claude directory."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / ".claude").is_dir():
            return parent
    return None


# Find project root for all path operations
PROJECT_ROOT = find_project_root() or Path.cwd()


# -----------------------------------------------------------------------------
# Session Summary Functions
# -----------------------------------------------------------------------------


def load_session_start_time() -> datetime | None:
    """
    Load the session start time from logs/sessions.jsonl.
    Returns the most recent session start time.
    """
    log_file = PROJECT_ROOT / "logs" / "sessions.jsonl"
    if not os.path.exists(log_file):
        return None

    try:
        last_session = None
        with open(log_file) as f:
            for line in f:
                if line.strip():
                    last_session = json.loads(line)

        if last_session and "session_start" in last_session:
            start_str = last_session["session_start"]
            # Handle timezone format variations
            return datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except (json.JSONDecodeError, OSError, ValueError):
        pass

    return None


def load_session_state() -> dict:
    """
    Load session state from .company/session_state.json.
    Falls back to sessions.jsonl for start_time if needed.
    """
    state_file = PROJECT_ROOT / ".company" / "session_state.json"
    state = {}

    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # If no start_time in state, try to get from sessions.jsonl
    if "start_time" not in state:
        start = load_session_start_time()
        if start:
            state["start_time"] = start.isoformat()

    return state


def count_tool_calls(session_id: str | None = None) -> dict:
    """
    Count tool calls from the activity log.
    If session_id is provided, filter by that session.
    Otherwise, count all tools from the current session (based on recent timestamps).
    """
    log_file = PROJECT_ROOT / "logs" / "activity.jsonl"
    if not os.path.exists(log_file):
        return {}

    tool_counts: Counter = Counter()
    session_start = load_session_start_time()

    try:
        with open(log_file) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)

                    # Filter by session_id if provided
                    if session_id and entry.get("session_id") != session_id:
                        continue

                    # Otherwise filter by timestamp (after session start)
                    if not session_id and session_start:
                        entry_time_str = entry.get("timestamp", "")
                        if entry_time_str:
                            entry_time = datetime.fromisoformat(
                                entry_time_str.replace("Z", "+00:00")
                            )
                            if entry_time < session_start:
                                continue

                    tool_name = entry.get("tool", "unknown")
                    tool_counts[tool_name] += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    return dict(tool_counts)


def count_commits_in_session() -> int:
    """
    Count git commits created during the current session.
    Uses git log --since with the session start time.
    """
    session_start = load_session_start_time()
    if not session_start:
        return 0

    # Format for git log --since
    since_str = session_start.strftime("%Y-%m-%d %H:%M:%S")

    import subprocess

    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_str}", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.getcwd(),
        )
        if result.returncode == 0:
            lines = [line for line in result.stdout.strip().split("\n") if line.strip()]
            return len(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return 0


def count_tasks_completed_in_session() -> int:
    """
    Count tasks marked as completed during the session.
    Looks in activity log for Task tool calls or git commits with task IDs.
    """
    # For now, we approximate by counting commits (1 task = 1 commit pattern)
    return count_commits_in_session()


def get_current_health() -> int:
    """
    Get current health score from dashboard_aggregator.
    Returns a default of 75 if unavailable.
    """
    try:
        # Add hooks/company to path for import
        hooks_company_dir = str(PROJECT_ROOT / ".claude" / "hooks" / "company")
        if hooks_company_dir not in sys.path:
            sys.path.insert(0, hooks_company_dir)

        from dashboard_aggregator import aggregate_health

        health_data = aggregate_health()
        return health_data.get("health_score", 75)
    except (ImportError, Exception):
        return 75


def get_alert_summary() -> dict:
    """
    Get alert summary for the session.
    Returns counts of resolved and new alerts.
    """
    # For now, return placeholder - could be extended to read from alerts log
    return {"resolved": 0, "new": 0}


def format_duration(delta: timedelta) -> str:
    """Format timedelta as 'Xh Ym'."""
    total_seconds = int(delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_session_summary(session_id: str | None = None) -> dict:
    """
    Generate session summary statistics.

    Args:
        session_id: Optional session ID to filter activity

    Returns:
        Dict with session statistics
    """
    # Load session state
    session_state = load_session_state()
    start_time_str = session_state.get("start_time")
    start_health = session_state.get("start_health", 75)

    # Calculate duration
    if start_time_str:
        try:
            start = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            duration = datetime.now(timezone.utc) - start
            duration_str = format_duration(duration)
        except ValueError:
            duration_str = "Unknown"
    else:
        duration_str = "Unknown"

    # Count tool calls from activity log
    tool_counts = count_tool_calls(session_id)
    total_calls = sum(tool_counts.values())

    # Format tool breakdown (top tools)
    top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:4]
    tool_breakdown_str = ", ".join(f"{name}: {count}" for name, count in top_tools)

    # Count tasks and commits
    commits = count_commits_in_session()
    tasks = count_tasks_completed_in_session()

    # Get current health
    current_health = get_current_health()
    health_change = current_health - start_health

    # Get alert summary
    alerts = get_alert_summary()

    return {
        "duration": duration_str,
        "tool_calls": total_calls,
        "tool_breakdown": tool_breakdown_str,
        "tool_counts": tool_counts,
        "tasks_completed": tasks,
        "commits_created": commits,
        "patterns_captured": 0,  # Placeholder for future pattern detection
        "health_current": current_health,
        "health_start": start_health,
        "health_change": health_change,
        "alerts_resolved": alerts["resolved"],
        "alerts_new": alerts["new"],
    }


def render_session_summary(summary: dict) -> str:
    """
    Render the session summary as formatted text.

    Args:
        summary: Session summary dict from get_session_summary()

    Returns:
        Formatted string for display
    """
    # Health change indicator
    health_change = summary["health_change"]
    if health_change > 0:
        health_indicator = f"(+{health_change} from session start)"
    elif health_change < 0:
        health_indicator = f"({health_change} from session start)"
    else:
        health_indicator = "(unchanged)"

    # Tool calls line
    tool_line = f"Tool calls: {summary['tool_calls']}"
    if summary["tool_breakdown"]:
        tool_line += f" ({summary['tool_breakdown']})"

    lines = [
        "",
        "-" * 61,
        "SESSION SUMMARY",
        "-" * 61,
        f"Duration: {summary['duration']}",
        tool_line,
        f"Tasks completed: {summary['tasks_completed']}",
        f"Commits created: {summary['commits_created']}",
        "",
        f"Health: {summary['health_current']}/100 {health_indicator}",
        f"Alerts: {summary['alerts_resolved']} resolved, {summary['alerts_new']} new",
        "-" * 61,
        "",
    ]

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Main Hook Logic
# -----------------------------------------------------------------------------


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    _ = input_data.get("stop_reason", "")  # Reserved for future filtering
    transcript = input_data.get("transcript", [])
    session_id = input_data.get("session_id")

    # Check if the last few messages indicate incomplete work
    last_messages = transcript[-5:] if len(transcript) >= 5 else transcript

    # Look for signs of incomplete work
    incomplete_signals = [
        "TODO",
        "FIXME",
        "not yet implemented",
        "will do this later",
        "skipping for now",
        "placeholder",
    ]

    last_assistant_msg = ""
    for msg in reversed(last_messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_assistant_msg = content
            elif isinstance(content, list):
                last_assistant_msg = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            break

    for signal in incomplete_signals:
        if signal.lower() in last_assistant_msg.lower():
            result = {
                "continue": True,
                "reason": f"Incomplete work detected: found '{signal}' in response. Please complete all work before stopping.",
            }
            print(json.dumps(result))
            sys.exit(0)

    # Generate and print session summary
    try:
        summary = get_session_summary(session_id)
        print(render_session_summary(summary), file=sys.stderr)
    except Exception:
        # Don't fail the hook if summary generation fails
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
