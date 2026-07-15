# /// script
# requires-python = ">=3.10"
# ///
"""
SubagentStop Hook: Validate sub-agent output quality.
Ensures sub-agents deliver complete, structured results.
Also handles auto-consultant lifecycle updates.
"""

import json
import os
import sys

# Import consultant_lifecycle functions with graceful fallback
_consultant_lifecycle_available = False
try:
    # Add the company hooks directory to the path for imports
    _hooks_dir = os.path.dirname(os.path.abspath(__file__))
    _company_hooks_dir = os.path.join(_hooks_dir, "company")
    if _company_hooks_dir not in sys.path:
        sys.path.insert(0, _company_hooks_dir)

    from consultant_lifecycle import update_consultant_status

    _consultant_lifecycle_available = True
except ImportError:
    # Fallback: consultant_lifecycle not available
    def update_consultant_status(consultant_id, status, context=None, company_dir=None):
        return {
            "success": False,
            "reason": "import_error",
            "message": "consultant_lifecycle module not available",
        }


def _extract_learnings_from_output(output: str) -> dict | None:
    """
    Extract learnings/insights from subagent output.

    Looks for patterns like:
    - "Learned: ..."
    - "Key insight: ..."
    - "Pattern discovered: ..."
    - "## Learnings" sections

    Returns dict with extracted learnings or None if none found.
    """
    if not output:
        return None

    learnings = {
        "patterns": [],
        "insights": [],
        "decisions": [],
    }

    _ = output.lower()  # Reserved for case-insensitive matching

    # Look for explicit learning markers
    learning_markers = [
        ("learned:", "insights"),
        ("key insight:", "insights"),
        ("pattern discovered:", "patterns"),
        ("pattern identified:", "patterns"),
        ("decision:", "decisions"),
        ("decided to:", "decisions"),
    ]

    lines = output.split("\n")
    for line in lines:
        line_lower = line.lower().strip()
        for marker, category in learning_markers:
            if marker in line_lower:
                # Extract the content after the marker
                idx = line_lower.find(marker)
                content = line[idx + len(marker) :].strip()
                if content:
                    learnings[category].append(content)

    # Check if any learnings were found
    total_learnings = sum(len(v) for v in learnings.values())
    if total_learnings == 0:
        return None

    return learnings


def _handle_auto_consultant(input_data: dict) -> None:
    """
    Handle auto-consultant lifecycle updates after subagent completion.

    If the subagent was an auto-consultant:
    - Updates status to "available"
    - Captures any learnings from the output
    - Updates lastActive timestamp (handled by update_consultant_status)
    """
    if not _consultant_lifecycle_available:
        return

    # Check if this subagent is an auto-consultant
    # Check both top-level and metadata sub-object for backward compatibility
    metadata = input_data.get("metadata", {})
    is_auto_consultant = input_data.get("is_auto_consultant", False) or metadata.get(
        "is_auto_consultant", False
    )
    if not is_auto_consultant:
        return

    consultant_id = input_data.get("consultant_id") or metadata.get("consultant_id")
    if not consultant_id:
        return

    output = input_data.get("output", "")

    # Extract learnings from the output
    learnings = _extract_learnings_from_output(output)

    # Build context string for the status update
    context_parts = ["Subagent completed"]
    if learnings:
        learning_count = sum(len(v) for v in learnings.values())
        context_parts.append(f"{learning_count} learnings captured")

    context = ". ".join(context_parts)

    # Update consultant status to available
    try:
        result = update_consultant_status(
            consultant_id=consultant_id,
            status="available",
            context=context,
        )
        # Log result for debugging (non-blocking)
        if not result.get("success"):
            # Could log to stderr or audit log, but don't fail the hook
            pass
    except Exception:
        # Gracefully handle any errors - don't fail the hook
        pass


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    agent_type = input_data.get("subagent_type", "")
    output = input_data.get("output", "")

    # Handle auto-consultant lifecycle updates (non-blocking)
    _handle_auto_consultant(input_data)

    # Validate that architect agents produce structured plans
    if agent_type == "Plan" or "architect" in agent_type.lower():
        required_sections = ["##", "step", "file"]
        missing = [s for s in required_sections if s.lower() not in output.lower()]
        if missing:
            result = {
                "continue": True,
                "reason": f"Architect output missing structure. Expected sections containing: {', '.join(missing)}. Please provide a complete plan with steps and files.",
            }
            print(json.dumps(result))
            sys.exit(0)

    # Validate that reviewer agents provide actionable feedback
    if "review" in agent_type.lower():
        if len(output.strip()) < 50:
            result = {
                "continue": True,
                "reason": "Review output too brief. Please provide detailed feedback with specific file references and line numbers.",
            }
            print(json.dumps(result))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
