# /// script
# requires-python = ">=3.10"
# ///
"""
Hook Configuration Module — Central security profile management.

This module reads the security profile from forge-config.json and provides
functions for hooks to determine their behavior based on the active profile.

Security Profiles:
- strict: All hooks block on issues (defense in depth)
- standard: Critical hooks block, others warn (balanced)
- minimal: Only catastrophic operations blocked (relies on /gate)

Usage in hooks:
    from hook_config import get_hook_behavior, should_exit_with_block

    behavior = get_hook_behavior("secrets_scanner")
    if behavior == "off":
        sys.exit(0)  # Hook disabled

    # ... hook logic ...

    if found_issue:
        exit_code = 2 if behavior == "block" else 1
        sys.exit(exit_code)
"""

import json
import sys
from pathlib import Path
from typing import Literal

# Type definitions
HookBehavior = Literal["block", "warn", "off", "on", "block_reduced"]
ProfileName = Literal["strict", "standard", "minimal"]

# Default profile if not configured
DEFAULT_PROFILE: ProfileName = "standard"

# Hook behavior definitions per profile
# This serves as fallback if forge-config.json doesn't have the hook defined
DEFAULT_HOOK_BEHAVIORS: dict[str, dict[ProfileName, HookBehavior]] = {
    "block_dangerous": {
        "strict": "block",
        "standard": "block",
        "minimal": "block_reduced",
    },
    "secrets_scanner": {"strict": "block", "standard": "block", "minimal": "warn"},
    "git_guardian": {"strict": "block", "standard": "block", "minimal": "warn"},
    "lint_on_edit": {"strict": "block", "standard": "warn", "minimal": "off"},
    "prompt_guard": {"strict": "block", "standard": "warn", "minimal": "off"},
    "validate_quality": {"strict": "block", "standard": "warn", "minimal": "off"},
    "dependency_check": {"strict": "warn", "standard": "warn", "minimal": "off"},
    "log_activity": {"strict": "on", "standard": "on", "minimal": "on"},
    "session_init": {"strict": "on", "standard": "on", "minimal": "on"},
    "notify": {"strict": "on", "standard": "on", "minimal": "on"},
}

# Reduced dangerous patterns for minimal profile
# Only blocks truly catastrophic operations
MINIMAL_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+[/~]",  # rm -rf on root or home
    r"rm\s+-rf\s+/",  # rm -rf root
    r"sudo\s+rm\s+-rf",  # sudo rm -rf
    r"mkfs\.",  # format filesystem
    r"dd\s+if=.*of=/dev/sd",  # raw disk write
    r":\(\)\{.*\|.*&\s*\};:",  # fork bomb
]


def find_forge_config() -> Path | None:
    """Find forge-config.json by searching up from cwd."""
    cwd = Path.cwd()

    # Check common locations
    paths_to_check = [
        cwd / ".claude" / "forge-config.json",
        cwd / "forge-config.json",
    ]

    # Search up the directory tree
    current = cwd
    while current != current.parent:
        paths_to_check.append(current / ".claude" / "forge-config.json")
        current = current.parent

    for path in paths_to_check:
        if path.exists():
            return path

    return None


