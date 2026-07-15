#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Employee Initiative Module — P26 Task 26.4

Enables employees to propose work independently based on:
- TODO/FIXME comments discovered in the codebase
- Code quality improvements
- Follow-up tasks from recently completed work
- Research findings

This module empowers the organization to be self-improving, not just
top-down directed. Employees can identify and propose valuable work
that managers then review and approve.

Usage:
    # Generate proposals for an employee
    python employee_initiative.py propose --employee-id "senior-python-developer"

    # Scan for TODOs matching employee capabilities
    python employee_initiative.py scan-todos --employee-id "senior-python-developer"

    # Generate follow-up proposals from recent work
    python employee_initiative.py follow-ups --employee-id "senior-python-developer"

    # Get initiative stats
    python employee_initiative.py stats
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
work_allocator = None
company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global work_allocator, company_resolver
    if work_allocator is not None:
        return

    try:
        from . import company_resolver as cr
        from . import work_allocator as wa

        work_allocator = wa
        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        work_allocator = wa
        company_resolver = cr


# Configuration
MAX_PROPOSALS_PER_EMPLOYEE_PER_HOUR = 3
TODO_PATTERNS = [
    r"#\s*TODO\s*:?\s*(.+)",
    r"#\s*FIXME\s*:?\s*(.+)",
    r"#\s*HACK\s*:?\s*(.+)",
    r"//\s*TODO\s*:?\s*(.+)",
    r"//\s*FIXME\s*:?\s*(.+)",
    r"/\*\s*TODO\s*:?\s*(.+)\*/",
]

# Map TODO types to proposal types
TODO_TYPE_MAP = {
    "TODO": "improvement",
    "FIXME": "bug",
    "HACK": "refactor",
}


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_project_root() -> Path:
    """Get the project root directory."""
    _ensure_imports()
    # Company directory is at {project_root}/.company
    # So project root is its parent
    company_dir = company_resolver.get_company_dir()
    return company_dir.parent


def get_employee_capabilities(employee_id: str) -> list[str]:
    """
    Get capabilities for an employee.

    Reads from org.json to find employee's capabilities.
    """
    org_path = get_company_dir() / "org.json"

    if not org_path.exists():
        return []

    try:
        with open(org_path, "r") as f:
            org = json.load(f)

        # Search all employees
        for emp in org.get("employees", []):
            if emp.get("id") == employee_id:
                return emp.get("capabilities", [])

        return []
    except (json.JSONDecodeError, OSError):
        return []


def get_recent_proposals(employee_id: str, hours: int = 1) -> int:
    """
    Count proposals submitted by employee in last N hours.

    Used to enforce rate limiting.
    """
    _ensure_imports()
    queue = work_allocator.load_queue()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    count = 0
    for proposal in queue.get("proposed", []):
        if proposal.get("proposed_by") != employee_id:
            continue

        proposed_at_str = proposal.get("proposed_at")
        if proposed_at_str:
            try:
                proposed_at = datetime.fromisoformat(
                    proposed_at_str.replace("Z", "+00:00")
                )
                if proposed_at >= cutoff:
                    count += 1
            except (ValueError, TypeError):
                pass

    return count


def can_submit_proposal(employee_id: str) -> tuple[bool, str]:
    """
    Check if employee can submit a proposal (rate limiting).

    Returns:
        Tuple of (can_submit, reason_if_not)
    """
    recent_count = get_recent_proposals(employee_id, hours=1)

    if recent_count >= MAX_PROPOSALS_PER_EMPLOYEE_PER_HOUR:
        return (
            False,
            f"Rate limited: {recent_count}/{MAX_PROPOSALS_PER_EMPLOYEE_PER_HOUR} proposals in last hour",
        )

    return (True, "")


