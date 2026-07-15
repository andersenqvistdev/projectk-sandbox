# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Pre-validate files before write/edit.
Checks that we're not writing to protected paths or binary files.

Security Profile Aware:
- strict: Blocks on protected path access
- standard: Warns on protected path access
- minimal: Disabled
"""

import json
import os
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


HOOK_NAME = "lint_on_edit"


def _is_daemon_context() -> bool:
    """Check if we're running in daemon context (should respect humanProtected)."""
    # Daemon sets specific env vars or runs from specific paths
    return (
        os.environ.get("FORGE_DAEMON") == "1"
        or "forge_daemon" in os.environ.get("_", "")
        or os.environ.get("FORGE_EMPLOYEE_ID") is not None
    )


def _get_human_protected_paths() -> list[str]:
    """Load human-protected paths from forge-config.json."""
    try:
        config_path = os.path.join(os.getcwd(), "forge-config.json")
        if not os.path.exists(config_path):
            return []
        with open(config_path) as f:
            config = json.load(f)
        hp = config.get("humanProtected", {})
        if not hp.get("enabled", False):
            return []
        return hp.get("paths", [])
    except Exception:
        return []


def _matches_pattern(file_path: str, pattern: str) -> bool:
    """Check if file_path matches a glob-like pattern."""
    import fnmatch

    # Normalize paths
    file_path = file_path.lstrip("./")
    pattern = pattern.lstrip("./")
    return fnmatch.fnmatch(file_path, pattern)


PROTECTED_PATHS = [
    ".git/",
    "node_modules/",
    ".env",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
]

# Files that get content-validated instead of blanket-blocked.
# Writes are allowed if the content passes validation; blocked otherwise.
VALIDATED_PATHS = {
    ".company/org.json": "_validate_org_json",
}

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pdf",
    ".doc",
    ".docx",
    ".pyc",
    ".pyo",
    ".class",
}


