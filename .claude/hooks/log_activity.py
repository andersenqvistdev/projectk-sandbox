# /// script
# requires-python = ">=3.10"
# ///
"""
PostToolUse Hook: Log all tool activity to JSON audit trail.
Critical for observability — understand what the agent did and why.

Agent Attribution:
- Captures agent context from environment variables (set by agent spawner)
- Falls back to session context from input data
- Enables filtering logs by agent, role, department, or task

Executive Decision Tracking:
- CEO and CTO actions tagged with executive_decision: true
- Decision types: business_alignment, technical_validation, hiring, phase_transition
- Phase context attached from STATE.md
- Enables filtering logs by executive_decision: true
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_project_root() -> Path | None:
    """Find project root by looking for .claude directory."""
    # Strategy 1: Walk up from cwd
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent
    # Strategy 2: Walk up from script location
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / ".claude").is_dir():
            return parent
    return None


# Find project root and set paths accordingly
PROJECT_ROOT = find_project_root() or Path.cwd()
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "activity.jsonl"

# Executive roles that trigger executive decision tagging
EXECUTIVE_ROLES = {"ceo", "cto"}

# Decision type detection patterns
DECISION_PATTERNS = {
    "hiring": [
        r"hire",
        r"recruit",
        r"onboard",
        r"employee",
        r"workforce",
        r"add[-_]?agent",
        r"spawn[-_]?agent",
        r"new[-_]?agent",
    ],
    "phase_transition": [
        r"phase[-_]?transition",
        r"lifecycle",
        r"growth",
        r"scale",
        r"startup",
        r"mature",
        r"decline",
        r"pivot",
        r"next[-_]?phase",
    ],
    "technical_validation": [
        r"architecture",
        r"design",
        r"review",
        r"validate",
        r"approve",
        r"technical",
        r"implementation",
        r"code[-_]?review",
        r"security",
    ],
    "business_alignment": [
        r"strategy",
        r"roadmap",
        r"priority",
        r"business",
        r"align",
        r"goal",
        r"objective",
        r"milestone",
        r"direction",
    ],
}


def get_current_phase() -> str | None:
    """
    Read the current company phase from STATE.md.

    Returns:
        Phase string (e.g., "v1.6 Living Company - PLANNING COMPLETE") or None if not found.
    """
    # Try multiple locations for STATE.md
    state_paths = [
        Path(os.getcwd()) / ".planning" / "STATE.md",
        Path(os.environ.get("FORGE_PROJECT_ROOT", os.getcwd()))
        / ".planning"
        / "STATE.md",
    ]

    for state_path in state_paths:
        if state_path.exists():
            try:
                content = state_path.read_text()
                # Look for Phase line in Last Session section
                # Format: - **Phase:** v1.6 Living Company - PLANNING COMPLETE
                match = re.search(r"\*\*Phase:\*\*\s*(.+?)(?:\n|$)", content)
                if match:
                    return match.group(1).strip()
            except (OSError, IOError):
                continue

    return None


def detect_decision_type(tool_name: str, tool_input: dict, agent_role: str) -> str:
    """
    Detect the type of executive decision based on context.

    Args:
        tool_name: The tool being used
        tool_input: The tool input parameters
        agent_role: The role of the agent (ceo or cto)

    Returns:
        Decision type: "business_alignment", "technical_validation", "hiring", or "phase_transition"
    """
    # Build context string from tool usage
    context_parts = [tool_name.lower()]

    if tool_name == "Bash":
        context_parts.append(tool_input.get("command", "").lower())
    elif tool_name in ("Read", "Write", "Edit"):
        context_parts.append(tool_input.get("file_path", "").lower())
    elif tool_name == "Task":
        context_parts.append(tool_input.get("description", "").lower())
        context_parts.append(tool_input.get("subagent_type", "").lower())
    else:
        context_parts.append(str(tool_input).lower())

    context = " ".join(context_parts)

    # Check patterns in priority order
    for decision_type, patterns in DECISION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, context, re.IGNORECASE):
                return decision_type

    # Default based on role
    if agent_role == "cto":
        return "technical_validation"
    return "business_alignment"


def get_executive_metadata(
    agent_context: dict, tool_name: str, tool_input: dict
) -> dict | None:
    """
    Generate executive decision metadata for CEO/CTO actions.

    Args:
        agent_context: The agent context dict (must have agent_role)
        tool_name: The tool being used
        tool_input: The tool input parameters

    Returns:
        Executive metadata dict or None if not an executive role.
    """
    agent_role = agent_context.get("agent_role", "").lower()

    if agent_role not in EXECUTIVE_ROLES:
        return None

    decision_type = detect_decision_type(tool_name, tool_input, agent_role)
    phase_context = get_current_phase()

    metadata = {
        "executive_decision": True,
        "decision_type": decision_type,
    }

    if phase_context:
        metadata["phase_context"] = phase_context

    return metadata


def get_agent_context(input_data: dict | None = None) -> dict | None:
    """
    Detect agent context from environment and session.

    Detection strategy (in order of priority):
    1. Environment variables (set by agent spawner)
    2. Session context from input_data
    3. Return None if no context found (fallback handled by caller)

    Returns:
        Agent context dict or None if no context available.
    """
    # Primary: Check environment variables (set by agent spawner)
    agent_id = os.environ.get("FORGE_AGENT_ID")
    if agent_id:
        return {
            "agent_id": agent_id,
            "agent_role": os.environ.get("FORGE_AGENT_ROLE", "agent"),
            "department": os.environ.get("FORGE_AGENT_DEPT"),
            "current_task": os.environ.get("FORGE_CURRENT_TASK"),
        }

    # Secondary: Check input_data for agent metadata
    if input_data:
        # Direct agent_context in input
        if input_data.get("agent_context"):
            ctx = input_data["agent_context"]
            return {
                "agent_id": ctx.get("agent_id", "unknown"),
                "agent_role": ctx.get("agent_role", "agent"),
                "department": ctx.get("department"),
                "current_task": ctx.get("current_task"),
            }

        # Individual fields in input
        if input_data.get("agent_id"):
            return {
                "agent_id": input_data["agent_id"],
                "agent_role": input_data.get("agent_role", "agent"),
                "department": input_data.get("department"),
                "current_task": input_data.get("current_task"),
            }

    # No agent context found
    return None


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "unknown")
    tool_input = input_data.get("tool_input", {})
    tool_output = input_data.get("tool_output", {})
    session_id = input_data.get("session_id", "unknown")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool": tool_name,
        "input_summary": _summarize_input(tool_name, tool_input),
        "output_length": len(str(tool_output)),
        "success": not tool_output.get("is_error", False)
        if isinstance(tool_output, dict)
        else True,
    }

    # Add agent context if available (maintains backward compatibility)
    agent_context = get_agent_context(input_data)
    if agent_context:
        entry["agent_context"] = agent_context

        # Add executive decision metadata for CEO/CTO actions
        executive_metadata = get_executive_metadata(
            agent_context, tool_name, tool_input
        )
        if executive_metadata:
            entry.update(executive_metadata)

    os.makedirs(LOG_DIR, exist_ok=True)

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    sys.exit(0)


def _summarize_input(tool_name: str, tool_input: dict) -> str:
    """Create a concise summary of what the tool did without dumping full content."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:200] if len(cmd) > 200 else cmd
    elif tool_name in ("Read", "Write", "Edit"):
        return tool_input.get("file_path", "unknown file")
    elif tool_name == "Grep":
        return f"pattern={tool_input.get('pattern', '')} path={tool_input.get('path', '.')}"
    elif tool_name == "Glob":
        return f"pattern={tool_input.get('pattern', '')}"
    elif tool_name == "Task":
        return f"subagent={tool_input.get('subagent_type', '')} desc={tool_input.get('description', '')[:100]}"
    else:
        return str(tool_input)[:200]


if __name__ == "__main__":
    main()
