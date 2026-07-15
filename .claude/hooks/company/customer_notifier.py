#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
customer_notifier.py — Notify customers of work completion.

WS-109-008: Customer notification when work is complete.

Supports:
- In-app notifications (logged to .company/notifications/)
- Slack webhook (if configured)
- Email (placeholder for future implementation)

Usage:
    from customer_notifier import notify_customer_pr_merged

    notify_customer_pr_merged(
        customer_id="cust-123",
        pr_number=456,
        pr_title="feat: Add user auth",
        task_id="task-20260330..."
    )
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Resolve paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
COMPANY_DIR = PROJECT_ROOT / ".company"
NOTIFICATIONS_DIR = COMPANY_DIR / "notifications"


def _ensure_notifications_dir() -> Path:
    """Ensure notifications directory exists."""
    NOTIFICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    return NOTIFICATIONS_DIR


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON file atomically."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_customer(customer_id: str) -> dict | None:
    """Load customer data from customers.json."""
    customers_file = COMPANY_DIR / "customers" / "customers.json"
    if not customers_file.exists():
        return None
    try:
        with open(customers_file) as f:
            data = json.load(f)
        for customer in data.get("customers", []):
            if customer.get("id") == customer_id:
                return customer
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _find_customer_for_task(task_id: str) -> str | None:
    """Find customer_id associated with a task."""
    # Check task results for customer_id
    results_dir = COMPANY_DIR / "results"
    if results_dir.exists():
        for result_file in results_dir.glob(f"{task_id}*.json"):
            try:
                with open(result_file) as f:
                    data = json.load(f)
                if data.get("customer_id"):
                    return data["customer_id"]
            except (json.JSONDecodeError, OSError):
                pass

    # Check work queue history
    queue_file = COMPANY_DIR / "state" / "work_queue.json"
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                data = json.load(f)
            # Search all queues for the task
            for queue_name in ["completed", "archived"]:
                for task in data.get(queue_name, []):
                    if task.get("task_id") == task_id:
                        return task.get("customer_id")
        except (json.JSONDecodeError, OSError):
            pass

    return None


def _send_slack_notification(
    webhook_url: str,
    customer_name: str,
    pr_number: int,
    pr_title: str,
    repo_url: str | None = None,
) -> bool:
    """Send Slack notification via webhook."""
    try:
        import urllib.request

        pr_link = f"{repo_url}/pull/{pr_number}" if repo_url else f"PR #{pr_number}"

        payload = {
            "text": f"Work completed for {customer_name}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":white_check_mark: *Work Completed*\n\n"
                        f"*Customer:* {customer_name}\n"
                        f"*PR:* <{pr_link}|{pr_title}>\n"
                        f"*Status:* Merged and deployed",
                    },
                }
            ],
        }

        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200

    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
        return False


def notify_customer_pr_merged(
    customer_id: str | None = None,
    pr_number: int = 0,
    pr_title: str = "",
    task_id: str | None = None,
    pr_url: str | None = None,
    send_slack: bool = True,
) -> dict[str, Any]:
    """
    Notify a customer that their PR has been merged.

    Args:
        customer_id: Customer ID (or auto-detect from task_id)
        pr_number: PR number
        pr_title: PR title
        task_id: Task ID (used to find customer if not provided)
        pr_url: Full PR URL
        send_slack: Whether to attempt Slack notification

    Returns:
        dict with success status and notification ID
    """
    result: dict[str, Any] = {
        "success": False,
        "notification_id": None,
        "channels": [],
        "errors": [],
    }

    # Find customer
    if not customer_id and task_id:
        customer_id = _find_customer_for_task(task_id)

    if not customer_id:
        result["errors"].append("No customer_id provided or found")
        # Still log the notification for audit
        customer_id = "unknown"

    # Load customer data
    customer = _load_customer(customer_id) if customer_id != "unknown" else None
    customer_name = customer.get("name", customer_id) if customer else customer_id

    # Create notification record
    now = datetime.now(timezone.utc)
    notification_id = f"notif-{now.strftime('%Y%m%d%H%M%S')}-{pr_number}"

    notification = {
        "id": notification_id,
        "type": "pr_merged",
        "customer_id": customer_id,
        "customer_name": customer_name,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_url": pr_url,
        "task_id": task_id,
        "created_at": now.isoformat(),
        "channels_sent": [],
        "channels_failed": [],
    }

    # Save notification record
    try:
        _ensure_notifications_dir()
        notif_file = NOTIFICATIONS_DIR / f"{notification_id}.json"
        _atomic_write_json(notif_file, notification)
        result["notification_id"] = notification_id
        result["channels"].append("file")
        notification["channels_sent"].append("file")
    except Exception as e:
        result["errors"].append(f"Failed to save notification: {e}")

    # Send Slack notification if configured
    if send_slack and customer:
        slack_webhook = customer.get("slack_webhook")
        if not slack_webhook:
            # Try global slack webhook from config
            secrets_file = Path.home() / ".config" / "forge" / "secrets.json"
            if secrets_file.exists():
                try:
                    with open(secrets_file) as f:
                        secrets = json.load(f)
                    slack_webhook = secrets.get("slack", {}).get("webhook_url")
                except (json.JSONDecodeError, OSError):
                    pass

        if slack_webhook:
            # Extract repo URL from pr_url
            repo_url = None
            if pr_url and "/pull/" in pr_url:
                repo_url = pr_url.rsplit("/pull/", 1)[0]

            if _send_slack_notification(
                slack_webhook, customer_name, pr_number, pr_title, repo_url
            ):
                result["channels"].append("slack")
                notification["channels_sent"].append("slack")
            else:
                notification["channels_failed"].append("slack")
                result["errors"].append("Slack notification failed")

    # Update notification record with final status
    try:
        notif_file = NOTIFICATIONS_DIR / f"{notification_id}.json"
        _atomic_write_json(notif_file, notification)
    except Exception:
        pass

    result["success"] = len(result["channels"]) > 0
    return result


def get_pending_notifications(customer_id: str | None = None) -> list[dict]:
    """Get notifications for a customer (or all if no customer_id)."""
    notifications = []

    if not NOTIFICATIONS_DIR.exists():
        return notifications

    for notif_file in sorted(NOTIFICATIONS_DIR.glob("notif-*.json"), reverse=True):
        try:
            with open(notif_file) as f:
                notif = json.load(f)
            if customer_id is None or notif.get("customer_id") == customer_id:
                notifications.append(notif)
        except (json.JSONDecodeError, OSError):
            pass

    return notifications[:50]  # Last 50 notifications


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "list":
            cust_id = sys.argv[2] if len(sys.argv) > 2 else None
            notifications = get_pending_notifications(cust_id)
            print(json.dumps(notifications, indent=2, default=str))
        elif cmd == "test":
            result = notify_customer_pr_merged(
                customer_id="test-customer",
                pr_number=999,
                pr_title="Test PR",
                task_id="task-test",
                send_slack=False,
            )
            print(json.dumps(result, indent=2, default=str))
        else:
            print("Usage: customer_notifier.py [list [customer_id] | test]")
    else:
        print("Customer Notifier — WS-109-008")
        print("Usage: customer_notifier.py [list [customer_id] | test]")
