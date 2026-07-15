#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Company Root Resolution Utility — shared module for finding multi-project company roots.

Implements upward directory resolution to find `.forge-company-root` marker file.
This is the core utility for v1.2 Multi-Project Company feature. All other hooks
that need to locate the company directory should import this module.

The `.forge-company-root` marker file contains JSON:
{
    "version": "1.0",
    "company_name": "optional",
    "created_at": "ISO 8601",
    "config": {
        "work_queue_mode": "company-level",
        "strict_mode": false
    }
}

Functions:
    find_company_root(start_path) -> Path | None
        Finds the company root by traversing upward looking for .forge-company-root

    get_company_dir(start_path) -> Path
        Gets the .company directory (multi-project or legacy fallback)

    is_multi_project_mode(start_path) -> bool
        Returns True if in multi-project mode (has .forge-company-root)

    get_project_id(project_path) -> str
        Gets a unique project identifier from its path

    get_current_project() -> dict | None
        Gets information about the current project context

Usage:
    from company_resolver import find_company_root, get_company_dir

    # Find the company root
    root = find_company_root(Path.cwd())
    if root:
        print(f"Company root: {root}")
    else:
        print("No company root found, using legacy mode")

    # Get the company directory (always returns a valid path)
    company_dir = get_company_dir(Path.cwd())
"""

import hashlib
import json
import sys
import warnings
from pathlib import Path

# The marker file that indicates a multi-project company root
COMPANY_ROOT_MARKER = ".forge-company-root"

# Legacy company directory name (v1.1 compatibility)
LEGACY_COMPANY_DIR = ".company"

# Default marker file content
DEFAULT_MARKER_CONTENT = {
    "version": "1.0",
    "company_name": None,
    "created_at": None,
    "config": {
        "work_queue_mode": "company-level",
        "strict_mode": False,
    },
}


def find_company_root(start_path: Path | str | None = None) -> Path | None:
    """
    Find the company root by traversing upward from start_path.

    Searches for the `.forge-company-root` marker file starting from the
    given path and traversing upward through parent directories until
    either the marker is found or the filesystem root is reached.

    Args:
        start_path: The path to start searching from. Defaults to current
                   working directory if None.

    Returns:
        Path to the directory containing `.forge-company-root`, or None
        if no marker is found.

    Examples:
        >>> find_company_root(Path("/projects/myproject/src"))
        PosixPath('/projects')  # if /projects/.forge-company-root exists

        >>> find_company_root(Path("/tmp/random"))
        None  # if no marker found
    """
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path)

    # Resolve to absolute path for consistent traversal
    try:
        current = start_path.resolve()
    except (OSError, PermissionError):
        # If we can't resolve the path, try using it as-is
        current = Path(start_path).absolute()

    # Track visited paths to avoid infinite loops (e.g., symlink cycles)
    visited: set[Path] = set()

    while current not in visited:
        visited.add(current)

        marker_path = current / COMPANY_ROOT_MARKER
        try:
            if marker_path.exists() and marker_path.is_file():
                return current
        except (OSError, PermissionError):
            # Permission denied - skip this directory and continue upward
            pass

        # Move to parent directory
        parent = current.parent

        # Check if we've reached the filesystem root
        if parent == current:
            break

        current = parent

    return None


def _get_main_project_from_worktree(start_path: Path) -> Path | None:
    """
    Detect if we're in a git worktree and return the main project path.

    Git worktrees have a .git file (not directory) pointing to the main repo.
    We use `git rev-parse --git-common-dir` to find the main .git directory,
    then derive the main project path from it.

    Returns:
        Path to main project if in a worktree, None otherwise.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=start_path,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        common_dir = Path(result.stdout.strip())

        # If common_dir is just ".git", we're in the main repo, not a worktree
        if common_dir.name == ".git" and not common_dir.is_absolute():
            return None

        # In a worktree, common_dir is absolute path to main repo's .git
        # e.g., /Users/user/project/.git -> main project is /Users/user/project
        if common_dir.is_absolute() and common_dir.name == ".git":
            return common_dir.parent

        # Handle /path/to/project/.git/worktrees/xxx case
        if "worktrees" in common_dir.parts:
            # Find the .git directory above worktrees
            parts = common_dir.parts
            for i, part in enumerate(parts):
                if (
                    part == ".git"
                    and i + 1 < len(parts)
                    and parts[i + 1] == "worktrees"
                ):
                    return Path(*parts[:i])

        return None
    except Exception:
        return None