def scan_todos_in_file(
    file_path: Path,
    employee_capabilities: list[str],
) -> list[dict]:
    """
    Scan a file for TODO/FIXME/HACK comments.

    Args:
        file_path: Path to the file to scan
        employee_capabilities: Capabilities to match against

    Returns:
        List of TODO items found
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, IOError):
        return []

    todos = []
    lines = content.split("\n")

    for i, line in enumerate(lines):
        for pattern in TODO_PATTERNS:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                text = match.group(1).strip()

                # Determine TODO type
                todo_type = "TODO"
                for t in ["FIXME", "HACK", "TODO"]:
                    if t.lower() in line.lower():
                        todo_type = t
                        break

                todos.append(
                    {
                        "file": str(file_path),
                        "line": i + 1,
                        "text": text,
                        "type": todo_type,
                        "proposal_type": TODO_TYPE_MAP.get(todo_type, "improvement"),
                        "context": "\n".join(lines[max(0, i - 2) : i + 3]),
                    }
                )

    return todos


def scan_todos(
    employee_id: str,
    directories: list[str] | None = None,
    extensions: list[str] | None = None,
    max_results: int = 10,
) -> list[dict]:
    """
    Scan codebase for TODOs matching employee capabilities.

    Args:
        employee_id: The employee scanning for TODOs
        directories: Directories to scan (defaults to common code directories)
        extensions: File extensions to scan (defaults to .py, .js, .ts, .tsx)
        max_results: Maximum number of results to return

    Returns:
        List of TODO items with file, line, text, and type
    """
    _ensure_imports()

    project_root = get_project_root()
    capabilities = get_employee_capabilities(employee_id)

    # Default directories to scan (excludes tests - fixture data creates noise)
    if directories is None:
        directories = [
            ".claude/hooks/company",
            "src",
            "lib",
            "app",
        ]

    # Patterns to skip (test files contain fixture TODOs, not real work)
    skip_patterns = ["test_", "_test.", "conftest.", "fixture", "mock"]

    # Default extensions
    if extensions is None:
        extensions = [".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs"]

    todos = []

    for dir_name in directories:
        dir_path = project_root / dir_name
        if not dir_path.exists():
            continue

        for ext in extensions:
            for file_path in dir_path.rglob(f"*{ext}"):
                # Skip very large files
                if file_path.stat().st_size > 100_000:
                    continue

                # Skip test files (contain fixture TODOs, not real work)
                filename = file_path.name.lower()
                if any(pattern in filename for pattern in skip_patterns):
                    continue

                file_todos = scan_todos_in_file(file_path, capabilities)
                todos.extend(file_todos)

                if len(todos) >= max_results:
                    return todos[:max_results]

    return todos[:max_results]


def get_recent_completions(employee_id: str, hours: int = 24) -> list[dict]:
    """
    Get tasks recently completed by an employee.

    Used to generate follow-up proposals.
    """
    _ensure_imports()
    queue = work_allocator.load_queue()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    completions = []
    for task in queue.get("completed", []):
        if task.get("assigned_to") != employee_id:
            continue

        completed_at_str = task.get("completed_at")
        if completed_at_str:
            try:
                completed_at = datetime.fromisoformat(
                    completed_at_str.replace("Z", "+00:00")
                )
                if completed_at >= cutoff:
                    completions.append(task)
            except (ValueError, TypeError):
                pass

    return completions


def generate_post_completion_proposal(
    employee_id: str,
    completed_task: dict,
) -> dict | None:
    """
    Generate a single follow-up proposal from a just-completed task.

    This is called immediately after task completion to enable bottom-up
    work generation. Unlike the batch initiative cycle, this runs on
    every successful task completion (with probability check).

    Args:
        employee_id: The employee who completed the task
        completed_task: The task dict that was just completed

    Returns:
        A proposal dict with title, description, proposal_type, or None
        if rate-limited or no follow-up is appropriate.
    """
    # Check rate limiting first
    can_submit, _reason = can_submit_proposal(employee_id)
    if not can_submit:
        return None

    # POST-COMPLETION FOLLOW-UPS DISABLED.
    #
    # Every completed task was spawning "Improve test coverage for...",
    # "Add regression test for...", "Add tests for...", "Verify and test..."
    # follow-ups. These follow-ups then completed and spawned their own
    # follow-ups, creating an infinite busywork cascade that filled the
    # queue with low-value derivative tasks instead of real feature work.
    #
    # The daemon should only work on tasks from:
    # 1. Human submissions (/company-request)
    # 2. Roadmap scanning (discovery thread)
    # 3. Goal-driven task generation
    #
    # Re-enable selectively if needed by checking forge-config.json
    # for autoGeneratedTasks.postCompletionFollowUps = true.
    return None


def submit_post_completion_proposal(
    employee_id: str,
    completed_task: dict,
) -> dict:
    """
    Submit a post-completion proposal to the work queue.

    This is the wrapper function called from operation_loop.py after
    successful task completion. It generates a proposal and submits it
    if one is appropriate.

    Args:
        employee_id: The employee who completed the task
        completed_task: The task dict that was just completed

    Returns:
        Result dict with:
        - success: bool
        - proposal_id: str | None
        - reason: str (explanation of outcome)
    """
    _ensure_imports()

    # Generate proposal
    proposal = generate_post_completion_proposal(employee_id, completed_task)

    if proposal is None:
        # Rate limited or no appropriate follow-up
        can_submit, reason = can_submit_proposal(employee_id)
        if not can_submit:
            return {
                "success": False,
                "proposal_id": None,
                "reason": reason,
            }
        return {
            "success": False,
            "proposal_id": None,
            "reason": "No appropriate follow-up for this task type",
        }

    # Submit the proposal
    try:
        result = work_allocator.submit_proposal(
            title=proposal.get("title", "Untitled"),
            proposer_id=employee_id,
            description=proposal.get("description", ""),
            proposal_type=proposal.get("proposal_type", "follow_up"),
        )

        if result.get("success"):
            return {
                "success": True,
                "proposal_id": result.get("proposal_id"),
                "reason": f"Submitted follow-up proposal: {proposal.get('title', '')[:50]}",
            }
        else:
            return {
                "success": False,
                "proposal_id": None,
                "reason": result.get("error", "Unknown submission error"),
            }
    except Exception as e:
        return {
            "success": False,
            "proposal_id": None,
            "reason": f"Submission failed: {str(e)}",
        }


def generate_follow_up_proposals(
    employee_id: str,
    max_proposals: int = 3,
) -> list[dict]:
    """
    Generate follow-up proposal ideas from recent work.

    Analyzes recently completed tasks and suggests logical follow-ups.

    Returns:
        List of proposal suggestions (not yet submitted)
    """
    # FOLLOW-UP PROPOSAL GENERATION DISABLED.
    #
    # Sibling function generate_post_completion_proposal was disabled for
    # the same reason (employee_initiative.py:340-355). This function
    # escaped that earlier fix and continued producing cascade-prefixed
    # titles by prepending "Document: {title}", "Improve test coverage
    # for: {title}", etc. When those follow-up tasks complete, the next
    # cycle re-wraps them into "Document: Document: {title}" and
    # eventually "Document: Document: Document: Document: ..." — observed
    # on 2026-04-19 in merged PRs #958, #959, #960.
    #
    # Even though the pipeline ships these PRs cleanly (phantom guard
    # passes, CI stays green), the content is low-value derivative work
    # that pollutes main and consumes compute/API budget that should go
    # to real feature tasks.
    #
    # Re-enable selectively by checking forge-config.json for
    # autoGeneratedTasks.followUpProposals = true AND adding a guard
    # that skips completions whose title or source already marks them
    # as follow-ups (prevents the recursive wrapping).
    return []


def generate_todo_proposals(
    employee_id: str,
    max_proposals: int = 3,
) -> list[dict]:
    """
    Generate proposals from TODO comments in codebase.

    Returns:
        List of proposal suggestions (not yet submitted)
    """
    todos = scan_todos(employee_id, max_results=max_proposals * 2)
    proposals = []

    for todo in todos[:max_proposals]:
        # Create a proposal from the TODO
        file_name = Path(todo["file"]).name
        proposals.append(
            {
                "title": f"[{todo['type']}] {todo['text'][:60]}",
                "description": (
                    f"Found in {file_name}:{todo['line']}\n\n"
                    f"Context:\n```\n{todo['context']}\n```"
                ),
                "proposal_type": todo["proposal_type"],
                "source_file": todo["file"],
                "source_line": todo["line"],
            }
        )

    return proposals


def submit_initiative_proposals(
    employee_id: str,
    proposals: list[dict],
    auto_submit: bool = True,
) -> list[dict]:
    """
    Submit initiative proposals to the work queue.

    Args:
        employee_id: The employee submitting proposals
        proposals: List of proposal dicts with title, description, proposal_type
        auto_submit: If True, actually submit; if False, just validate

    Returns:
        List of results for each proposal
    """
    _ensure_imports()

    results = []

    for proposal in proposals:
        # Check rate limiting
        can_submit, reason = can_submit_proposal(employee_id)
        if not can_submit:
            results.append(
                {
                    "success": False,
                    "title": proposal.get("title"),
                    "error": reason,
                }
            )
            continue

        if auto_submit:
            result = work_allocator.submit_proposal(
                title=proposal.get("title", "Untitled"),
                proposer_id=employee_id,
                description=proposal.get("description", ""),
                proposal_type=proposal.get("proposal_type", "improvement"),
            )
        else:
            result = {
                "success": True,
                "dry_run": True,
                "title": proposal.get("title"),
                "message": "Would submit proposal",
            }

        results.append(result)

    return results


def run_initiative_cycle(
    employee_id: str,
    sources: list[str] | None = None,
    max_proposals: int = 2,
    auto_submit: bool = True,
) -> dict:
    """
    Run a full initiative cycle for an employee.

    Generates and optionally submits proposals from multiple sources.

    Args:
        employee_id: The employee to run initiative for
        sources: Sources to use (todos, follow_ups, improvements)
        max_proposals: Maximum proposals to generate
        auto_submit: If True, submit proposals; if False, dry run

    Returns:
        Dict with cycle results
    """
    if sources is None:
        sources = ["todos", "follow_ups"]

    # Check rate limiting first
    can_submit, reason = can_submit_proposal(employee_id)
    if not can_submit:
        return {
            "success": False,
            "employee_id": employee_id,
            "error": reason,
            "proposals_generated": 0,
            "proposals_submitted": 0,
        }

    all_proposals = []

    # Gather proposals from each source
    if "todos" in sources:
        todo_proposals = generate_todo_proposals(
            employee_id, max_proposals=max_proposals
        )
        all_proposals.extend(todo_proposals)

    if "follow_ups" in sources:
        follow_up_proposals = generate_follow_up_proposals(
            employee_id, max_proposals=max_proposals
        )
        all_proposals.extend(follow_up_proposals)

    # Limit total proposals
    proposals_to_submit = all_proposals[:max_proposals]

    # Submit proposals
    results = submit_initiative_proposals(
        employee_id=employee_id,
        proposals=proposals_to_submit,
        auto_submit=auto_submit,
    )

    submitted = sum(1 for r in results if r.get("success"))

    return {
        "success": True,
        "employee_id": employee_id,
        "proposals_generated": len(all_proposals),
        "proposals_submitted": submitted,
        "auto_submit": auto_submit,
        "results": results,
    }


def get_initiative_stats() -> dict:
    """
    Get statistics on employee initiative activity.

    Returns:
        Dict with initiative metrics
    """
    _ensure_imports()
    queue = work_allocator.load_queue()
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    last_week = now - timedelta(days=7)

    # Count proposals by status
    proposals = queue.get("proposed", [])
    completed = queue.get("completed", [])

    # Recent proposals
    recent_proposals = []
    for p in proposals:
        proposed_at_str = p.get("proposed_at")
        if proposed_at_str:
            try:
                proposed_at = datetime.fromisoformat(
                    proposed_at_str.replace("Z", "+00:00")
                )
                if proposed_at >= last_24h:
                    recent_proposals.append(p)
            except (ValueError, TypeError):
                pass

    # Count accepted/rejected
    accepted_count = 0
    rejected_count = 0
    for task in completed:
        if task.get("status") == "rejected":
            rejected_count += 1
        elif task.get("proposed_by"):
            # Was a proposal that got completed
            completed_at_str = task.get("completed_at")
            if completed_at_str:
                try:
                    completed_at = datetime.fromisoformat(
                        completed_at_str.replace("Z", "+00:00")
                    )
                    if completed_at >= last_week:
                        accepted_count += 1
                except (ValueError, TypeError):
                    pass

    # Proposals by employee
    by_employee: dict[str, int] = {}
    for p in proposals:
        emp = p.get("proposed_by", "unknown")
        by_employee[emp] = by_employee.get(emp, 0) + 1

    return {
        "total_pending_proposals": len(proposals),
        "proposals_last_24h": len(recent_proposals),
        "accepted_last_week": accepted_count,
        "rejected_last_week": rejected_count,
        "proposals_by_employee": by_employee,
        "approval_rate": (
            accepted_count / (accepted_count + rejected_count)
            if (accepted_count + rejected_count) > 0
            else 0.0
        ),
    }


def print_help():
    """Print usage help."""
    help_text = """
