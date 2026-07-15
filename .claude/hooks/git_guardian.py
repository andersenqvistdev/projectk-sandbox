# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Git Guardian — validates git operations for security.
Blocks commits with secrets, prevents pushes to protected branches,
validates commit messages, and catches large binary files.

Security Profile Aware:
- strict/standard: Blocks on security issues
- minimal: Warns only (relies on /gate checkpoint)
"""

import json
import re
import shlex
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


HOOK_NAME = "git_guardian"

PROTECTED_BRANCHES = ["main", "master", "production", "release"]

# Max file size to allow in commits (5MB)
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

SENSITIVE_PATHS = [
    r"\.env($|\.)",
    r"\.env\.local",
    r"\.env\.production",
    r"secrets/",
    r"credentials",
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"id_rsa",
    r"id_ed25519",
]

# Patterns for executable syntax in commit messages (dangerous injection vectors)
# These are ACTUAL executable constructs, not descriptive text.
# NOTE: bare backticks are intentionally NOT blocked here. In a commit message a
# commit message is inert text that nothing executes, and backticks are standard
# markdown inline-code referencing `flags`, `files`, or `functions`. Blocking them
# is a false positive that forces awkward prose. The genuinely injection-shaped
# constructs below ($(), eval, here-doc piped to a shell) remain blocked.
EXECUTABLE_MESSAGE_PATTERNS = [
    (r"\$\([^)]+\)", "subshell command substitution"),  # $(command)
    (r"eval\s+[\"'\$]", "eval construct"),  # eval "...", eval $var
    (r"<<\s*\w+.*\|\s*(ba)?sh", "here-doc piped to shell"),  # << EOF | bash
]

# Patterns for secret content in staged files
SECRET_CONTENT_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"-----BEGIN\s+(?:RSA\s+|EC\s+)?PRIVATE\s+KEY-----", "Private Key"),
    (r"sk_live_[A-Za-z0-9]{24,}", "Stripe Secret Key"),
    (r"gh[pousr]_[A-Za-z0-9_]{36,}", "GitHub Token"),
    (r"sk-ant-[A-Za-z0-9_-]{40,}", "Anthropic API Key"),
    (r"sk-[A-Za-z0-9]{48,}", "OpenAI API Key"),
]


def extract_commit_message(command: str) -> str | None:
    """Extract the commit message from a git commit command.

    Returns the message text if -m or --message flag is present, None otherwise.
    Handles quoted messages correctly using shlex.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    # Find -m or --message flag
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-m" or token == "--message":
            # Next token is the message
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return None
        elif token.startswith("-m"):
            # -m"message" or -mmessage format
            return token[2:]
        elif token.startswith("--message="):
            # --message="message" format
            return token[10:]
        i += 1

    return None


def check_message_for_executable_syntax(message: str) -> tuple[str, str] | None:
    """Check if a commit message contains executable syntax.

    Returns (pattern_name, matched_pattern) if found, None if safe.
    Only blocks actual executable constructs, NOT descriptive text.
    """
    for pattern, name in EXECUTABLE_MESSAGE_PATTERNS:
        if re.search(pattern, message):
            return (name, pattern)
    return None


