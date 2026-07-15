# /// script
# requires-python = ">=3.10"
# ///
"""
PostToolUse Hook: Validate code quality after file writes/edits.
Runs linter and formatter checks. Exit code 2 blocks if quality fails.

Security Profile Aware:
- strict: Blocks on quality failures
- standard: Warns on quality failures
- minimal: Disabled
"""

import json
import os
import subprocess
import sys

# Import hook_config for profile-aware behavior
try:
    from hook_config import get_exit_code, is_enabled
except ImportError:
    # Fallback if hook_config not available
    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True


HOOK_NAME = "validate_quality"

# Map file extensions to their quality check commands
QUALITY_CHECKS = {
    ".py": [
        {"name": "ruff-check", "cmd": ["ruff", "check", "--fix"], "fix": True},
        {"name": "ruff-format", "cmd": ["ruff", "format"], "fix": True},
    ],
    ".ts": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
    ".tsx": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
    ".js": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
    ".jsx": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
}


def main():
    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    if tool_name not in ("Write", "Edit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        sys.exit(0)

    _, ext = os.path.splitext(file_path)
    checks = QUALITY_CHECKS.get(ext, [])

    if not checks:
        sys.exit(0)

    errors = []
    for check in checks:
        try:
            result = subprocess.run(
                check["cmd"] + [file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 and not check.get("fix"):
                errors.append(f"{check['name']}: {result.stderr[:500]}")
        except FileNotFoundError:
            # Tool not installed, skip
            continue
        except subprocess.TimeoutExpired:
            continue

    if errors:
        error_msg = "\n".join(errors)
        exit_code = get_exit_code(HOOK_NAME, issue_found=True)
        decision = "block" if exit_code == 2 else "warn"
        print(
            json.dumps(
                {
                    "decision": decision,
                    "reason": f"Quality check {'failed' if decision == 'block' else 'warning'}:\n{error_msg}",
                }
            )
        )
        sys.exit(exit_code)

    sys.exit(0)


if __name__ == "__main__":
    main()
