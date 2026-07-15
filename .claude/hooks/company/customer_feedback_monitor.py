#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
customer_feedback_monitor.py — Surface unacknowledged customer feedback.

Venture-factory foundation promised a feedback channel
(`submit_customer_feedback()` in customer_portal.py, shipped PR #965) but
never wired it to anything. Feedback entries landed on disk and sat there
— a silent dead-end for customers trying to steer their venture.

This module closes that loop. Each cycle:

1. Walk every customer's feedback log at
   `.company/customers/<customer_id>/feedback.json`
2. For every entry with `status: "unacknowledged"`, queue a review task
   assigned to the `customer-success-lead` agent via work_allocator
3. Once the task queues successfully, atomically flip the entry's status
   to `"surfaced"` and stamp `surfaced_at` + `surfaced_as_title` so the
   same entry is never re-surfaced

Called from:
- CLI: `uv run customer_feedback_monitor.py scan [--dry-run]`
- Daemon hook: `surface_pending_feedback(company_root)` on each cycle

Design notes:
- Idempotence comes from the feedback entry's own status flip, NOT from
  queue-level dedup. Once flipped to "surfaced" the entry is ignored.
  This is stronger than title-matching the queue because it survives
  queue cleanup / purges.
- Fail-safe: if task queueing fails, the entry stays "unacknowledged"
  so the next cycle retries. The status flip is the last step.
- Atomic write: feedback.json is rewritten via tempfile + rename, same
  pattern as customer_portal.submit_customer_feedback().
- Never clobbers a "resolved" entry (human marker). Only flips
  "unacknowledged" → "surfaced". Any other status is left alone.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Lazy import guard — same pattern as venture_scope_monitor.
work_allocator = None
company_resolver = None


def _ensure_imports() -> None:
    """Lazily import sibling modules so this file is CLI-usable standalone."""
    global work_allocator, company_resolver
    if work_allocator is not None:
        return
    try:
        from . import company_resolver as cr
        from . import work_allocator as wa
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]
    work_allocator = wa
    company_resolver = cr


# Statuses the monitor understands. Anything else is left alone (forward-
# compatible with future extensions the portal may add).
STATUS_UNACKNOWLEDGED = "unacknowledged"
STATUS_SURFACED = "surfaced"
STATUS_RESOLVED = "resolved"


# ---------------------------------------------------------------------------
# Feedback discovery
# ---------------------------------------------------------------------------


