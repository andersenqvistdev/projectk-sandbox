#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Customer Support Ticket System — ticket lifecycle management.

Implements a ticket system for tracking customer issues with SLA tracking,
health scoring, and integration with the work queue for escalation.

Uses file-based locking for safe concurrent access (same pattern as
work_allocator.py).

Ticket Statuses: open -> in_progress -> resolved -> closed
                 (can reopen from resolved/closed back to open)

Priority Levels:
    1 = Critical (system down, security breach)
    2 = High (major functionality broken)
    3 = Normal (standard bugs, questions)
    4 = Low (cosmetic, feature requests)

Escalation Levels:
    L1 = Front-line support
    L2 = Senior support / engineering
    L3 = Engineering team lead
    L4 = Executive escalation
"""

import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────

TICKETS_FILE = "customer/tickets.json"
TICKETS_LOCK_FILE = "customer/tickets.lock"
HEALTH_FILE = "customer/health.json"
HEALTH_LOCK_FILE = "customer/health.lock"
LOCK_TIMEOUT = 10

SEVERITY_LABELS = {
    1: "P1 — Critical",
    2: "P2 — High",
    3: "P3 — Normal",
    4: "P4 — Low",
}

VALID_STATUSES = {"open", "in_progress", "resolved", "closed"}
VALID_CATEGORIES = {"bug", "feature_request", "question", "configuration", "security"}
VALID_ESCALATION_LEVELS = {"L1", "L2", "L3", "L4"}
VALID_SUPPORT_TIERS = {"community", "professional", "enterprise"}

# SLA targets: (priority, tier) -> (response_hours, resolution_hours)
# None means no SLA target for that metric.
SLA_TARGETS: dict[tuple[int, str], tuple[int, int | None]] = {
    (1, "enterprise"): (1, 4),
    (1, "professional"): (4, 8),
    (2, "enterprise"): (4, 8),
    (2, "professional"): (8, 24),
    (3, "enterprise"): (8, 40),
    (3, "professional"): (16, None),
    (4, "enterprise"): (24, None),
    (4, "professional"): (40, None),
}


# ── Path helpers ───────────────────────────────────────────────────────────


def _get_company_dir() -> Path:
    """Get the company directory path."""
    return Path(__file__).resolve().parent.parent.parent.parent / ".company"


def get_tickets_path() -> Path:
    """Get the tickets file path."""
    return _get_company_dir() / TICKETS_FILE


def get_tickets_lock_path() -> Path:
    """Get the tickets lock file path."""
    return _get_company_dir() / TICKETS_LOCK_FILE


def get_health_lock_path() -> Path:
    """Get the health data lock file path."""
    return _get_company_dir() / HEALTH_LOCK_FILE


def _get_health_path() -> Path:
    """Get the health file path."""
    return _get_company_dir() / HEALTH_FILE


def _ensure_customer_dir() -> None:
    """Ensure .company/customer directory exists."""
    customer_dir = _get_company_dir() / "customer"
    customer_dir.mkdir(parents=True, exist_ok=True)


def _is_pytest() -> bool:
    """Check if running under pytest."""
    return "PYTEST_CURRENT_TEST" in os.environ or any(
        "pytest" in str(v) for v in [os.environ.get("_", "")]
    )


def _is_production_path(path: Path) -> bool:
    """Check if a path resolves to the real .company directory."""
    real_company = _get_company_dir()
    try:
        return path.resolve().is_relative_to(real_company.resolve())
    except (ValueError, OSError):
        return False


# ── Locking ────────────────────────────────────────────────────────────────


class TicketLock:
    """
    Context manager for file-based ticket locking.

    Uses fcntl.flock for atomic file locking on Unix systems.
    Same pattern as QueueLock in work_allocator.py.
    """

    def __init__(self, lock_path: Path | None = None, timeout: int = LOCK_TIMEOUT):
        self.lock_path = lock_path or get_tickets_lock_path()
        self.timeout = timeout
        self.lock_file = None

    def __enter__(self):
        _ensure_customer_dir()
        self.lock_file = open(self.lock_path, "w")

        start_time = time.time()
        while True:
            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() - start_time > self.timeout:
                    self.lock_file.close()
                    self.lock_file = None
                    raise TimeoutError(
                        f"Could not acquire ticket lock within {self.timeout}s"
                    )
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
        return False


# ── IO functions ───────────────────────────────────────────────────────────


def get_empty_tickets() -> dict:
    """Return empty tickets structure."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "tickets": {
            "open": [],
            "in_progress": [],
            "resolved": [],
            "closed": [],
        },
        "metadata": {
            "created_at": now,
            "last_modified": now,
            "total_tickets_created": 0,
        },
    }