def get_company_dir(start_path: Path | str | None = None) -> Path:
    """
    Get the .company directory path.

    Priority order:
    1. If in a git worktree, use MAIN project's .company (not worktree's)
    2. In multi-project mode (when .forge-company-root is found), returns
       the .company directory at the company root level.
    3. In legacy mode (no marker found), returns .company in the current
       directory for backward compatibility with v1.1.

    Args:
        start_path: The path to start searching from. Defaults to current
                   working directory if None.

    Returns:
        Path to the .company directory. Note: This directory may not
        exist yet; callers should create it if needed.

    Examples:
        >>> get_company_dir(Path("/projects/myproject"))
        PosixPath('/projects/.company')  # multi-project mode

        >>> get_company_dir(Path("/tmp/random"))
        PosixPath('/tmp/random/.company')  # legacy fallback

        >>> get_company_dir(Path("/tmp/forge-worktrees/wt-xxx"))
        PosixPath('/projects/.company')  # worktree -> main project
    """
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path)

    # WS-111: If running in a git worktree, use MAIN project's .company
    # This ensures reference docs are created in the shared location
    main_project = _get_main_project_from_worktree(start_path)
    if main_project is not None:
        return main_project / LEGACY_COMPANY_DIR

    company_root = find_company_root(start_path)

    if company_root is not None:
        # Multi-project mode: use company root level
        return company_root / LEGACY_COMPANY_DIR
    else:
        # Legacy mode: use current directory
        try:
            return start_path.resolve() / LEGACY_COMPANY_DIR
        except (OSError, PermissionError):
            return start_path.absolute() / LEGACY_COMPANY_DIR


def is_multi_project_mode(start_path: Path | str | None = None) -> bool:
    """
    Check if operating in multi-project mode.

    Multi-project mode is active when a .forge-company-root marker
    is found by traversing upward from the start path.

    Args:
        start_path: The path to start searching from. Defaults to current
                   working directory if None.

    Returns:
        True if in multi-project mode (marker found), False otherwise.

    Examples:
        >>> is_multi_project_mode(Path("/projects/myproject"))
        True  # if /projects/.forge-company-root exists

        >>> is_multi_project_mode(Path("/tmp/random"))
        False  # no marker found
    """
    return find_company_root(start_path) is not None


def get_project_id(project_path: Path | str | None = None) -> str:
    """
    Get a unique project identifier from its path.

    Generates a deterministic ID based on the project path. The ID
    is derived from the directory name combined with a short hash
    of the full resolved path to ensure uniqueness.

    Args:
        project_path: The project path. Defaults to current working
                     directory if None.

    Returns:
        A string identifier in the format "dirname-hash6" where:
        - dirname is the sanitized directory name (lowercase, alphanumeric)
        - hash6 is the first 6 characters of the path's SHA256 hash

    Examples:
        >>> get_project_id(Path("/projects/MyProject"))
        "myproject-a1b2c3"

        >>> get_project_id(Path("/other/MyProject"))
        "myproject-d4e5f6"  # different hash due to different path
    """
    if project_path is None:
        project_path = Path.cwd()
    else:
        project_path = Path(project_path)

    try:
        resolved = project_path.resolve()
    except (OSError, PermissionError):
        resolved = project_path.absolute()

    # Get the directory name and sanitize it
    dir_name = resolved.name
    # Keep only alphanumeric and hyphens, convert to lowercase
    sanitized_name = "".join(
        c if c.isalnum() or c == "-" else "" for c in dir_name
    ).lower()

    # Ensure we have at least something
    if not sanitized_name:
        sanitized_name = "project"

    # Generate a short hash of the full path for uniqueness
    path_hash = hashlib.sha256(str(resolved).encode()).hexdigest()[:6]

    return f"{sanitized_name}-{path_hash}"


# Machine-global parent for all Forge daemon worktrees. Never create
# worktrees directly here — always inside get_worktree_base().
WORKTREE_ROOT = Path("/tmp/forge-worktrees")


def get_worktree_base(project_root: Path | str | None = None) -> Path:
    """Per-project daemon worktree base: /tmp/forge-worktrees/<project-id>.

    The base was machine-global for years, which let one project's daemon
    GC or harvest ANOTHER project's worktrees (same gh login → pushes and
    PRs on the wrong repo). Namespacing by project id — the same id the
    LaunchAgent label uses — confines every daemon to its own subtree.

    Defaults to THIS installation's root, anchored on __file__ rather than
    cwd: workers run with cwd inside /tmp worktrees and must still resolve
    their home project's base.
    """
    if project_root is None:
        # .claude/hooks/company/company_resolver.py -> project root
        project_root = Path(__file__).resolve().parents[3]
    return WORKTREE_ROOT / get_project_id(project_root)


