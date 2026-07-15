"""Social content draft generator for Forge daemon activity.

Generates short-form drafts (<=280 chars) from company metrics.
Appends to .company/social/content_queue.json with dedup by content hash.
Does NOT post — creates drafts only. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _load_json(path: Path) -> dict | list | None:
    """Load a JSON file, return None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_queue(queue_path: Path, data: dict) -> None:
    """Atomically save queue file (no truncation window)."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(queue_path.parent), suffix=".tmp", prefix=".cq_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(queue_path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


MAX_QUEUE_SIZE = 50

_PLATFORM_HEADING_MAP = {
    "x": "X/Twitter",
    "twitter": "X/Twitter",  # alias for x
    "reddit": "Reddit",
    "linkedin": "LinkedIn",
}


def _content_hash(text: str) -> str:
    """MD5 hex digest of content string for dedup."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _load_voice_guidelines(company_dir: Path, platform: str) -> str:
    """Return the platform section from social/voice.md, or '' if missing."""
    heading = _PLATFORM_HEADING_MAP.get(platform.lower())
    if not heading:
        return ""
    voice_path = company_dir / "social" / "voice.md"
    try:
        content = voice_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    # Extract section between ### <heading> and the next ### or ---
    lines = content.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if line.strip().startswith("###") and heading in line:
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("###") or line.strip() == "---":
                break
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def _get_queue_size(company_dir: Path) -> int:
    """Return number of items currently in the social content queue."""
    queue_path = company_dir / "social" / "content_queue.json"
    data = _load_json(queue_path)
    if not data or not isinstance(data, dict):
        return 0
    return len(data.get("queue", []))


def _discussion_draft(m: dict) -> str | None:
    """Generate a Reddit-style discussion prompt from company metrics."""
    emp = m.get("employee_count", 0)
    days = m.get("uptime_days", 0)
    total = m.get("total_tasks", 0)
    if emp < 1 or total < 10 or days < 2:
        return None
    return (
        f"We've been running {emp} AI employees autonomously for {days} days. "
        f"What would you want to know about fully autonomous AI teams?"
    )


def _article_draft(m: dict) -> str | None:
    """Generate a LinkedIn article draft from company metrics."""
    emp = m.get("employee_count", 0)
    total = m.get("total_tasks", 0)
    name = m.get("company_name", "Forge Labs")
    rate = m.get("success_rate", 0.0)
    if emp < 1 or total < 10:
        return None
    return (
        f"How {name} runs autonomously:\n"
        f"• {emp} AI employees working in parallel\n"
        f"• {total} tasks completed with {rate}% success rate\n"
        f"• Zero human prompts required"
    )


def _gather_metrics(company_dir: Path) -> dict:
    """Read company data files and extract key metrics."""
    metrics: dict = {
        "employee_count": 0,
        "total_tasks": 0,
        "success_rate": 0.0,
        "daemon_cycles": 0,
        "uptime_days": 0,
        "company_name": "Forge Labs",
    }

    # org.json — employees and task counts
    org = _load_json(company_dir / "org.json")
    if org and isinstance(org, dict):
        employees = org.get("employees", [])
        metrics["employee_count"] = len(employees)
        metrics["company_name"] = org.get("company", {}).get("name", "Forge Labs")
        total = 0
        for emp in employees:
            total += emp.get("efficiency", {}).get("tasks_completed", 0)
        metrics["total_tasks"] = total

    # efficiency_data.json — success rate
    eff = _load_json(company_dir / "state/efficiency_data.json")
    if eff and isinstance(eff, dict):
        executions = eff.get("task_executions", [])
        if executions:
            successes = sum(1 for e in executions if e.get("success"))
            metrics["success_rate"] = round(successes / len(executions) * 100)

    # adaptive_scheduler_state.json — daemon cycles
    sched = _load_json(company_dir / "state/adaptive_scheduler_state.json")
    if sched and isinstance(sched, dict):
        metrics["daemon_cycles"] = sched.get("transitions_count", 0)

    # session_state.json — uptime
    session = _load_json(company_dir / "state/session_state.json")
    if session and isinstance(session, dict):
        start_str = session.get("start_time")
        if start_str:
            try:
                start = datetime.fromisoformat(start_str)
                now = datetime.now(timezone.utc)
                metrics["uptime_days"] = max(1, (now - start).days)
            except (ValueError, TypeError):
                metrics["uptime_days"] = 1

    return metrics


