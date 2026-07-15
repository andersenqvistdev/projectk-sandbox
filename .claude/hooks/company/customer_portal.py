#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
customer_portal.py — Customer-facing status portal.

WS-109-009: Self-serve customer status portal.

Provides:
- License key validation
- Work request status lookup
- PR progress tracking
- Notification history

Usage:
    # CLI
    uv run customer_portal.py status LICENSE_KEY
    uv run customer_portal.py requests LICENSE_KEY
    uv run customer_portal.py notifications LICENSE_KEY

    # As module
    from customer_portal import get_customer_status
    status = get_customer_status("LICENSE_KEY_HERE")
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Resolve paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
COMPANY_DIR = PROJECT_ROOT / ".company"


def _load_customers() -> list[dict]:
    """Load all customers."""
    customers_file = COMPANY_DIR / "customers" / "customers.json"
    if not customers_file.exists():
        return []
    try:
        with open(customers_file) as f:
            data = json.load(f)
        return data.get("customers", [])
    except (json.JSONDecodeError, OSError):
        return []


def _find_customer_by_license(license_key: str) -> dict | None:
    """Find customer by license key."""
    customers = _load_customers()
    for customer in customers:
        if customer.get("license_key") == license_key:
            return customer
        # Also check licenses array
        for lic in customer.get("licenses", []):
            if lic.get("key") == license_key:
                return customer
    return None


def _get_customer_requests(customer_id: str) -> list[dict]:
    """Get all work requests for a customer."""
    requests = []

    # Check work queue for tasks with this customer_id
    queue_file = COMPANY_DIR / "state" / "work_queue.json"
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                data = json.load(f)

            for queue_name in ["pending", "in_progress", "completed", "blocked"]:
                for task in data.get(queue_name, []):
                    if task.get("customer_id") == customer_id:
                        requests.append(
                            {
                                "task_id": task.get("task_id"),
                                "title": task.get("title"),
                                "status": queue_name,
                                "created_at": task.get("created_at"),
                                "completed_at": task.get("completed_at"),
                                "priority": task.get("priority"),
                            }
                        )
        except (json.JSONDecodeError, OSError):
            pass

    # Also check results for completed work
    results_dir = COMPANY_DIR / "results"
    if results_dir.exists():
        for result_file in sorted(results_dir.glob("*.json"), reverse=True)[:100]:
            try:
                with open(result_file) as f:
                    data = json.load(f)
                if data.get("customer_id") == customer_id:
                    # Avoid duplicates
                    task_id = data.get("task_id")
                    if not any(r["task_id"] == task_id for r in requests):
                        requests.append(
                            {
                                "task_id": task_id,
                                "title": data.get("title", "Unknown"),
                                "status": "completed",
                                "created_at": data.get("started_at"),
                                "completed_at": data.get("completed_at"),
                                "pr_url": data.get("pr_url"),
                                "pr_number": data.get("pr_number"),
                            }
                        )
            except (json.JSONDecodeError, OSError):
                pass

    return sorted(requests, key=lambda x: x.get("created_at", ""), reverse=True)[:50]


def _get_customer_notifications(customer_id: str) -> list[dict]:
    """Get notifications for a customer."""
    notifications = []
    notif_dir = COMPANY_DIR / "notifications"

    if not notif_dir.exists():
        return notifications

    for notif_file in sorted(notif_dir.glob("notif-*.json"), reverse=True)[:20]:
        try:
            with open(notif_file) as f:
                data = json.load(f)
            if data.get("customer_id") == customer_id:
                notifications.append(data)
        except (json.JSONDecodeError, OSError):
            pass

    return notifications


def get_customer_status(license_key: str) -> dict[str, Any]:
    """
    Get full customer status by license key.

    Returns:
        dict with customer info, active requests, and recent notifications
    """
    result: dict[str, Any] = {
        "success": False,
        "error": None,
        "customer": None,
        "requests": [],
        "notifications": [],
        "summary": {},
    }

    # Validate license
    customer = _find_customer_by_license(license_key)
    if not customer:
        result["error"] = "Invalid license key"
        return result

    customer_id = customer.get("id")

    # Safe customer info (exclude sensitive fields)
    result["customer"] = {
        "id": customer_id,
        "name": customer.get("name"),
        "plan": customer.get("plan", "trial"),
        "status": customer.get("status", "active"),
    }

    # Get requests
    requests = _get_customer_requests(customer_id)
    result["requests"] = requests

    # Get notifications
    notifications = _get_customer_notifications(customer_id)
    result["notifications"] = notifications

    # Summary
    result["summary"] = {
        "total_requests": len(requests),
        "pending": len([r for r in requests if r.get("status") == "pending"]),
        "in_progress": len([r for r in requests if r.get("status") == "in_progress"]),
        "completed": len([r for r in requests if r.get("status") == "completed"]),
        "blocked": len([r for r in requests if r.get("status") == "blocked"]),
        "recent_notifications": len(notifications),
    }

    result["success"] = True
    return result