def find_customer_feedback(company_root: Path) -> list[dict[str, Any]]:
    """Find every feedback.json under the company customers tree.

    Expected layout:
        <company_root>/.company/customers/<customer_id>/feedback.json

    Returns one dict per customer with a feedback file, even if empty:
      - customer_id: directory name
      - feedback_path: Path to feedback.json
      - entries: list[dict] (parsed); empty list on parse error
      - parse_error: str | None (for visibility upstream)
    """
    customers_root = company_root / ".company" / "customers"
    results: list[dict[str, Any]] = []

    if not customers_root.is_dir():
        return results

    for customer_dir in sorted(customers_root.iterdir()):
        if not customer_dir.is_dir():
            continue
        feedback_path = customer_dir / "feedback.json"
        if not feedback_path.exists():
            continue

        entries: list[dict[str, Any]] = []
        parse_error: str | None = None
        try:
            raw = json.loads(feedback_path.read_text())
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
            else:
                parse_error = "feedback.json root is not a list"
        except (json.JSONDecodeError, OSError) as e:
            parse_error = f"unreadable: {e}"

        results.append(
            {
                "customer_id": customer_dir.name,
                "feedback_path": feedback_path,
                "entries": entries,
                "parse_error": parse_error,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Entry classification + task construction
# ---------------------------------------------------------------------------


def needs_surfacing(entry: dict[str, Any]) -> bool:
    """True iff this entry has not been surfaced yet.

    Only the literal "unacknowledged" status qualifies. Any other value
    (surfaced / resolved / unknown-future-status) is left alone so this
    module never regresses state set by another component.
    """
    if not isinstance(entry, dict):
        return False
    return entry.get("status") == STATUS_UNACKNOWLEDGED


def _feedback_preview(message: str, limit: int = 60) -> str:
    """One-line preview of a feedback message for use in task titles."""
    single_line = " ".join(str(message or "").split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1].rstrip() + "…"


def build_feedback_task(customer_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Construct a customer-success review task payload for a feedback entry.

    Does NOT submit to the queue — returns the dict for the caller to enqueue.
    """
    preview = _feedback_preview(entry.get("message", ""))
    request_ref = entry.get("request_id")
    timestamp = entry.get("timestamp") or ""

    description_lines = [
        f"Customer `{customer_id}` submitted feedback:",
        "",
        f"> {entry.get('message') or '(empty message)'}",
        "",
        f"**Submitted at:** {timestamp}",
    ]
    if request_ref:
        description_lines.append(f"**About task:** `{request_ref}`")
    description_lines += [
        "",
        "Deliverable: acknowledge the feedback, classify it (scope change "
        "/ rejection / request / question), route to the appropriate owner, "
        "and either resolve inline or hand off. Flip the feedback entry's "
        "status to `resolved` once closed — customer_portal is the canonical "
        "state.",
    ]

    return {
        "title": f"Customer feedback: {customer_id} — {preview}",
        "description": "\n".join(description_lines),
        "priority": 2,
        "estimated_complexity": "standard",
        "source": "customer-feedback",
        "assigned_to": "customer-success-lead",
        "required_capabilities": [
            "customer-support",
            "feedback-routing",
            "onboarding",
        ],
        "requires_deliverable": True,
        "customer_id": customer_id,
        "feedback_timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# Atomic feedback.json rewrite
# ---------------------------------------------------------------------------


def _write_feedback_atomic(feedback_path: Path, entries: list[dict[str, Any]]) -> None:
    """Rewrite feedback.json atomically — temp file + os.replace.

    Mirrors customer_portal.submit_customer_feedback()'s write pattern so
    a crash mid-rewrite can't leave the file truncated.
    """
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(feedback_path.parent),
        prefix=".feedback.",
        suffix=".tmp",
    )
    try:
        json.dump(entries, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, str(feedback_path))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# End-to-end: surface any pending feedback
# ---------------------------------------------------------------------------


def surface_pending_feedback(
    company_root: Path, dry_run: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    """Walk every customer's feedback log, queue review tasks for each
    unacknowledged entry, and flip the entry's status to "surfaced" once
    the task is safely in the queue.

    Returns a status dict with counts and any issues for the caller to log.
    """
    _ensure_imports()

    when = (now or datetime.now(timezone.utc)).isoformat()
    customers = find_customer_feedback(company_root)

    surfaced: list[dict[str, str]] = []
    skipped_already_surfaced = 0
    errors: list[str] = []

    for customer_info in customers:
        if customer_info["parse_error"]:
            errors.append(
                f"{customer_info['customer_id']}: {customer_info['parse_error']}"
            )
            continue

        entries = customer_info["entries"]
        customer_id = customer_info["customer_id"]
        any_mutated = False

        for entry in entries:
            if not needs_surfacing(entry):
                if entry.get("status") in (STATUS_SURFACED, STATUS_RESOLVED):
                    skipped_already_surfaced += 1
                continue

            task = build_feedback_task(customer_id, entry)

            if dry_run:
                surfaced.append(
                    {
                        "customer_id": customer_id,
                        "timestamp": entry.get("timestamp", ""),
                        "title": task["title"],
                    }
                )
                continue

            try:
                work_allocator.add_task(
                    title=task["title"],
                    priority=task["priority"],
                    description=task["description"],
                    estimated_complexity=task["estimated_complexity"],
                    source=task["source"],
                    required_capabilities=task["required_capabilities"],
                )
            except Exception as e:
                errors.append(
                    f"{customer_id}: add_task failed "
                    f"(entry {entry.get('timestamp', '?')}): {e}"
                )
                # Leave status unacknowledged so the next cycle retries.
                continue

            # Status flip only after the task is in the queue. Stamp the
            # title so a future audit can trace queue → feedback entry.
            entry["status"] = STATUS_SURFACED
            entry["surfaced_at"] = when
            entry["surfaced_as_title"] = task["title"]
            any_mutated = True
            surfaced.append(
                {
                    "customer_id": customer_id,
                    "timestamp": entry.get("timestamp", ""),
                    "title": task["title"],
                }
            )

        if any_mutated and not dry_run:
            try:
                _write_feedback_atomic(customer_info["feedback_path"], entries)
            except Exception as e:
                errors.append(
                    f"{customer_id}: atomic write of feedback.json failed: {e}"
                )

    return {
        "customers_scanned": len(customers),
        "surfaced_count": len(surfaced),
        "surfaced": surfaced,
        "skipped_already_surfaced": skipped_already_surfaced,
        "errors": errors,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_main() -> int:
    _ensure_imports()
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    if not args or args[0] != "scan":
        print("Usage: customer_feedback_monitor.py scan [--dry-run]")
        print("")
        print("Scans .company/customers/*/feedback.json, raises customer-success")
        print("review tasks for every 'unacknowledged' entry, flips status to")
        print("'surfaced' on success.")
        return 1

    company_root = company_resolver.get_company_dir().parent
    result = surface_pending_feedback(company_root, dry_run=dry_run)
    print(json.dumps(result, indent=2, default=str))
    if result["errors"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
