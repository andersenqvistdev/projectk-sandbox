# /// script
# requires-python = ">=3.10"
# ///
"""
Utility functions for Forge hooks.
Provides common functionality like project root detection.
"""

import os
import sys
from pathlib import Path


def find_project_root() -> Path | None:
    """
    Find the project root by looking for .claude directory.
    Walks up from current directory and from the hook's own location.

    Returns the project root path or None if not found.
    """
    # Strategy 1: Walk up from current working directory
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent

    # Strategy 2: Walk up from the hook script's location
    # This works even if cwd is wrong
    if __file__:
        script_path = Path(__file__).resolve()
        for parent in script_path.parents:
            if (parent / ".claude").is_dir():
                return parent

    return None


def get_hooks_dir() -> Path | None:
    """Get the .claude/hooks directory path."""
    root = find_project_root()
    if root:
        return root / ".claude" / "hooks"
    return None


def ensure_project_context():
    """
    Ensure we're operating in the correct project context.
    Changes to project root if needed.

    Returns True if context is valid, False otherwise.
    """
    root = find_project_root()
    if root is None:
        return False

    # If cwd is not the project root, change to it
    if Path.cwd() != root:
        os.chdir(root)

    return True


def get_input_json() -> dict:
    """
    Read JSON input from stdin (standard hook input).
    Returns empty dict if no input or invalid JSON.
    """
    import json

    try:
        input_data = sys.stdin.read()
        if input_data.strip():
            return json.loads(input_data)
    except (json.JSONDecodeError, IOError):
        pass
    return {}


# Export for use by other hooks
__all__ = [
    "find_project_root",
    "get_hooks_dir",
    "ensure_project_context",
    "get_input_json",
]