def submit_customer_feedback(
    license_key: str, message: str, request_id: str | None = None
) -> dict[str, Any]:
    """
    Append customer feedback to the customer's feedback log.

    Venture-factory foundation slice: gives customers a channel to
    inject scope changes, rejections, or requests mid-flight. Writes
    to .company/customers/<customer_id>/feedback.json; the escalation
    router / daemon will be updated later to read and surface these.

    Args:
        license_key: Customer license key for authentication
        message: The feedback message text
        request_id: Optional task_id this feedback is about

    Returns:
        dict with success flag + feedback entry metadata
    """
    customer = _find_customer_by_license(license_key)
    if not customer:
        return {"success": False, "error": "Invalid license key"}
    if not message or not message.strip():
        return {"success": False, "error": "Feedback message is required"}

    customer_id = customer.get("id")
    feedback_dir = COMPANY_DIR / "customers" / customer_id
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = feedback_dir / "feedback.json"

    existing: list[dict] = []
    if feedback_path.exists():
        try:
            with open(feedback_path) as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
        "request_id": request_id,
        "message": message.strip(),
        "status": "unacknowledged",
    }
    existing.append(entry)

    # Atomic write via temp-in-same-dir + rename
    import os as _os
    import tempfile

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(feedback_dir),
        prefix=".feedback.",
        suffix=".tmp",
    )
    try:
        json.dump(existing, tmp, indent=2)
        tmp.flush()
        _os.fsync(tmp.fileno())
        tmp.close()
        _os.replace(tmp.name, str(feedback_path))
    except Exception:
        _os.unlink(tmp.name)
        raise

    return {
        "success": True,
        "customer_id": customer_id,
        "feedback_path": str(feedback_path),
        "feedback_count": len(existing),
        "entry": entry,
    }


def format_status_display(status: dict) -> str:
    """Format status for terminal display."""
    if not status.get("success"):
        return f"Error: {status.get('error', 'Unknown error')}"

    lines = [
        "═" * 70,
        f" CUSTOMER STATUS                     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "═" * 70,
        "",
        f" Customer: {status['customer']['name']}",
        f" Plan: {status['customer']['plan']}",
        f" Status: {status['customer']['status']}",
        "",
        "─" * 70,
        " SUMMARY",
        "─" * 70,
        f" Total Requests: {status['summary']['total_requests']}",
        f"   Pending:     {status['summary']['pending']}",
        f"   In Progress: {status['summary']['in_progress']}",
        f"   Completed:   {status['summary']['completed']}",
        f"   Blocked:     {status['summary']['blocked']}",
        "",
    ]

    if status["requests"]:
        lines.extend(
            [
                "─" * 70,
                " RECENT REQUESTS",
                "─" * 70,
            ]
        )
        for req in status["requests"][:10]:
            status_icon = {
                "pending": "○",
                "in_progress": "◐",
                "completed": "●",
                "blocked": "✗",
            }.get(req.get("status", ""), "?")
            pr_info = f" → PR #{req['pr_number']}" if req.get("pr_number") else ""
            lines.append(f" {status_icon} {req['title'][:50]}{pr_info}")
        lines.append("")

    if status["notifications"]:
        lines.extend(
            [
                "─" * 70,
                " RECENT NOTIFICATIONS",
                "─" * 70,
            ]
        )
        for notif in status["notifications"][:5]:
            lines.append(f" • {notif.get('pr_title', 'Unknown')}")
        lines.append("")

    lines.append("═" * 70)
    return "\n".join(lines)


def main():
    """CLI entry point."""
    if len(sys.argv) < 3:
        print("Usage: customer_portal.py <command> <license_key> [args...]")
        print("")
        print("Commands:")
        print("  status        Full customer status")
        print("  requests      List work requests")
        print("  notifications List notifications")
        print(
            "  feedback      Submit feedback: feedback <license> <message> [--request-id ID]"
        )
        print("")
        print("Example:")
        print("  uv run customer_portal.py status YOUR_LICENSE_KEY")
        print(
            '  uv run customer_portal.py feedback YOUR_LICENSE_KEY "Please add dark mode"'
        )
        sys.exit(1)

    command = sys.argv[1]
    license_key = sys.argv[2]

    if command == "status":
        status = get_customer_status(license_key)
        print(format_status_display(status))
    elif command == "requests":
        status = get_customer_status(license_key)
        if status["success"]:
            print(json.dumps(status["requests"], indent=2, default=str))
        else:
            print(f"Error: {status['error']}")
    elif command == "notifications":
        status = get_customer_status(license_key)
        if status["success"]:
            print(json.dumps(status["notifications"], indent=2, default=str))
        else:
            print(f"Error: {status['error']}")
    elif command == "json":
        status = get_customer_status(license_key)
        print(json.dumps(status, indent=2, default=str))
    elif command == "feedback":
        if len(sys.argv) < 4:
            print(
                "Usage: feedback <license_key> <message> [--request-id ID]",
                file=sys.stderr,
            )
            sys.exit(1)
        message = sys.argv[3]
        request_id = None
        if "--request-id" in sys.argv:
            i = sys.argv.index("--request-id")
            if i + 1 < len(sys.argv):
                request_id = sys.argv[i + 1]
        result = submit_customer_feedback(license_key, message, request_id=request_id)
        if result.get("success"):
            print(
                f"Feedback recorded ({result['feedback_count']} total) at "
                f"{result['feedback_path']}"
            )
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