def load_config() -> dict:
    """Load forge-config.json, returning empty dict if not found."""
    config_path = find_forge_config()
    if not config_path:
        return {}

    try:
        with open(config_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get_current_profile() -> ProfileName:
    """Get the currently active security profile."""
    config = load_config()
    security = config.get("security", {})
    profile = security.get("profile", DEFAULT_PROFILE)

    # Validate profile name
    if profile not in ("strict", "standard", "minimal"):
        return DEFAULT_PROFILE

    return profile  # type: ignore


def get_hook_behavior(hook_name: str) -> HookBehavior:
    """
    Get the behavior for a specific hook based on current profile.

    Args:
        hook_name: Name of the hook (e.g., "secrets_scanner", "block_dangerous")

    Returns:
        HookBehavior: "block", "warn", "off", "on", or "block_reduced"
    """
    profile = get_current_profile()
    config = load_config()

    # Try to get from config first
    security = config.get("security", {})
    hooks = security.get("hooks", {})
    hook_config = hooks.get(hook_name, {})

    if hook_config and profile in hook_config:
        return hook_config[profile]

    # Fall back to defaults
    if hook_name in DEFAULT_HOOK_BEHAVIORS:
        return DEFAULT_HOOK_BEHAVIORS[hook_name].get(profile, "off")

    # Unknown hook, default to "on" for strict/standard, "off" for minimal
    return "off" if profile == "minimal" else "on"


def is_enabled(hook_name: str) -> bool:
    """
    Check if a hook is enabled (not "off").

    Args:
        hook_name: Name of the hook

    Returns:
        True if the hook should run, False if disabled
    """
    behavior = get_hook_behavior(hook_name)
    return behavior != "off"


def should_block(hook_name: str) -> bool:
    """
    Check if a hook should block (exit code 2) on issues.

    Args:
        hook_name: Name of the hook

    Returns:
        True if the hook should use exit code 2, False for warning (exit code 1)
    """
    behavior = get_hook_behavior(hook_name)
    return behavior in ("block", "block_reduced")


def should_warn(hook_name: str) -> bool:
    """
    Check if a hook should warn (exit code 1) on issues.

    Args:
        hook_name: Name of the hook

    Returns:
        True if the hook should use exit code 1 (warning)
    """
    behavior = get_hook_behavior(hook_name)
    return behavior == "warn"


def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
    """
    Get the appropriate exit code for a hook based on profile.

    Args:
        hook_name: Name of the hook
        issue_found: Whether an issue was found (default True)

    Returns:
        Exit code: 0 (success), 1 (warning), or 2 (block)
    """
    if not issue_found:
        return 0

    behavior = get_hook_behavior(hook_name)

    if behavior in ("block", "block_reduced"):
        return 2
    elif behavior == "warn":
        return 1
    else:
        return 0


def get_reduced_patterns(hook_name: str) -> list[str] | None:
    """
    Get reduced pattern list for minimal profile hooks.

    Args:
        hook_name: Name of the hook

    Returns:
        List of reduced patterns, or None if not applicable
    """
    behavior = get_hook_behavior(hook_name)
    if behavior != "block_reduced":
        return None

    if hook_name == "block_dangerous":
        return MINIMAL_DANGEROUS_PATTERNS

    return None


def get_profile_info() -> dict:
    """
    Get detailed information about the current security profile.

    Returns:
        Dict with profile name, description, and hook statuses
    """
    profile = get_current_profile()
    config = load_config()
    security = config.get("security", {})
    profiles = security.get("profiles", {})

    profile_info = profiles.get(profile, {})

    hook_statuses = {}
    for hook_name in DEFAULT_HOOK_BEHAVIORS:
        hook_statuses[hook_name] = get_hook_behavior(hook_name)

    return {
        "profile": profile,
        "description": profile_info.get("description", ""),
        "philosophy": profile_info.get("philosophy", ""),
        "hooks": hook_statuses,
    }


def print_profile_status(file=sys.stderr) -> None:
    """Print formatted profile status to stderr."""
    info = get_profile_info()

    border = "═" * 67
    print(f"\n╔{border}╗", file=file)
    print(f"║ {'FORGE SECURITY PROFILE':<40} [{info['profile']:>10}] ║", file=file)
    print(f"╠{border}╣", file=file)
    print(f"║ {info['description']:<65} ║", file=file)
    print(f"╠{border}╣", file=file)
    print(f"║ {'Hook Status:':<65} ║", file=file)

    # Format hook statuses
    hooks = info["hooks"]
    hook_names = list(hooks.keys())

    for i, hook_name in enumerate(hook_names):
        status = hooks[hook_name].upper()
        is_last = i == len(hook_names) - 1
        prefix = "└─" if is_last else "├─"
        display_name = f"{hook_name}.py"
        line = f" {prefix} {display_name:<25} {status:<8}"
        print(f"║{line:<65} ║", file=file)

    print(f"╚{border}╝\n", file=file)


# For direct execution (testing)
if __name__ == "__main__":
    print_profile_status(file=sys.stdout)