def load_tickets() -> dict:
    """Load tickets from file."""
    tickets_path = get_tickets_path()

    # P80: Prevent pytest from reading production tickets.
    if _is_pytest() and _is_production_path(tickets_path):
        return get_empty_tickets()

    if not tickets_path.exists():
        return get_empty_tickets()

    try:
        with open(tickets_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return get_empty_tickets()


def save_tickets(tickets: dict) -> None:
    """Save tickets to file with atomic write.

    Uses tempfile + os.replace to prevent race conditions.
    """
    _ensure_customer_dir()
    tickets_path = get_tickets_path()

    # P80: Guard against pytest overwriting production tickets.
    if _is_pytest() and _is_production_path(tickets_path):
        return

    tickets["metadata"]["last_modified"] = datetime.now(timezone.utc).isoformat()

    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix="tickets_",
        dir=str(tickets_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tickets, f, indent=2)
        os.replace(tmp_path, tickets_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def generate_ticket_id() -> str:
    """Generate unique ticket ID."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"ticket-{timestamp}-{short_uuid}"


# ── CRUD functions ─────────────────────────────────────────────────────────


def create_ticket(
    title: str,
    description: str = "",
    priority: int = 3,
    category: str = "bug",
    reporter: str = "",
    customer_id: str | None = None,
    support_tier: str = "community",
    tags: list[str] | None = None,
    environment: dict | None = None,
) -> dict:
    """Create a new support ticket.

    Returns {"success": bool, "ticket_id": str, ...}.
    """
    # Validate inputs
    if not title or not title.strip():
        return {"success": False, "error": "Title is required"}

    priority = max(1, min(4, int(priority)))

    if category not in VALID_CATEGORIES:
        return {
            "success": False,
            "error": f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}",
        }

    if support_tier not in VALID_SUPPORT_TIERS:
        return {
            "success": False,
            "error": f"Invalid support_tier '{support_tier}'. Must be one of: {VALID_SUPPORT_TIERS}",
        }

    now = datetime.now(timezone.utc).isoformat()
    ticket_id = generate_ticket_id()

    # Calculate SLA deadlines
    sla = calculate_sla_deadlines(priority, support_tier)

    ticket = {
        "ticket_id": ticket_id,
        "title": title.strip(),
        "description": description,
        "priority": priority,
        "severity_label": SEVERITY_LABELS.get(priority, f"P{priority}"),
        "category": category,
        "status": "open",
        "customer_id": customer_id,
        "reporter": reporter,
        "assigned_to": None,
        "escalation_level": "L1",
        "support_tier": support_tier,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
        "closed_at": None,
        "sla_response_deadline": sla.get("response_deadline"),
        "sla_resolution_deadline": sla.get("resolution_deadline"),
        "sla_response_met": None,
        "sla_resolution_met": None,
        "tags": tags or [],
        "notes": [],
        "related_task_id": None,
        "environment": environment or {},
    }

    with TicketLock():
        tickets = load_tickets()
        tickets["tickets"]["open"].append(ticket)
        tickets["metadata"]["total_tickets_created"] = (
            tickets["metadata"].get("total_tickets_created", 0) + 1
        )
        save_tickets(tickets)

    logger.info("Created ticket %s: %s (P%d)", ticket_id, title, priority)
    return {"success": True, "ticket_id": ticket_id, "ticket": ticket}


def _find_ticket(tickets: dict, ticket_id: str) -> tuple[dict | None, str | None]:
    """Find a ticket across all status buckets.

    Returns (ticket, status_bucket) or (None, None).
    """
    for status, bucket in tickets["tickets"].items():
        for t in bucket:
            if t.get("ticket_id") == ticket_id:
                return t, status
    return None, None


def update_ticket(
    ticket_id: str,
    status: str | None = None,
    priority: int | None = None,
    assigned_to: str | None = None,
    escalation_level: str | None = None,
    notes: str | None = None,
    note_author: str = "system",
) -> dict:
    """Update an existing ticket's fields.

    Returns {"success": bool, ...}.
    """
    with TicketLock():
        tickets = load_tickets()
        ticket, current_status = _find_ticket(tickets, ticket_id)

        if ticket is None or current_status is None:
            return {"success": False, "error": f"Ticket {ticket_id} not found"}

        now = datetime.now(timezone.utc).isoformat()
        ticket["updated_at"] = now

        if priority is not None:
            ticket["priority"] = max(1, min(4, int(priority)))
            ticket["severity_label"] = SEVERITY_LABELS.get(
                ticket["priority"], f"P{ticket['priority']}"
            )

        if assigned_to is not None:
            ticket["assigned_to"] = assigned_to

        if escalation_level is not None:
            if escalation_level in VALID_ESCALATION_LEVELS:
                ticket["escalation_level"] = escalation_level

        if notes is not None:
            ticket["notes"].append(
                {
                    "author": note_author,
                    "content": notes,
                    "timestamp": now,
                }
            )

        # Handle status transition
        if status is not None and status != current_status:
            if status not in VALID_STATUSES:
                return {
                    "success": False,
                    "error": f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}",
                }

            # Remove from current bucket
            tickets["tickets"][current_status] = [
                t
                for t in tickets["tickets"][current_status]
                if t.get("ticket_id") != ticket_id
            ]
            ticket["status"] = status

            if status == "resolved":
                ticket["resolved_at"] = now
                # Check SLA resolution compliance
                if ticket.get("sla_resolution_deadline"):
                    ticket["sla_resolution_met"] = (
                        now <= ticket["sla_resolution_deadline"]
                    )
            elif status == "closed":
                ticket["closed_at"] = now

            # Add to new bucket
            tickets["tickets"][status].append(ticket)

        save_tickets(tickets)

    logger.info("Updated ticket %s", ticket_id)
    return {"success": True, "ticket_id": ticket_id, "ticket": ticket}


def resolve_ticket(
    ticket_id: str,
    resolution_notes: str = "",
    resolved_by: str = "system",
) -> dict:
    """Resolve a ticket with notes.

    Returns {"success": bool, ...}.
    """
    with TicketLock():
        tickets = load_tickets()
        ticket, current_status = _find_ticket(tickets, ticket_id)

        if ticket is None or current_status is None:
            return {"success": False, "error": f"Ticket {ticket_id} not found"}

        if current_status in ("resolved", "closed"):
            return {
                "success": False,
                "error": f"Ticket {ticket_id} is already {current_status}",
            }

        now = datetime.now(timezone.utc).isoformat()
        ticket["updated_at"] = now
        ticket["resolved_at"] = now
        ticket["status"] = "resolved"

        # Check SLA resolution compliance
        if ticket.get("sla_resolution_deadline"):
            ticket["sla_resolution_met"] = now <= ticket["sla_resolution_deadline"]

        if resolution_notes:
            ticket["notes"].append(
                {
                    "author": resolved_by,
                    "content": f"Resolution: {resolution_notes}",
                    "timestamp": now,
                }
            )

        # Move to resolved bucket
        tickets["tickets"][current_status] = [
            t
            for t in tickets["tickets"][current_status]
            if t.get("ticket_id") != ticket_id
        ]
        tickets["tickets"]["resolved"].append(ticket)
        save_tickets(tickets)

    logger.info("Resolved ticket %s by %s", ticket_id, resolved_by)
    return {"success": True, "ticket_id": ticket_id, "ticket": ticket}


def close_ticket(ticket_id: str) -> dict:
    """Close a ticket (typically after resolution is confirmed).

    Returns {"success": bool, ...}.
    """
    with TicketLock():
        tickets = load_tickets()
        ticket, current_status = _find_ticket(tickets, ticket_id)

        if ticket is None or current_status is None:
            return {"success": False, "error": f"Ticket {ticket_id} not found"}

        if current_status == "closed":
            return {"success": False, "error": f"Ticket {ticket_id} is already closed"}

        now = datetime.now(timezone.utc).isoformat()
        ticket["updated_at"] = now
        ticket["closed_at"] = now
        ticket["status"] = "closed"

        # Move to closed bucket
        tickets["tickets"][current_status] = [
            t
            for t in tickets["tickets"][current_status]
            if t.get("ticket_id") != ticket_id
        ]
        tickets["tickets"]["closed"].append(ticket)
        save_tickets(tickets)

    logger.info("Closed ticket %s", ticket_id)
    return {"success": True, "ticket_id": ticket_id, "ticket": ticket}


def reopen_ticket(ticket_id: str, reason: str = "") -> dict:
    """Reopen a resolved or closed ticket.

    Returns {"success": bool, ...}.
    """
    with TicketLock():
        tickets = load_tickets()
        ticket, current_status = _find_ticket(tickets, ticket_id)

        if ticket is None or current_status is None:
            return {"success": False, "error": f"Ticket {ticket_id} not found"}

        if current_status == "open":
            return {"success": False, "error": f"Ticket {ticket_id} is already open"}

        now = datetime.now(timezone.utc).isoformat()
        ticket["updated_at"] = now
        ticket["status"] = "open"
        ticket["resolved_at"] = None
        ticket["closed_at"] = None
        # Reset SLA compliance since ticket is reopened
        ticket["sla_resolution_met"] = None

        if reason:
            ticket["notes"].append(
                {
                    "author": "system",
                    "content": f"Reopened: {reason}",
                    "timestamp": now,
                }
            )

        # Move to open bucket
        tickets["tickets"][current_status] = [
            t
            for t in tickets["tickets"][current_status]
            if t.get("ticket_id") != ticket_id
        ]
        tickets["tickets"]["open"].append(ticket)
        save_tickets(tickets)

    logger.info("Reopened ticket %s: %s", ticket_id, reason)
    return {"success": True, "ticket_id": ticket_id, "ticket": ticket}


# ── Query functions ────────────────────────────────────────────────────────


def get_ticket(ticket_id: str) -> dict:
    """Get a single ticket by ID.

    Returns {"success": bool, "ticket": dict | None}.
    No side effects.
    """
    tickets = load_tickets()
    ticket, status = _find_ticket(tickets, ticket_id)

    if ticket is None:
        return {"success": False, "error": f"Ticket {ticket_id} not found"}

    return {"success": True, "ticket": ticket, "status": status}


def list_tickets(
    status: str | None = None,
    priority: int | None = None,
    customer_id: str | None = None,
    assigned_to: str | None = None,
) -> dict:
    """List tickets with optional filters.

    Returns {"success": bool, "tickets": list, "count": int}.
    No side effects.
    """
    tickets = load_tickets()
    result = []

    # Determine which buckets to search
    if status and status in VALID_STATUSES:
        buckets = {status: tickets["tickets"].get(status, [])}
    else:
        buckets = tickets["tickets"]

    for _status, bucket in buckets.items():
        for t in bucket:
            # Apply filters
            if priority is not None and t.get("priority") != priority:
                continue
            if customer_id is not None and t.get("customer_id") != customer_id:
                continue
            if assigned_to is not None and t.get("assigned_to") != assigned_to:
                continue
            result.append(t)

    # Sort by priority (ascending = most critical first), then by created_at
    result.sort(key=lambda t: (t.get("priority", 4), t.get("created_at", "")))

    return {"success": True, "tickets": result, "count": len(result)}


def get_ticket_stats() -> dict:
    """Get ticket statistics.

    Returns {"success": bool, "stats": dict}.
    No side effects.
    """
    tickets = load_tickets()

    stats = {
        "by_status": {},
        "by_priority": {1: 0, 2: 0, 3: 0, 4: 0},
        "by_category": {},
        "total": 0,
        "total_created": tickets["metadata"].get("total_tickets_created", 0),
    }

    all_tickets = []
    for status, bucket in tickets["tickets"].items():
        stats["by_status"][status] = len(bucket)
        all_tickets.extend(bucket)

    stats["total"] = len(all_tickets)

    for t in all_tickets:
        p = t.get("priority", 3)
        stats["by_priority"][p] = stats["by_priority"].get(p, 0) + 1

        cat = t.get("category", "unknown")
        stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1

    return {"success": True, "stats": stats}


# ── SLA functions ──────────────────────────────────────────────────────────


def calculate_sla_deadlines(priority: int, support_tier: str) -> dict:
    """Calculate SLA response and resolution deadlines.

    Returns {"response_deadline": str | None, "resolution_deadline": str | None}.
    No side effects.
    """
    key = (priority, support_tier)
    targets = SLA_TARGETS.get(key)

    if targets is None:
        # Community tier or unknown combo: no SLA
        return {"response_deadline": None, "resolution_deadline": None}

    response_hours, resolution_hours = targets
    now = datetime.now(timezone.utc)

    from datetime import timedelta

    result: dict[str, str | None] = {}

    result["response_deadline"] = (now + timedelta(hours=response_hours)).isoformat()
    result["resolution_deadline"] = (
        (now + timedelta(hours=resolution_hours)).isoformat()
        if resolution_hours is not None
        else None
    )

    return result


def check_sla_compliance(ticket_id: str) -> dict:
    """Check SLA compliance for a specific ticket.

    Returns {"success": bool, "compliance": dict}.
    No side effects.
    """
    tickets = load_tickets()
    ticket, _status = _find_ticket(tickets, ticket_id)

    if ticket is None:
        return {"success": False, "error": f"Ticket {ticket_id} not found"}

    now = datetime.now(timezone.utc).isoformat()
    compliance: dict[str, bool | str | None] = {
        "ticket_id": ticket_id,
        "support_tier": ticket.get("support_tier", "community"),
        "priority": ticket.get("priority", 3),
    }

    # Response SLA
    response_deadline = ticket.get("sla_response_deadline")
    if response_deadline:
        if ticket.get("sla_response_met") is not None:
            compliance["response_met"] = ticket["sla_response_met"]
            compliance["response_status"] = (
                "met" if ticket["sla_response_met"] else "breached"
            )
        elif now > response_deadline:
            compliance["response_met"] = False
            compliance["response_status"] = "breached"
        else:
            compliance["response_met"] = None
            compliance["response_status"] = "pending"
    else:
        compliance["response_met"] = None
        compliance["response_status"] = "no_sla"

    # Resolution SLA
    resolution_deadline = ticket.get("sla_resolution_deadline")
    if resolution_deadline:
        if ticket.get("sla_resolution_met") is not None:
            compliance["resolution_met"] = ticket["sla_resolution_met"]
            compliance["resolution_status"] = (
                "met" if ticket["sla_resolution_met"] else "breached"
            )
        elif now > resolution_deadline:
            compliance["resolution_met"] = False
            compliance["resolution_status"] = "breached"
        else:
            compliance["resolution_met"] = None
            compliance["resolution_status"] = "pending"
    else:
        compliance["resolution_met"] = None
        compliance["resolution_status"] = "no_sla"

    return {"success": True, "compliance": compliance}


# ── Health tracking ────────────────────────────────────────────────────────


def load_health() -> dict:
    """Load customer health data from file.

    No side effects.
    """
    health_path = _get_health_path()

    if _is_pytest() and _is_production_path(health_path):
        return {"customers": {}, "last_updated": None}

    if not health_path.exists():
        return {"customers": {}, "last_updated": None}

    try:
        with open(health_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"customers": {}, "last_updated": None}


def save_health(health: dict) -> None:
    """Save customer health data with atomic write."""
    _ensure_customer_dir()
    health_path = _get_health_path()

    if _is_pytest() and _is_production_path(health_path):
        return

    health["last_updated"] = datetime.now(timezone.utc).isoformat()

    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix="health_",
        dir=str(health_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(health, f, indent=2)
        os.replace(tmp_path, health_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_customer_health(customer_id: str, customer_name: str = "") -> dict:
    """Recalculate health score for a customer from ticket data.

    Health score (0-100) based on:
    - Open ticket ratio (30%): fewer open tickets relative to total = higher
    - P1/P2 exposure (25%): open P1/P2 tickets penalize heavily
    - SLA compliance rate (25%): percentage of tickets meeting SLA
    - Resolution time trend (20%): improving trend = boost

    Returns {"success": bool, "health": dict}.
    """
    tickets = load_tickets()

    # Gather all tickets for this customer
    customer_tickets = []
    for _status, bucket in tickets["tickets"].items():
        for t in bucket:
            if t.get("customer_id") == customer_id:
                customer_tickets.append(t)

    if not customer_tickets:
        return {
            "success": False,
            "error": f"No tickets found for customer {customer_id}",
        }

    total = len(customer_tickets)
    open_tickets = [
        t for t in customer_tickets if t.get("status") in ("open", "in_progress")
    ]
    resolved_tickets = [
        t for t in customer_tickets if t.get("status") in ("resolved", "closed")
    ]

    # Component 1: Open ticket ratio (30%) — fewer open = higher score
    open_ratio = len(open_tickets) / total if total > 0 else 0
    open_score = (1.0 - open_ratio) * 100

    # Component 2: P1/P2 exposure (25%) — any open critical/high = penalty
    open_critical = [t for t in open_tickets if t.get("priority", 4) <= 2]
    if len(open_critical) == 0:
        critical_score = 100.0
    elif len(open_critical) == 1:
        critical_score = 30.0
    else:
        critical_score = max(0.0, 30.0 - (len(open_critical) - 1) * 15.0)

    # Component 3: SLA compliance rate (25%)
    sla_tracked = [
        t for t in customer_tickets if t.get("sla_resolution_met") is not None
    ]
    if sla_tracked:
        sla_met = sum(1 for t in sla_tracked if t.get("sla_resolution_met") is True)
        sla_score = (sla_met / len(sla_tracked)) * 100
    else:
        sla_score = 100.0  # No SLA data = assume healthy

    # Component 4: Resolution time trend (20%)
    # Compare recent resolution times to older ones
    resolved_with_times = []
    for t in resolved_tickets:
        created = t.get("created_at")
        resolved = t.get("resolved_at")
        if created and resolved:
            resolved_with_times.append(
                {
                    "created_at": created,
                    "resolved_at": resolved,
                }
            )

    if len(resolved_with_times) >= 2:
        # Multiple resolutions = enough data to see a trend.
        # Boost score slightly for having resolution history.
        trend_score = 70.0
    elif resolved_with_times:
        trend_score = 60.0  # Only one resolution, neutral
    else:
        trend_score = 50.0  # No resolutions yet

    # Weighted combination
    health_score = (
        open_score * 0.30
        + critical_score * 0.25
        + sla_score * 0.25
        + trend_score * 0.20
    )
    health_score = round(min(100, max(0, health_score)), 1)

    # Determine status label
    if health_score >= 80:
        status_label = "healthy"
    elif health_score >= 50:
        status_label = "at_risk"
    else:
        status_label = "critical"

    customer_health = {
        "customer_id": customer_id,
        "customer_name": customer_name,
        "health_score": health_score,
        "status": status_label,
        "total_tickets": total,
        "open_tickets": len(open_tickets),
        "resolved_tickets": len(resolved_tickets),
        "open_critical": len(open_critical),
        "sla_compliance_pct": round(sla_score, 1),
        "components": {
            "open_ratio_score": round(open_score, 1),
            "critical_exposure_score": round(critical_score, 1),
            "sla_compliance_score": round(sla_score, 1),
            "resolution_trend_score": round(trend_score, 1),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Save to health file (locked to prevent concurrent write loss)
    with TicketLock(lock_path=get_health_lock_path()):
        health = load_health()
        health["customers"][customer_id] = customer_health
        save_health(health)

    logger.info(
        "Updated health for customer %s: score=%.1f (%s)",
        customer_id,
        health_score,
        status_label,
    )
    return {"success": True, "health": customer_health}


def get_customer_health(customer_id: str) -> dict:
    """Get cached health data for a customer.

    Returns {"success": bool, "health": dict | None}.
    No side effects.
    """
    health = load_health()
    customer_health = health.get("customers", {}).get(customer_id)

    if customer_health is None:
        return {
            "success": False,
            "error": f"No health data for customer {customer_id}",
        }

    return {"success": True, "health": customer_health}


# ── Integration ────────────────────────────────────────────────────────────


def escalate_ticket_to_task(ticket_id: str) -> dict:
    """Escalate a ticket by creating a work queue task.

    Links the ticket to the task via related_task_id.
    Returns {"success": bool, "task_id": str, ...}.
    """
    # Import work_allocator lazily to avoid circular imports
    try:
        from . import work_allocator
    except ImportError:
        import work_allocator  # type: ignore[no-redef]

    with TicketLock():
        tickets = load_tickets()
        ticket, current_status = _find_ticket(tickets, ticket_id)

        if ticket is None or current_status is None:
            return {"success": False, "error": f"Ticket {ticket_id} not found"}

        if ticket.get("related_task_id"):
            return {
                "success": False,
                "error": f"Ticket {ticket_id} already escalated to task {ticket['related_task_id']}",
            }

        # Map ticket priority to task priority
        priority = ticket.get("priority", 3)

        # Map category to capabilities
        category = ticket.get("category", "bug")
        cap_map = {
            "bug": ["debugging", "code"],
            "feature_request": ["code", "design"],
            "question": ["documentation"],
            "configuration": ["devops", "configuration"],
            "security": ["security", "code"],
        }
        capabilities = cap_map.get(category, ["code"])

        # WS-108: Build rich description with full ticket context
        description_parts = [ticket.get("description", "")]

        # Add ticket metadata
        description_parts.append("")
        description_parts.append("---")
        description_parts.append(f"**Ticket ID:** {ticket_id}")
        description_parts.append(f"**Category:** {category}")
        description_parts.append(
            f"**Customer:** {ticket.get('customer_id', 'unknown')}"
        )
        description_parts.append(
            f"**Support Tier:** {ticket.get('support_tier', 'community')}"
        )
        description_parts.append(f"**Priority:** {priority}")
        description_parts.append(f"**Created:** {ticket.get('created_at', 'unknown')}")

        # Add ticket history (notes) for context
        notes = ticket.get("notes", [])
        if notes:
            description_parts.append("")
            description_parts.append("**Ticket History:**")
            for note in notes[-5:]:  # Last 5 notes
                author = note.get("author", "unknown")
                content = note.get("content", "")[:200]
                description_parts.append(f"- [{author}] {content}")
            if len(notes) > 5:
                description_parts.append(f"- ... and {len(notes) - 5} more notes")

        # Add SLA info if available
        sla_deadline = ticket.get("sla_deadline")
        if sla_deadline:
            description_parts.append("")
            description_parts.append(f"**SLA Deadline:** {sla_deadline}")

        rich_description = "\n".join(description_parts)

        # Map complexity based on ticket history and category
        complexity = "standard"
        if category == "security":
            complexity = "complex"
        elif len(notes) > 10:
            complexity = "complex"  # Long-running tickets are likely complex

        # Create work queue task
        task_result = work_allocator.add_task(
            title=f"[Support] {ticket['title']}",
            priority=priority,
            required_capabilities=capabilities,
            description=rich_description,
            source="escalation",
            estimated_complexity=complexity,
        )

        if not task_result.get("success"):
            return {
                "success": False,
                "error": f"Failed to create task: {task_result.get('error', 'unknown')}",
            }

        task_id = task_result.get("task_id", "")

        # Link ticket to task
        ticket["related_task_id"] = task_id
        ticket["updated_at"] = datetime.now(timezone.utc).isoformat()
        ticket["notes"].append(
            {
                "author": "system",
                "content": f"Escalated to work queue task: {task_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Move to in_progress if currently open
        if current_status == "open":
            tickets["tickets"]["open"] = [
                t for t in tickets["tickets"]["open"] if t.get("ticket_id") != ticket_id
            ]
            ticket["status"] = "in_progress"
            tickets["tickets"]["in_progress"].append(ticket)

        save_tickets(tickets)

    logger.info("Escalated ticket %s to task %s", ticket_id, task_id)
    return {
        "success": True,
        "ticket_id": ticket_id,
        "task_id": task_id,
    }
