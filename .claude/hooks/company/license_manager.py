"""
License Manager — lifecycle tracking and follow-up email automation.

Scans .company/licenses/ for issued licenses, classifies them by state
(trial_active, trial_expired, paid), and determines which follow-up email
touchpoints are due based on days elapsed since issuance.

Usage:
  uv run .claude/hooks/company/license_manager.py scan
  uv run .claude/hooks/company/license_manager.py process
  uv run .claude/hooks/company/license_manager.py status
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Trial follow-up schedule: (day_offset, touchpoint_name)
TRIAL_TOUCHPOINTS: list[tuple[int, str]] = [
    (0, "trial-welcome"),
    (3, "trial-day3"),
    (7, "trial-day7"),
    (12, "trial-day12"),
    (14, "trial-day14"),
]

PAID_TOUCHPOINTS: list[tuple[int, str]] = [
    (0, "paid-onboarding"),
]

# Project root: payments -> company -> hooks -> .claude -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LICENSE_DIR = PROJECT_ROOT / ".company" / "licenses"
PENDING_EMAILS_PATH = PROJECT_ROOT / ".company" / "state" / "pending_emails.json"
EMAIL_TEMPLATES_DIR = PROJECT_ROOT / "docs" / "sales" / "email-templates"


@dataclass
class LicenseMeta:
    org_id: str
    customer_email: str
    tier: str
    stripe_session_id: str
    touchpoints_sent: list[str]
    created_at: datetime
    is_trial: bool
    is_expired: bool


@dataclass
class PendingAction:
    org_id: str
    org_name: str
    customer_email: str
    tier: str
    touchpoint: str
    reason: str = field(default="")


def load_license_json(license_path: Path) -> Optional[dict]:
    """Load the license JSON file, returning None on any error."""
    try:
        with open(license_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def load_license_meta(meta_path: Path) -> Optional[dict]:
    """Load the .meta.json companion file, returning None on any error."""
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def classify_license(
    license_data: dict,
    meta: dict,
    now: datetime,
) -> Optional[LicenseMeta]:
    """Classify a license and return its metadata, or None if unreadable."""
    org_id = license_data.get("org_id") or meta.get("org_id", "")
    customer_email = meta.get("customer_email", "")
    tier = license_data.get("tier", "")
    stripe_session_id = meta.get("stripe_session_id", "")
    touchpoints_sent: list[str] = meta.get("touchpoints_sent", [])

    created_at_str = meta.get("created_at", "")
    try:
        created_at = datetime.fromisoformat(created_at_str)
    except (ValueError, TypeError):
        return None

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    valid_until = license_data.get("valid_until", "perpetual")
    is_trial = valid_until.lower() != "perpetual"
    is_expired = False

    if is_trial:
        try:
            expiry = datetime.fromisoformat(valid_until)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            is_expired = now > expiry
        except (ValueError, TypeError):
            is_expired = False

    return LicenseMeta(
        org_id=org_id,
        customer_email=customer_email,
        tier=tier,
        stripe_session_id=stripe_session_id,
        touchpoints_sent=touchpoints_sent,
        created_at=created_at,
        is_trial=is_trial,
        is_expired=is_expired,
    )


def scan_licenses(
    license_dir: Path = LICENSE_DIR,
    now: Optional[datetime] = None,
) -> list[LicenseMeta]:
    """Scan the license directory and return classified license metadata."""
    if now is None:
        now = datetime.now(timezone.utc)

    if not license_dir.exists():
        return []

    results = []
    for license_path in sorted(license_dir.glob("*.json")):
        if license_path.name.endswith(".meta.json"):
            continue

        meta_path = license_path.parent / f"{license_path.stem}.meta.json"
        license_data = load_license_json(license_path)
        if license_data is None:
            continue

        meta_data = load_license_meta(meta_path) if meta_path.exists() else {}
        if meta_data is None:
            meta_data = {}

        classified = classify_license(license_data, meta_data, now)
        if classified is not None:
            results.append(classified)

    return results


def build_actions(
    licenses: list[LicenseMeta],
    now: Optional[datetime] = None,
) -> list[PendingAction]:
    """Determine which email touchpoints are due for each license."""
    if now is None:
        now = datetime.now(timezone.utc)

    actions = []
    for lic in licenses:
        if lic.is_expired:
            schedule: list[tuple[int, str]] = []
        elif lic.is_trial:
            schedule = TRIAL_TOUCHPOINTS
        else:
            schedule = PAID_TOUCHPOINTS

        created_at = lic.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        elapsed_days = (now - created_at).days
        org_name = (
            lic.customer_email.split("@")[0].replace(".", " ").title()
            if lic.customer_email
            else lic.org_id
        )

        for day_offset, touchpoint in schedule:
            if elapsed_days >= day_offset and touchpoint not in lic.touchpoints_sent:
                actions.append(
                    PendingAction(
                        org_id=lic.org_id,
                        org_name=org_name,
                        customer_email=lic.customer_email,
                        tier=lic.tier,
                        touchpoint=touchpoint,
                        reason=f"day {elapsed_days} >= threshold {day_offset}",
                    )
                )

    return actions


def enqueue_pending_email(
    action: PendingAction,
    pending_path: Path = PENDING_EMAILS_PATH,
    now: Optional[datetime] = None,
) -> None:
    """Append a pending email action to pending_emails.json."""
    if now is None:
        now = datetime.now(timezone.utc)

    pending_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "org_id": action.org_id,
        "org_name": action.org_name,
        "customer_email": action.customer_email,
        "tier": action.tier,
        "touchpoint": action.touchpoint,
        "queued_at": now.isoformat(),
    }

    existing: list = []
    if pending_path.exists():
        try:
            with open(pending_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []

    existing.append(entry)
    with open(pending_path, "w") as f:
        json.dump(existing, f, indent=2)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    now = datetime.now(timezone.utc)
    licenses = scan_licenses(now=now)

    if command == "scan":
        print(f"Found {len(licenses)} license(s):")
        for lic in licenses:
            if lic.is_expired:
                status = "trial_expired"
            elif lic.is_trial:
                status = "trial_active"
            else:
                status = "paid"
            print(f"  {lic.org_id}: {status} (tier={lic.tier})")

    elif command == "process":
        actions = build_actions(licenses, now=now)
        print(f"Processing {len(actions)} pending action(s):")
        for action in actions:
            print(f"  Enqueuing {action.touchpoint} for {action.org_id}")
            enqueue_pending_email(action, now=now)
        print("Done.")

    elif command == "status":
        trial_active = sum(1 for lic in licenses if lic.is_trial and not lic.is_expired)
        trial_expired = sum(1 for lic in licenses if lic.is_trial and lic.is_expired)
        paid = sum(1 for lic in licenses if not lic.is_trial)
        actions = build_actions(licenses, now=now)
        print("License Status:")
        print(f"  Trial (active):  {trial_active}")
        print(f"  Trial (expired): {trial_expired}")
        print(f"  Paid:            {paid}")
        print(f"  Pending actions: {len(actions)}")

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Usage: license_manager.py [scan|process|status]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