def get_staged_files() -> list[str]:
    """Get list of files staged for commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_current_branch() -> str:
    """Get current git branch."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get_staged_content(file_path: str) -> str:
    """Get the staged content of a file."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def check_commit(command: str) -> dict | None:
    """Validate a git commit operation."""
    issues = []

    # Check staged files for sensitive paths
    staged_files = get_staged_files()
    for file_path in staged_files:
        for pattern in SENSITIVE_PATHS:
            if re.search(pattern, file_path, re.IGNORECASE):
                issues.append(f"Sensitive file staged: {file_path}")

    # Scan staged content for secrets
    for file_path in staged_files:
        content = get_staged_content(file_path)
        # Only scan added lines (lines starting with +)
        added_lines = [
            ln[1:]
            for ln in content.split("\n")
            if ln.startswith("+") and not ln.startswith("+++")
        ]

        for line in added_lines:
            for pattern, name in SECRET_CONTENT_PATTERNS:
                if re.search(pattern, line):
                    issues.append(f"Secret in staged content ({name}): {file_path}")
                    break  # One finding per file is enough

    # Check for large binary files
    for file_path in staged_files:
        try:
            import os

            if os.path.exists(file_path):
                size = os.path.getsize(file_path)
                if size > MAX_FILE_SIZE_BYTES:
                    size_mb = size / (1024 * 1024)
                    issues.append(
                        f"Large file ({size_mb:.1f}MB): {file_path} — consider Git LFS"
                    )
        except OSError:
            pass

    # Check commit message for executable syntax
    message = extract_commit_message(command)
    if message:
        exec_check = check_message_for_executable_syntax(message)
        if exec_check:
            name, pattern = exec_check
            # Human-readable output to stderr
            print("", file=sys.stderr)
            print(
                "╔══════════════════════════════════════════════════════════════╗",
                file=sys.stderr,
            )
            print(
                "║  BLOCKED: Executable Syntax in Commit Message               ║",
                file=sys.stderr,
            )
            print(
                "╠══════════════════════════════════════════════════════════════╣",
                file=sys.stderr,
            )
            truncated_msg = message[:50] + "..." if len(message) > 50 else message
            print(f"║  Message: {truncated_msg:<52} ║", file=sys.stderr)
            print(f"║  Found: {name:<54} ║", file=sys.stderr)
            print(
                "║                                                              ║",
                file=sys.stderr,
            )
            print(
                "║  Commit messages should be descriptive text only.           ║",
                file=sys.stderr,
            )
            print(
                "║  Remove executable syntax: backticks, $(), eval             ║",
                file=sys.stderr,
            )
            print(
                "╚══════════════════════════════════════════════════════════════╝",
                file=sys.stderr,
            )
            print("", file=sys.stderr)

            return {
                "decision": "block",
                "reason": f"GIT GUARDIAN — executable syntax in commit message:\n"
                f"  Found: {name}\n"
                f"  Message: {truncated_msg}\n\n"
                f"Commit messages should be descriptive text only.\n"
                f"Tip: Describe what you're testing, not the actual pattern.\n"
                f'  Bad:  "Fix `whoami` injection"\n'
                f'  Good: "Fix command substitution injection"',
            }

    if issues:
        # Human-readable output to stderr
        print("", file=sys.stderr)
        print(
            "╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  BLOCKED: Secret Detected in Commit                         ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        for issue in issues:
            # Truncate long issues to fit in box
            display_issue = issue[:60] if len(issue) > 60 else issue
            print(f"║  - {display_issue:<58} ║", file=sys.stderr)
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Suggestions:                                                ║",
            file=sys.stderr,
        )
        print(
            "║  1. Use environment variables for secrets                    ║",
            file=sys.stderr,
        )
        print(
            "║  2. Add sensitive files to .gitignore                        ║",
            file=sys.stderr,
        )
        print(
            "║  3. Use a secrets manager (Vault, AWS Secrets Manager)       ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print("", file=sys.stderr)

        # JSON output to stdout (unchanged)
        report = "GIT GUARDIAN — commit blocked:\n\n"
        for issue in issues:
            report += f"  - {issue}\n"
        report += "\nFix these issues before committing."
        return {"decision": "block", "reason": report}

    return None


def extract_git_push_args(command: str) -> list[str] | None:
    """
    Extract arguments from a git push command.
    Returns None if this is not a direct git push command.
    Only matches actual 'git push' commands, not text inside messages.
    """
    try:
        # Use shlex to properly parse the command, respecting quotes
        tokens = shlex.split(command)
    except ValueError:
        # Malformed command, let it through
        return None

    # Find 'git' followed by 'push' at the start of a command
    # Handle cases like: git push, git -c ... push, etc.
    git_idx = None
    for i, token in enumerate(tokens):
        if token == "git":
            git_idx = i
            break

    if git_idx is None:
        return None

    # Look for 'push' as the git subcommand (skip any git global flags)
    push_idx = None
    for i in range(git_idx + 1, len(tokens)):
        token = tokens[i]
        # Skip git global flags like -c, --work-tree, etc.
        if token.startswith("-"):
            # Skip flags that take an argument
            if token in ["-c", "-C", "--git-dir", "--work-tree"]:
                continue
            continue
        # This should be the subcommand
        if token == "push":
            push_idx = i
        break

    if push_idx is None:
        return None

    # Return everything after 'push'
    return tokens[push_idx + 1 :]


def check_push(command: str) -> dict | None:
    """Validate a git push operation."""
    push_args = extract_git_push_args(command)

    if push_args is None:
        # Not a git push command
        return None

    branch = get_current_branch()

    # Parse the push arguments to find the target ref
    # git push [options] [<repository> [<refspec>...]]
    target_ref = None
    remote = None

    # Skip options and find remote/refspec
    i = 0
    while i < len(push_args):
        arg = push_args[i]
        if arg.startswith("-"):
            # Skip flags
            # Some flags take arguments (NOT -u/--set-upstream, they're standalone)
            if arg in ["-o", "--push-option", "--repo"]:
                i += 1  # Skip the next arg too
            i += 1
            continue
        # First non-option is the remote
        if remote is None:
            remote = arg
        else:
            # Next non-option is the refspec
            target_ref = arg
            break
        i += 1

    # If pushing to a protected branch explicitly
    if target_ref:
        for protected in PROTECTED_BRANCHES:
            # Check if the refspec targets a protected branch
            # Handle formats: main, origin/main, refs/heads/main, :main
            if (
                target_ref == protected
                or target_ref.endswith(f"/{protected}")
                or target_ref == f":{protected}"
            ):
                # Human-readable output to stderr
                print("", file=sys.stderr)
                print(
                    "╔══════════════════════════════════════════════════════════════╗",
                    file=sys.stderr,
                )
                print(
                    "║  BLOCKED: Push to Protected Branch                          ║",
                    file=sys.stderr,
                )
                print(
                    "╠══════════════════════════════════════════════════════════════╣",
                    file=sys.stderr,
                )
                print(f"║  Branch: {protected:<53} ║", file=sys.stderr)
                print(
                    "║                                                              ║",
                    file=sys.stderr,
                )
                print(
                    "║  Direct pushes to protected branches are not allowed.       ║",
                    file=sys.stderr,
                )
                print(
                    "║  Please use a feature branch and create a pull request.     ║",
                    file=sys.stderr,
                )
                print(
                    "║                                                              ║",
                    file=sys.stderr,
                )
                print(
                    "║  Try this instead:                                           ║",
                    file=sys.stderr,
                )
                print(
                    "║    git checkout -b feature/your-feature                      ║",
                    file=sys.stderr,
                )
                print(
                    "║    git push -u origin feature/your-feature                   ║",
                    file=sys.stderr,
                )
                print(
                    "║    gh pr create                                              ║",
                    file=sys.stderr,
                )
                print(
                    "╚══════════════════════════════════════════════════════════════╝",
                    file=sys.stderr,
                )
                print("", file=sys.stderr)

                # JSON output to stdout (unchanged)
                return {
                    "decision": "block",
                    "reason": f"GIT GUARDIAN — push to protected branch '{protected}' blocked.\n"
                    f"Create a feature branch and open a pull request instead.\n"
                    f"  git checkout -b feature/your-feature\n"
                    f"  git push origin feature/your-feature",
                }

    # If no explicit refspec, check if current branch is protected
    if target_ref is None and branch in PROTECTED_BRANCHES:
        # Human-readable output to stderr
        print("", file=sys.stderr)
        print(
            "╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  BLOCKED: Push to Protected Branch                          ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        print(f"║  Current branch: {branch:<45} ║", file=sys.stderr)
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Direct pushes to protected branches are not allowed.       ║",
            file=sys.stderr,
        )
        print(
            "║  Please use a feature branch and create a pull request.     ║",
            file=sys.stderr,
        )
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Try this instead:                                           ║",
            file=sys.stderr,
        )
        print(
            "║    git checkout -b feature/your-feature                      ║",
            file=sys.stderr,
        )
        print(
            "║    git push -u origin feature/your-feature                   ║",
            file=sys.stderr,
        )
        print(
            "║    gh pr create                                              ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print("", file=sys.stderr)

        # JSON output to stdout (unchanged)
        return {
            "decision": "block",
            "reason": f"GIT GUARDIAN — push from protected branch '{branch}' blocked.\n"
            f"Create a feature branch and open a pull request instead.\n"
            f"  git checkout -b feature/your-feature\n"
            f"  git push origin feature/your-feature",
        }

    return None


def is_git_subcommand(command: str, subcommand: str) -> bool:
    """
    Check if the command is a git <subcommand> invocation.
    Properly parses the command to avoid matching text inside quotes.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    # Find 'git' token
    git_idx = None
    for i, token in enumerate(tokens):
        if token == "git":
            git_idx = i
            break

    if git_idx is None:
        return False

    # Look for the subcommand after git (skip global flags)
    for i in range(git_idx + 1, len(tokens)):
        token = tokens[i]
        if token.startswith("-"):
            # Skip git global flags
            if token in ["-c", "-C", "--git-dir", "--work-tree"]:
                continue
            continue
        # This is the subcommand
        return token == subcommand

    return False


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

    if tool_name != "Bash":
        sys.exit(0)

    command = tool_input.get("command", "")

    # Skip gh (GitHub CLI) commands entirely - they have their own workflows
    # and we don't want to block PR descriptions that mention git/branches
    try:
        tokens = shlex.split(command)
        if tokens and tokens[0] == "gh":
            sys.exit(0)
    except ValueError:
        pass

    # Get exit code based on security profile
    exit_code = get_exit_code(HOOK_NAME, issue_found=True)

    # Check git commit - uses proper parsing to avoid matching text in messages
    if is_git_subcommand(command, "commit"):
        result = check_commit(command)
        if result:
            # Adjust decision based on profile
            if exit_code == 1:
                result["decision"] = "warn"
            print(json.dumps(result))
            sys.exit(exit_code)

    # Check git push - uses proper parsing via extract_git_push_args
    if is_git_subcommand(command, "push"):
        result = check_push(command)
        if result:
            # Adjust decision based on profile
            if exit_code == 1:
                result["decision"] = "warn"
            print(json.dumps(result))
            sys.exit(exit_code)

    sys.exit(0)


if __name__ == "__main__":
    main()