def _validate_org_json(file_path: str, tool_name: str, tool_input: dict) -> str | None:
    """Validate writes to org.json won't wipe employees.

    Returns None if write is safe, or an error message if it should be blocked.
    Allows: hiring (adds employees), edits that keep employees, new files.
    Blocks: writes that would reduce employee count to 0 when employees exist.
    """
    # For Edit tool, check if the edit touches the employees array destructively
    if tool_name == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        # Block edits that replace the employees array with empty
        if '"employees"' in old and '"employees": []' in new:
            return "Edit would empty the employees array"
        # Allow other edits (e.g. changing a single field)
        return None

    # For Write tool, parse the full content and compare employee counts
    content = tool_input.get("content", "")
    try:
        new_data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return "Content is not valid JSON"

    new_employees = new_data.get("employees", [])

    # Read existing file to compare
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_employees = existing.get("employees", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # No existing file or can't read it — allow if new data has employees
        return None

    # Block: existing has employees but new content wipes them
    if len(existing_employees) > 0 and len(new_employees) == 0:
        return (
            f"Would wipe {len(existing_employees)} employees. "
            f"Use Python save_org() functions for safe programmatic modifications."
        )

    return None


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

    # Human-protected paths: Block daemon from modifying files reserved for human editing
    if _is_daemon_context():
        human_protected = _get_human_protected_paths()
        for pattern in human_protected:
            if _matches_pattern(file_path, pattern):
                print("", file=sys.stderr)
                print(
                    "╔══════════════════════════════════════════════════════════════╗",
                    file=sys.stderr,
                )
                print(
                    "║  BLOCKED: Human-Protected File                               ║",
                    file=sys.stderr,
                )
                print(
                    "╠══════════════════════════════════════════════════════════════╣",
                    file=sys.stderr,
                )
                print(f"║  Path: {file_path[:55]:<55} ║", file=sys.stderr)
                print(
                    "║                                                              ║",
                    file=sys.stderr,
                )
                print(
                    "║  This file is reserved for human editing only.              ║",
                    file=sys.stderr,
                )
                print(
                    "║  Configure in forge-config.json → humanProtected.paths      ║",
                    file=sys.stderr,
                )
                print(
                    "╚══════════════════════════════════════════════════════════════╝",
                    file=sys.stderr,
                )
                print("", file=sys.stderr)
                print(
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": f"BLOCKED: Human-protected path: {pattern}",
                        }
                    )
                )
                sys.exit(2)

    # Protected paths and binary files always block (exit 2) regardless of profile
    # because these are fundamental invariants:
    # - Writing to .git/ would corrupt the repository
    # - Writing to node_modules/ could break dependencies
    # - Binary files cannot be edited as text

    # Content-validated paths: allow writes that pass validation, block otherwise.
    # This protects critical data files without preventing legitimate commands
    # like /company-hire from working.
    for pattern, _validator_name in VALIDATED_PATHS.items():
        if pattern in file_path:
            error = _validate_org_json(file_path, tool_name, tool_input)
            if error:
                print("", file=sys.stderr)
                print(
                    "╔══════════════════════════════════════════════════════════════╗",
                    file=sys.stderr,
                )
                print(
                    "║  BLOCKED: Employee Data Protection                           ║",
                    file=sys.stderr,
                )
                print(
                    "╠══════════════════════════════════════════════════════════════╣",
                    file=sys.stderr,
                )
                print(f"║  {error[:60]:<60} ║", file=sys.stderr)
                print(
                    "║                                                              ║",
                    file=sys.stderr,
                )
                print(
                    "║  org.json employees are protected from accidental wipes.    ║",
                    file=sys.stderr,
                )
                print(
                    "║  Hiring, promotions, and edits that keep employees work.    ║",
                    file=sys.stderr,
                )
                print(
                    "╚══════════════════════════════════════════════════════════════╝",
                    file=sys.stderr,
                )
                print("", file=sys.stderr)
                print(
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": f"BLOCKED: {error}",
                        }
                    )
                )
                sys.exit(2)
            # Validation passed — allow the write
            break

    # Check protected paths
    for protected in PROTECTED_PATHS:
        if protected in file_path:
            # Human-readable output to stderr
            print("", file=sys.stderr)
            print(
                "╔══════════════════════════════════════════════════════════════╗",
                file=sys.stderr,
            )
            print(
                "║  BLOCKED: Protected Path                                     ║",
                file=sys.stderr,
            )
            print(
                "╠══════════════════════════════════════════════════════════════╣",
                file=sys.stderr,
            )
            print(f"║  Path: {file_path[:54]:<54} ║", file=sys.stderr)
            print(f"║  Protected pattern: {protected:<42} ║", file=sys.stderr)
            print(
                "║                                                              ║",
                file=sys.stderr,
            )
            print(
                "║  This path is protected to prevent accidental modifications ║",
                file=sys.stderr,
            )
            print(
                "║  to critical files (git, dependencies, lock files, etc.)    ║",
                file=sys.stderr,
            )
            print(
                "╚══════════════════════════════════════════════════════════════╝",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            # JSON output to stdout
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": f"BLOCKED: Cannot write to protected path: {protected}",
                    }
                )
            )
            sys.exit(2)

    # Check binary files
    _, ext = os.path.splitext(file_path)
    if ext.lower() in BINARY_EXTENSIONS:
        # Human-readable output to stderr
        print("", file=sys.stderr)
        print(
            "╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  BLOCKED: Binary File                                        ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        print(f"║  Path: {file_path[:54]:<54} ║", file=sys.stderr)
        print(f"║  Extension: {ext:<50} ║", file=sys.stderr)
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Binary files cannot be edited as text. Use appropriate     ║",
            file=sys.stderr,
        )
        print(
            "║  tools for binary file manipulation.                         ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        # JSON output to stdout
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"BLOCKED: Cannot write to binary file: {ext}",
                }
            )
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
