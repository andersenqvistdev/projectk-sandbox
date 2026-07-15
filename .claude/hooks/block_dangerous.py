# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Block dangerous commands before execution.
Deterministic safety — never rely on the LLM to avoid destructive ops.

Exit code 2 + JSON with decision:block stops the command.

Security Profile Aware:
- strict/standard: Blocks all dangerous patterns
- minimal: Only blocks catastrophic patterns (rm -rf /, fork bombs, etc.)
"""

import json
import re
import sys

# Import hook_config for profile-aware behavior
try:
    from hook_config import (
        get_exit_code,
        get_hook_behavior,
        get_reduced_patterns,
        is_enabled,
    )
except ImportError:
    # Fallback if hook_config not available
    def get_hook_behavior(hook_name: str) -> str:
        return "block"

    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def get_reduced_patterns(hook_name: str) -> list[str] | None:
        return None

    def is_enabled(hook_name: str) -> bool:
        return True


HOOK_NAME = "block_dangerous"

DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+[/~]",  # rm -rf on root or home
    r"rm\s+-rf\s+\.",  # rm -rf on current dir
    r"rm\s+-rf\s+\*",  # rm -rf wildcard
    r'rm\s+-rf\s+(--\s+)?["\']?\$',  # rm -rf with shell variable (may expand to dangerous path if unset/empty)
    r"sudo\s+rm",  # sudo rm anything
    r"chmod\s+777",  # world-writable permissions
    r"chmod\s+-R\s+777",  # recursive world-writable
    r"mkfs\.",  # format filesystem
    r"dd\s+if=.*of=/dev/",  # raw disk write
    r">\s*/dev/sd",  # overwrite disk device
    r"curl.*\|\s*bash",  # pipe curl to bash
    r"wget.*\|\s*bash",  # pipe wget to bash
    r"curl.*\|\s*sh",  # pipe curl to sh
    r"eval\s*\$\(curl",  # eval curl output
    r"git\s+push\s+--force\s+(origin\s+)?(main|master)",  # force push main
    r"git\s+reset\s+--hard",  # hard reset
    r":\(\)\{.*\|.*&\s*\};:",  # fork bomb
    r"echo\s+.*>\s*/etc/",  # overwrite system config
    r"npm\s+publish",  # accidental publish
]

SENSITIVE_PATH_PATTERNS = [
    r"/etc/passwd",
    r"/etc/shadow",
    r"~/.ssh/",
    r"~/.aws/",
    r"\.env($|\s)",
    r"credentials\.json",
    r"\.pem$",
    r"\.key$",
]


def truncate_command(command: str, max_length: int = 60) -> str:
    """Truncate command for display, preserving readability."""
    if len(command) <= max_length:
        return command
    return command[: max_length - 3] + "..."


def print_block_box(command: str, pattern: str, block_type: str = "dangerous") -> None:
    """Print human-readable error box to stderr."""
    truncated = truncate_command(command)

    # Determine box content based on block type
    if block_type == "dangerous":
        title = "DANGEROUS COMMAND BLOCKED"
        description = "Matched forbidden pattern"
    else:
        title = "SENSITIVE FILE ACCESS BLOCKED"
        description = "Matched sensitive path pattern"

    # Calculate box width (minimum 60 chars)
    content_width = max(60, len(truncated) + 4, len(pattern) + len(description) + 4)

    # Build the box
    top_border = "═" * (content_width + 2)

    print(f"\n╔{top_border}╗", file=sys.stderr)
    print(f"║ {title:^{content_width}} ║", file=sys.stderr)
    print(f"╠{top_border}╣", file=sys.stderr)
    print(f"║ Command: {truncated:<{content_width - 9}} ║", file=sys.stderr)
    print(
        f"║ {description}: {pattern:<{content_width - len(description) - 3}} ║",
        file=sys.stderr,
    )
    print(f"╠{top_border}╣", file=sys.stderr)
    print(
        f"║ {'TIP: Use /gate for legitimate dangerous operations':<{content_width}} ║",
        file=sys.stderr,
    )
    print(f"╚{top_border}╝\n", file=sys.stderr)


def check_command(command: str) -> tuple[dict, str, str] | None:
    """Check command against dangerous patterns.

    Returns tuple of (result_dict, pattern, block_type) or None if safe.
    """
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return (
                {
                    "decision": "block",
                    "reason": f"BLOCKED: Dangerous pattern detected: {pattern}",
                },
                pattern,
                "dangerous",
            )

    for pattern in SENSITIVE_PATH_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return (
                {
                    "decision": "block",
                    "reason": f"BLOCKED: Sensitive file access: {pattern}",
                },
                pattern,
                "sensitive",
            )

    return None


def main():
    try:
        # Check if hook is enabled for current security profile
        if not is_enabled(HOOK_NAME):
            sys.exit(0)

        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        if tool_name != "Bash":
            sys.exit(0)

        command = tool_input.get("command", "")

        # Check for reduced pattern mode (minimal profile)
        reduced_patterns = get_reduced_patterns(HOOK_NAME)
        if reduced_patterns is not None:
            # Only check against reduced catastrophic patterns
            check_result = None
            for pattern in reduced_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    check_result = (
                        {
                            "decision": "block",
                            "reason": f"BLOCKED: Catastrophic pattern detected: {pattern}",
                        },
                        pattern,
                        "dangerous",
                    )
                    break
        else:
            # Standard/strict: check against all patterns
            check_result = check_command(command)

        if check_result:
            result, pattern, block_type = check_result
            # Human-readable output to stderr first
            print_block_box(command, pattern, block_type)
            # JSON output to stdout (unchanged)
            print(json.dumps(result))
            sys.exit(get_exit_code(HOOK_NAME, issue_found=True))

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