def load_company_root_config(company_root: Path) -> dict:
    """
    Load and parse the .forge-company-root marker file.

    Args:
        company_root: Path to the directory containing the marker file.

    Returns:
        Parsed JSON content from the marker file, or default content
        if the file is invalid or cannot be read.

    Note:
        Invalid JSON will emit a warning but not raise an exception.
    """
    marker_path = company_root / COMPANY_ROOT_MARKER

    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

            # Handle empty file
            if not content:
                return DEFAULT_MARKER_CONTENT.copy()

            return json.loads(content)
    except json.JSONDecodeError as e:
        warnings.warn(
            f"Invalid JSON in {marker_path}: {e}. Using defaults.",
            stacklevel=2,
        )
        return DEFAULT_MARKER_CONTENT.copy()
    except (OSError, PermissionError) as e:
        warnings.warn(
            f"Cannot read {marker_path}: {e}. Using defaults.",
            stacklevel=2,
        )
        return DEFAULT_MARKER_CONTENT.copy()


def get_current_project() -> dict | None:
    """
    Get information about the current project context.

    Returns a dictionary with project metadata if in multi-project mode,
    or None if in legacy single-project mode.

    Returns:
        Dictionary with project information:
        - project_id: Unique identifier for this project
        - project_path: Resolved path to the project directory
        - company_root: Path to the company root directory
        - company_dir: Path to the .company directory
        - company_config: Parsed content of .forge-company-root
        - multi_project_mode: Always True when returned

        Returns None if not in multi-project mode.

    Examples:
        >>> get_current_project()
        {
            'project_id': 'myproject-a1b2c3',
            'project_path': PosixPath('/projects/myproject'),
            'company_root': PosixPath('/projects'),
            'company_dir': PosixPath('/projects/.company'),
            'company_config': {...},
            'multi_project_mode': True
        }

        >>> get_current_project()  # in legacy mode
        None
    """
    cwd = Path.cwd()
    company_root = find_company_root(cwd)

    if company_root is None:
        return None

    try:
        project_path = cwd.resolve()
    except (OSError, PermissionError):
        project_path = cwd.absolute()

    return {
        "project_id": get_project_id(project_path),
        "project_path": project_path,
        "company_root": company_root,
        "company_dir": get_company_dir(cwd),
        "company_config": load_company_root_config(company_root),
        "multi_project_mode": True,
    }


def main():
    """
    CLI interface for testing and debugging company root resolution.

    Usage:
        python company_resolver.py [command] [path]

    Commands:
        find      Find company root from path (default command)
        dir       Get company directory path
        mode      Check if in multi-project mode
        project   Get current project information
        id        Get project ID for path

    Examples:
        python company_resolver.py find /projects/myproject
        python company_resolver.py dir
        python company_resolver.py mode
        python company_resolver.py project
        python company_resolver.py id /projects/myproject
    """
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("\nCLI Commands:")
        print("  find [path]     Find company root from path")
        print("  dir [path]      Get company directory path")
        print("  mode [path]     Check if in multi-project mode")
        print("  project         Get current project information")
        print("  id [path]       Get project ID for path")
        sys.exit(0)

    command = args[0] if args else "find"
    path = Path(args[1]) if len(args) > 1 else None

    if command == "find":
        result = find_company_root(path)
        if result:
            print(f"Company root: {result}")
        else:
            print("No company root found")
        sys.exit(0 if result else 1)

    elif command == "dir":
        result = get_company_dir(path)
        print(f"Company dir: {result}")
        sys.exit(0)

    elif command == "mode":
        result = is_multi_project_mode(path)
        print(f"Multi-project mode: {result}")
        sys.exit(0)  # Not an error, just a status check

    elif command == "project":
        result = get_current_project()
        if result:
            print(
                json.dumps(
                    {
                        k: str(v) if isinstance(v, Path) else v
                        for k, v in result.items()
                    },
                    indent=2,
                )
            )
        else:
            print("Not in multi-project mode")
        sys.exit(0)  # Not an error, just a status check

    elif command == "id":
        result = get_project_id(path)
        print(f"Project ID: {result}")
        sys.exit(0)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
