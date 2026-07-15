#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
WS-066-001: PR Rollback Executor

Automated rollback for daemon PRs that fail CI after merge.

Safety gates:
- Only reverts daemon/* branches
- Checks for conflicts before pushing
- Rate-limited (max N per day)
- Protected paths cannot be reverted
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Protected paths that cannot be auto-reverted
PROTECTED_PATHS = [
    "CLAUDE.md",
    "forge-config.json",
    ".claude/settings.json",
    ".env",
]

# Max rollbacks per day to prevent runaway reverts
MAX_ROLLBACKS_PER_DAY = 5

# WS-066-001: Only CI *correctness* gates may trigger a destructive rollback.
# A rollback reverts merged code, so the trigger set is an ALLOWLIST (fail-closed):
# a failing check whose name is not a known correctness gate does NOT trigger a
# revert — a human reviews instead. This excludes infra/deploy jobs such as
# "Deploy" (runs on forge-website/** changes, fails on this repo for lack of
# deploy credentials) and "Auto-Merge", whose failures are environmental and are
# never grounds to revert otherwise-correct merged work. Matched by name prefix
# so suffixed jobs like "Install Test (macos-latest)" are covered.
DEFAULT_ROLLBACK_GATING_CHECKS = (
    "Test",  # also covers "Test Staleness Detection"
    "Lint",
    "Security",
    "Hooks",  # "Hooks Validate"
    "Install Test",  # "Install Test (macos-latest)" / "(ubuntu-latest)"
)


def select_gating_failures(
    checks: list[dict],
    gating_checks: tuple[str, ...] = DEFAULT_ROLLBACK_GATING_CHECKS,
) -> list[dict]:
    """Return only the check-run dicts that (a) concluded in failure AND (b) are
    correctness gates whose name starts with one of ``gating_checks``.

    Infra/deploy jobs (e.g. "Deploy", "Auto-Merge") are excluded even when
    failing, so they can never trigger a rollback of correct merged code.
    """
    prefixes = tuple(p.lower() for p in gating_checks)
    failures = []
    for check in checks:
        if check.get("conclusion") != "failure":
            continue
        name = (check.get("name", "") or "").lower()
        if name.startswith(prefixes):
            failures.append(check)
    return failures


def validate_rollback_safety(
    pr_number: int,
    commit_sha: str,
    company_dir: Path,
) -> tuple[bool, list[str]]:
    """
    Validate that a rollback is safe to execute.

    Returns:
        (is_safe, list of warnings/reasons if not safe)
    """
    warnings = []

    # Check rate limit
    state_file = company_dir / "state" / "rollback_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            today = datetime.now(timezone.utc).date().isoformat()
            today_count = sum(
                1
                for r in state.get("rollbacks", [])
                if r.get("timestamp", "").startswith(today)
            )
            if today_count >= MAX_ROLLBACKS_PER_DAY:
                warnings.append(
                    f"Rate limit: {today_count} rollbacks today (max {MAX_ROLLBACKS_PER_DAY})"
                )
                return False, warnings
        except Exception:
            pass

    # Check what files were changed in the commit
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            changed_files = result.stdout.strip().split("\n")
            for f in changed_files:
                if any(f.endswith(p) or f == p for p in PROTECTED_PATHS):
                    warnings.append(f"Protected path modified: {f}")
                    return False, warnings
    except Exception as e:
        warnings.append(f"Could not check changed files: {e}")

    return True, warnings


def execute_rollback(
    pr_number: int,
    pr_title: str,
    commit_sha: str,
    reason: str,
    failure_details: dict,
    company_dir: Path,
) -> dict:
    """
    Execute a rollback for a failed PR.

    Steps:
    1. Validate safety
    2. Create rollback branch
    3. git revert the commit
    4. Push branch
    5. Create PR
    6. Auto-merge if enabled

    Returns:
        dict with success status and details
    """
    result = {
        "success": False,
        "rollback_pr_number": None,
        "rollback_branch": None,
        "error": None,
    }

    # Step 1: Validate safety
    is_safe, warnings = validate_rollback_safety(pr_number, commit_sha, company_dir)
    if not is_safe:
        result["error"] = f"Unsafe rollback: {'; '.join(warnings)}"
        logger.warning(f"Rollback blocked for PR #{pr_number}: {warnings}")
        return result

    # Step 2: Create rollback branch
    rollback_branch = f"rollback/revert-pr{pr_number}-{commit_sha[:8]}"
    result["rollback_branch"] = rollback_branch

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True,
            timeout=60,
        )

        # Create branch from main
        checkout_result = subprocess.run(
            ["git", "checkout", "-b", rollback_branch, "origin/main"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checkout_result.returncode != 0:
            result["error"] = f"Failed to create branch: {checkout_result.stderr}"
            return result

        # Step 3: Revert the commit
        revert_result = subprocess.run(
            ["git", "revert", "--no-edit", commit_sha],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if revert_result.returncode != 0:
            # Abort and cleanup
            subprocess.run(
                ["git", "revert", "--abort"], capture_output=True, timeout=10
            )
            subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10)
            subprocess.run(
                ["git", "branch", "-D", rollback_branch],
                capture_output=True,
                timeout=10,
            )
            result["error"] = f"Revert conflict: {revert_result.stderr}"
            return result

        # Step 4: Push branch
        push_result = subprocess.run(
            ["git", "push", "-u", "origin", rollback_branch],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if push_result.returncode != 0:
            result["error"] = f"Push failed: {push_result.stderr}"
            _cleanup_branch(rollback_branch)
            return result

        # Step 5: Create PR
        pr_body = _build_rollback_pr_body(
            pr_number, pr_title, commit_sha, reason, failure_details
        )
        pr_result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"Revert PR #{pr_number}: {pr_title[:50]}",
                "--body",
                pr_body,
                "--base",
                "main",
                "--head",
                rollback_branch,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if pr_result.returncode != 0:
            result["error"] = f"PR creation failed: {pr_result.stderr}"
            return result

        rollback_pr_number = _parse_pr_number(pr_result.stdout)
        result["rollback_pr_number"] = rollback_pr_number

        # Step 6: Enable auto-merge
        if rollback_pr_number:
            subprocess.run(
                ["gh", "pr", "merge", str(rollback_pr_number), "--auto", "--squash"],
                capture_output=True,
                timeout=30,
            )

        # Record the rollback
        _record_rollback(
            company_dir=company_dir,
            original_pr=pr_number,
            rollback_pr=rollback_pr_number,
            commit_sha=commit_sha,
            reason=reason,
        )

        # Log activity
        log_rollback_activity(
            pr_number=pr_number,
            pr_title=pr_title,
            rollback_pr_number=rollback_pr_number,
            reason=reason,
            failure_type="ci_check_failure",
            failure_details=failure_details,
            company_dir=company_dir,
        )

        result["success"] = True
        logger.info(f"Rollback PR #{rollback_pr_number} created for PR #{pr_number}")

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Rollback failed for PR #{pr_number}: {e}")
    finally:
        # Always return to main
        subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10)

    return result


def _cleanup_branch(branch: str) -> None:
    """Clean up a failed rollback branch."""
    subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10)
    subprocess.run(["git", "branch", "-D", branch], capture_output=True, timeout=10)


def _parse_pr_number(output: str) -> int | None:
    """Parse PR number from gh pr create output."""
    if not output:
        return None
    # Output is like: https://github.com/org/repo/pull/123
    match = re.search(r"/pull/(\d+)", output)
    return int(match.group(1)) if match else None


def _build_rollback_pr_body(
    pr_number: int,
    pr_title: str,
    commit_sha: str,
    reason: str,
    failure_details: dict,
) -> str:
    """Build the PR body for a rollback PR."""
    failed_checks = failure_details.get("failed_checks", [])
    checks_list = (
        "\n".join(
            f"- {c.get('name', 'Unknown')}: {c.get('conclusion', 'failed')}"
            for c in failed_checks
        )
        or "- (no details)"
    )

    return f"""## Automated Rollback [WS-066-001]

This PR reverts #{pr_number} which failed after merge.

### Original PR
- **PR:** #{pr_number}
- **Title:** {pr_title}
- **Commit:** `{commit_sha[:12]}`

### Failure Reason
{reason}

### Failed Checks
{checks_list}

---
*This rollback was created automatically by the daemon rollback monitor.*
"""


def _record_rollback(
    company_dir: Path,
    original_pr: int,
    rollback_pr: int | None,
    commit_sha: str,
    reason: str,
) -> None:
    """Record rollback in state file for tracking."""
    state_dir = company_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "rollback_state.json"

    state = {"rollbacks": []}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass

    state["rollbacks"].append(
        {
            "original_pr": original_pr,
            "rollback_pr": rollback_pr,
            "commit_sha": commit_sha,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Keep last 100 entries
    state["rollbacks"] = state["rollbacks"][-100:]

    state_file.write_text(json.dumps(state, indent=2))


def log_rollback_activity(
    pr_number: int,
    pr_title: str,
    rollback_pr_number: int | None,
    reason: str,
    failure_type: str,
    failure_details: dict,
    company_dir: Path,
) -> None:
    """Log rollback to activity JSONL file."""
    state_dir = company_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    activity_file = state_dir / "activity.jsonl"

    entry = {
        "event_type": "rollback_executed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original_pr_number": pr_number,
        "original_pr_title": pr_title,
        "rollback_pr_number": rollback_pr_number,
        "reason": reason,
        "failure_type": failure_type,
        "failure_details": failure_details,
    }

    with open(activity_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def detect_failed_merges(
    lookback_hours: int = 1,
    company_dir: Path | None = None,
    gating_checks: tuple[str, ...] = DEFAULT_ROLLBACK_GATING_CHECKS,
) -> list[dict]:
    """
    Detect recently merged daemon PRs that have failing CI on main.

    Only failures of correctness gates in ``gating_checks`` count — infra/deploy
    job failures (e.g. "Deploy") never trigger a rollback. See
    :func:`select_gating_failures`.

    Returns list of PRs that need rollback.
    """
    failed_prs = []

    try:
        # Get recently merged daemon PRs
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                "head:daemon/",
                "--json",
                "number,title,mergeCommit,mergedAt",
                "--limit",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return []

        prs = json.loads(result.stdout or "[]")

        # Filter by merge time (lookback_hours)
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        filtered_prs = []
        for pr in prs:
            merged_at = pr.get("mergedAt", "")
            if merged_at:
                try:
                    merge_time = datetime.fromisoformat(
                        merged_at.replace("Z", "+00:00")
                    )
                    if merge_time >= cutoff_time:
                        filtered_prs.append(pr)
                except (ValueError, TypeError):
                    pass
        prs = filtered_prs

        # Check each PR's merge commit for failing checks
        for pr in prs:
            commit_sha = pr.get("mergeCommit", {}).get("oid")
            if not commit_sha:
                continue

            # Check commit status
            status_result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{{owner}}/{{repo}}/commits/{commit_sha}/check-runs",
                    "--jq",
                    ".check_runs[] | {name, conclusion}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if status_result.returncode == 0:
                # Parse check results
                parsed_checks = []
                for line in status_result.stdout.strip().split("\n"):
                    if line:
                        try:
                            parsed_checks.append(json.loads(line))
                        except Exception:
                            pass

                # WS-066-001: only correctness-gate failures justify a rollback;
                # infra/deploy job failures (e.g. "Deploy") are ignored.
                failed_checks = select_gating_failures(parsed_checks, gating_checks)

                if failed_checks:
                    failed_prs.append(
                        {
                            "pr_number": pr["number"],
                            "pr_title": pr["title"],
                            "commit_sha": commit_sha,
                            "failed_checks": failed_checks,
                        }
                    )

    except Exception as e:
        logger.error(f"Failed to detect failed merges: {e}")

    return failed_prs


if __name__ == "__main__":
    # CLI for manual testing
    import argparse

    parser = argparse.ArgumentParser(description="PR Rollback Executor")
    parser.add_argument("--detect", action="store_true", help="Detect failed merges")
    parser.add_argument("--rollback", type=int, help="Rollback a specific PR number")
    args = parser.parse_args()

    company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"

    if args.detect:
        failed = detect_failed_merges(company_dir=company_dir)
        print(f"Found {len(failed)} failed merges:")
        for pr in failed:
            print(f"  PR #{pr['pr_number']}: {pr['pr_title']}")

    elif args.rollback:
        print("Manual rollback not implemented via CLI yet")