Employee Initiative Module (P26)

Commands:
    propose         Run initiative cycle for an employee
    scan-todos      Scan codebase for TODOs
    follow-ups      Generate follow-up proposals
    stats           Get initiative statistics

Options:
    --employee-id ID    Employee ID (required for most commands)
    --max-proposals N   Maximum proposals to generate (default: 2)
    --dry-run           Don't actually submit proposals
    --sources LIST      Comma-separated sources: todos,follow_ups

Examples:
    python employee_initiative.py propose --employee-id senior-python-developer
    python employee_initiative.py scan-todos --employee-id senior-python-developer
    python employee_initiative.py stats
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "propose":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)

            sources = None
            if "sources" in args:
                sources = [s.strip() for s in args["sources"].split(",")]

            result = run_initiative_cycle(
                employee_id=args["employee_id"],
                sources=sources,
                max_proposals=int(args.get("max_proposals", 2)),
                auto_submit=args.get("dry_run", False) is not True,
            )
            print(json.dumps(result, indent=2))

        elif command == "scan-todos":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)

            todos = scan_todos(
                employee_id=args["employee_id"],
                max_results=int(args.get("max_results", 10)),
            )
            print(json.dumps(todos, indent=2))

        elif command == "follow-ups":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)

            proposals = generate_follow_up_proposals(
                employee_id=args["employee_id"],
                max_proposals=int(args.get("max_proposals", 3)),
            )
            print(json.dumps(proposals, indent=2))

        elif command == "stats":
            stats = get_initiative_stats()
            print(json.dumps(stats, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