def _milestone_draft(m: dict) -> str | None:
    """Generate a milestone draft at every 100-task boundary."""
    total = m["total_tasks"]
    if total < 100:
        return None
    rounded = (total // 100) * 100
    return (
        f"Our AI company just completed its {rounded}th task autonomously. "
        f"Zero human prompts needed."
    )


def _streak_draft(m: dict) -> str | None:
    """Generate a streak/uptime draft."""
    days = m["uptime_days"]
    cycles = m["daemon_cycles"]
    rate = m["success_rate"]
    if cycles < 1:
        return None
    return (
        f"Day {days} of fully autonomous operation. "
        f"Daemon cycle {cycles}. {rate}% success rate."
    )


def _insight_draft(m: dict) -> str | None:
    """Generate a stats-based insight draft."""
    emp = m["employee_count"]
    total = m["total_tasks"]
    if emp < 1 or total < 10:
        return None
    return f"{emp} AI employees. {total} tasks completed. One daemon. No sleep."


def generate_content_drafts(company_dir: Path) -> list[dict]:
    """Generate 1-3 social media content drafts from company metrics.

    Reads company data, produces short drafts (<=280 chars), and appends
    them to .company/social/content_queue.json with dedup by content hash.

    Args:
        company_dir: Path to .company directory.

    Returns:
        List of newly added draft dicts.
    """
    metrics = _gather_metrics(company_dir)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Build candidate drafts from templates
    generators = [
        ("milestone", _milestone_draft),
        ("streak", _streak_draft),
        ("insight", _insight_draft),
    ]

    candidates: list[dict] = []
    for draft_type, gen_fn in generators:
        text = gen_fn(metrics)
        if text is None:
            continue
        # Enforce 280-char limit
        if len(text) > 280:
            text = text[:277] + "..."
        candidates.append(
            {
                "type": draft_type,
                "platform": "x",
                "content": text,
                "generated_at": now_iso,
                "status": "draft",
                "content_hash": _content_hash(text),
            }
        )

    if not candidates:
        return []

    # Load existing queue
    queue_path = company_dir / "social" / "content_queue.json"
    queue_data = _load_json(queue_path)
    if not queue_data or not isinstance(queue_data, dict):
        queue_data = {"$schema": "queue.schema.json", "queue": []}

    existing_queue = queue_data.get("queue", [])

    # Collect existing hashes for dedup — check both content_hash field
    # and id field (schema uses id as unique key)
    existing_hashes: set[str] = set()
    for entry in existing_queue:
        h = entry.get("content_hash")
        if h:
            existing_hashes.add(h)
        # Also hash the text content of existing entries for robustness
        text = entry.get("content", {}).get("text", "")
        if text:
            existing_hashes.add(_content_hash(text))

    # Filter duplicates and convert to queue schema format
    new_drafts: list[dict] = []
    for draft in candidates:
        if draft["content_hash"] in existing_hashes:
            continue
        existing_hashes.add(draft["content_hash"])

        # Convert to queue schema format
        queue_entry = {
            "id": f"q-{draft['content_hash'][:8]}",
            "platform": draft["platform"],
            "action": "post",
            "content": {"text": draft["content"]},
            "status": "pending",
            "priority": 5,
            "created_at": draft["generated_at"],
            "created_by": "social-content-generator",
            "content_hash": draft["content_hash"],
            "_draft_type": draft["type"],
        }
        existing_queue.append(queue_entry)
        new_drafts.append(draft)

        if len(new_drafts) >= 3:
            break

    # Write back (append, not overwrite — we loaded and extended)
    if new_drafts:
        queue_data["queue"] = existing_queue
        _save_queue(queue_path, queue_data)

    return new_drafts


@dataclass
class _ContentResult:
    """Return type for content_generation_executor."""

    success: bool
    message: str
    task_id: str = ""


_CONTENT_TYPE_GENERATORS: dict = {
    "insight": _insight_draft,
    "discussion": _discussion_draft,
    "article": _article_draft,
}


def content_generation_executor(task: object, *, company_dir: Path) -> _ContentResult:
    """Cron executor: generate a single content draft for a given type and platform.

    Args:
        task: ScheduledTask with .id, .platform, and .config dict containing
              "content_type" (insight|discussion|article) and optional "max_chars".
        company_dir: Path to .company directory.

    Returns:
        _ContentResult with success flag and human-readable message.
    """
    content_type: str = getattr(task, "config", {}).get("content_type", "insight")
    platform: str = getattr(task, "platform", "x")
    max_chars: int = getattr(task, "config", {}).get("max_chars", 280) or 280
    task_id: str = getattr(task, "id", "")

    gen_fn = _CONTENT_TYPE_GENERATORS.get(content_type)
    if gen_fn is None:
        return _ContentResult(
            success=False,
            message=f"Unknown content_type: {content_type}",
            task_id=task_id,
        )

    # Queue overflow protection
    if _get_queue_size(company_dir) >= MAX_QUEUE_SIZE:
        return _ContentResult(
            success=True,
            message=f"Skipped: queue at {MAX_QUEUE_SIZE} (max)",
            task_id=task_id,
        )

    metrics = _gather_metrics(company_dir)
    text = gen_fn(metrics)

    if text is None:
        return _ContentResult(
            success=True,
            message=f"No {content_type} draft generated (insufficient metrics)",
            task_id=task_id,
        )

    # Apply character limit
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."

    content_hash = _content_hash(text)

    # Load existing queue for dedup check
    queue_path = company_dir / "social" / "content_queue.json"
    queue_data = _load_json(queue_path)
    if not queue_data or not isinstance(queue_data, dict):
        queue_data = {"$schema": "queue.schema.json", "queue": []}

    existing_queue: list = queue_data.get("queue", [])
    existing_hashes: set = {
        e.get("content_hash") for e in existing_queue if e.get("content_hash")
    }

    if content_hash in existing_hashes:
        return _ContentResult(
            success=True,
            message=f"Skipped duplicate {content_type} for {platform}",
            task_id=task_id,
        )

    # Append new entry
    now_iso = datetime.now(timezone.utc).isoformat()
    queue_entry = {
        "id": f"q-{content_hash[:8]}",
        "platform": platform,
        "action": "post",
        "content": {"text": text},
        "status": "pending",
        "priority": 5,
        "created_at": now_iso,
        "created_by": "content-cron",
        "content_hash": content_hash,
        "_draft_type": content_type,
    }
    existing_queue.append(queue_entry)
    queue_data["queue"] = existing_queue
    _save_queue(queue_path, queue_data)

    return _ContentResult(
        success=True,
        message=f"Generated {content_type} draft for {platform}",
        task_id=task_id,
    )


if __name__ == "__main__":
    import sys

    # Default: look for .company in cwd or parent
    cwd = Path.cwd()
    company_dir = cwd / ".company"
    if not company_dir.is_dir():
        company_dir = cwd.parent / ".company"
    if not company_dir.is_dir():
        print("Error: .company directory not found", file=sys.stderr)
        sys.exit(1)

    drafts = generate_content_drafts(company_dir)
    if not drafts:
        print("No new drafts generated (all duplicates or insufficient data).")
    else:
        print(f"Generated {len(drafts)} new draft(s):")
        for d in drafts:
            print(f"  [{d['type']}] {d['content']}")
            print(
                f"         ({len(d['content'])} chars, hash: {d['content_hash'][:8]})"
            )
