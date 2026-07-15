"""Social content publisher for Forge daemon.

Reads approved drafts from .company/social/content_queue.json and publishes
them to the appropriate platform (X, Reddit). Updates queue entries with
post results (posted_at, post_id, post_url, error, retry_count).

Stdlib only for I/O; social clients are lazily imported.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum retry attempts before marking an entry as permanently failed
MAX_RETRIES = 3


# =============================================================================
# Queue I/O helpers
# =============================================================================


def _load_json(path: Path) -> dict | None:
    """Load a JSON file, return None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_queue(queue_path: Path, data: dict) -> None:
    """Atomically save queue file using rename-on-close."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(queue_path.parent), suffix=".tmp", prefix=".sp_")
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


# =============================================================================
# Platform dispatch
# =============================================================================


def _is_scheduled_ready(entry: dict) -> bool:
    """Return True if the entry has no scheduled_for or its time has passed."""
    scheduled = entry.get("scheduled_for")
    if not scheduled:
        return True
    try:
        scheduled_dt = datetime.fromisoformat(scheduled)
        return datetime.now(timezone.utc) >= scheduled_dt
    except (ValueError, TypeError):
        return True


def _publish_x(entry: dict, company_dir: Path) -> dict[str, Any]:
    """Publish a single entry to X/Twitter."""
    import sys

    hooks_dir = Path(__file__).parent
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))

    from social.x_client import PostContent, XClient  # noqa: PLC0415

    content_data = entry.get("content", {})
    text = content_data.get("text", "")
    media = content_data.get("media") or None
    reply_to = entry.get("reply_to") or content_data.get("reply_to") or None
    thread_texts: list[str] = content_data.get("thread", [])

    client = XClient(company_dir=company_dir)
    if not client.credentials:
        return {
            "success": False,
            "error": "X credentials not configured",
            "post_id": None,
            "post_url": None,
        }
    if not client.authenticate():
        return {
            "success": False,
            "error": "X authentication failed",
            "post_id": None,
            "post_url": None,
        }

    if thread_texts:
        results = client.post_thread(thread_texts, reply_to_tweet_id=reply_to)
        if results and results[-1].success:
            last = results[-1]
            return {
                "success": True,
                "post_id": last.post_id,
                "post_url": last.url,
                "error": None,
            }
        last_error = results[-1].error if results else "Unknown thread error"
        return {
            "success": False,
            "error": last_error,
            "post_id": None,
            "post_url": None,
        }

    post = PostContent(text=text, media=media, reply_to=reply_to)
    result = client.post(post)
    return {
        "success": result.success,
        "post_id": result.post_id,
        "post_url": result.url,
        "error": result.error,
    }


def _publish_reddit(entry: dict, company_dir: Path) -> dict[str, Any]:
    """Publish a single entry to Reddit."""
    import sys

    hooks_dir = Path(__file__).parent
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))

    from social.reddit_client import PostContent, RedditClient  # noqa: PLC0415

    content_data = entry.get("content", {})
    text = content_data.get("text", "")
    title = content_data.get("title") or entry.get("title") or ""
    link = content_data.get("link") or None
    subreddit = entry.get("subreddit") or content_data.get("subreddit") or ""
    reply_to = entry.get("reply_to") or None

    client = RedditClient(company_dir=company_dir)
    if not client.credentials:
        return {
            "success": False,
            "error": "Reddit credentials not configured",
            "post_id": None,
            "post_url": None,
        }
    if not client.authenticate():
        return {
            "success": False,
            "error": "Reddit authentication failed",
            "post_id": None,
            "post_url": None,
        }

    if reply_to:
        result = client.reply(post_id=reply_to, content=text)
        return {
            "success": result.success,
            "post_id": result.post_id,
            "post_url": result.url,
            "error": result.error,
        }

    post = PostContent(text=text, subreddit=subreddit, title=title, link=link)
    result = client.post(post)
    return {
        "success": result.success,
        "post_id": result.post_id,
        "post_url": result.url,
        "error": result.error,
    }


_PLATFORM_HANDLERS: dict[str, Any] = {
    "x": _publish_x,
    "twitter": _publish_x,
    "reddit": _publish_reddit,
}


def _dispatch(entry: dict, company_dir: Path) -> dict[str, Any]:
    """Route an entry to the correct platform handler."""
    platform = entry.get("platform", "").lower()
    handler = _PLATFORM_HANDLERS.get(platform)
    if handler is None:
        return {
            "success": False,
            "error": f"Unsupported platform: {platform!r}",
            "post_id": None,
            "post_url": None,
        }
    try:
        return handler(entry, company_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Unhandled error publishing entry %s: %s", entry.get("id"), exc
        )
        return {
            "success": False,
            "error": f"Internal error: {exc}",
            "post_id": None,
            "post_url": None,
        }


# =============================================================================
# Main publisher
# =============================================================================


def publish_approved(company_dir: Path, *, dry_run: bool = False) -> list[dict]:
    """Publish all approved queue entries to their target platforms.

    Reads .company/social/content_queue.json, publishes every entry whose
    status is 'approved' (and whose scheduled_for has passed), then writes
    the updated queue back atomically.

    Args:
        company_dir: Path to the .company directory.
        dry_run: If True, select candidates but do not post or mutate the queue.

    Returns:
        List of result dicts per processed entry with keys:
            id, platform, success, post_id, post_url, error, dry_run.
    """
    queue_path = company_dir / "social" / "content_queue.json"
    queue_data = _load_json(queue_path)
    if not queue_data or not isinstance(queue_data, dict):
        logger.info("No content queue found at %s", queue_path)
        return []

    queue: list[dict] = queue_data.get("queue", [])
    results: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    mutated = False

    for entry in queue:
        if entry.get("status") != "approved":
            continue
        if not _is_scheduled_ready(entry):
            logger.debug("Entry %s not yet scheduled", entry.get("id"))
            continue

        entry_id = entry.get("id", "<unknown>")
        platform = entry.get("platform", "")

        if dry_run:
            results.append(
                {
                    "id": entry_id,
                    "platform": platform,
                    "success": None,
                    "post_id": None,
                    "post_url": None,
                    "error": None,
                    "dry_run": True,
                }
            )
            continue

        logger.info("Publishing entry %s to %s", entry_id, platform)
        outcome = _dispatch(entry, company_dir)

        if outcome["success"]:
            entry["status"] = "posted"
            entry["posted_at"] = now_iso
            entry["post_id"] = outcome["post_id"]
            entry["post_url"] = outcome["post_url"]
            entry.pop("error", None)
            logger.info("Posted %s -> %s", entry_id, outcome.get("post_url"))
        else:
            retry_count = entry.get("retry_count", 0) + 1
            entry["retry_count"] = retry_count
            entry["error"] = outcome["error"]
            if retry_count >= MAX_RETRIES:
                entry["status"] = "failed"
                logger.warning(
                    "Entry %s failed permanently after %d retries: %s",
                    entry_id,
                    retry_count,
                    outcome["error"],
                )
            else:
                logger.warning(
                    "Entry %s failed (attempt %d/%d): %s",
                    entry_id,
                    retry_count,
                    MAX_RETRIES,
                    outcome["error"],
                )

        mutated = True
        results.append(
            {
                "id": entry_id,
                "platform": platform,
                "success": outcome["success"],
                "post_id": outcome["post_id"],
                "post_url": outcome["post_url"],
                "error": outcome["error"],
                "dry_run": False,
            }
        )

    if mutated:
        queue_data["queue"] = queue
        _save_queue(queue_path, queue_data)

    return results


# =============================================================================
# Cron executor interface
# =============================================================================


def publisher_executor(task: object, *, company_dir: Path) -> object:
    """Cron executor: publish approved social content.

    Compatible with the cron executor interface used throughout this codebase.
    Returns an object with .success and .message attributes.
    """
    from dataclasses import dataclass

    @dataclass
    class _PublisherResult:
        success: bool
        message: str
        task_id: str = ""

    task_id: str = getattr(task, "id", "")
    dry_run: bool = getattr(task, "config", {}).get("dry_run", False)

    try:
        results = publish_approved(company_dir, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return _PublisherResult(
            success=False,
            message=f"Publisher error: {exc}",
            task_id=task_id,
        )

    if not results:
        return _PublisherResult(
            success=True,
            message="No approved entries to publish",
            task_id=task_id,
        )

    posted = sum(1 for r in results if r.get("success") is True)
    failed = sum(1 for r in results if r.get("success") is False)
    dry = sum(1 for r in results if r.get("dry_run"))

    parts: list[str] = []
    if dry:
        parts.append(f"{dry} dry-run")
    if posted:
        parts.append(f"{posted} posted")
    if failed:
        parts.append(f"{failed} failed")

    return _PublisherResult(
        success=failed == 0,
        message=", ".join(parts) if parts else "0 entries processed",
        task_id=task_id,
    )


# =============================================================================
# CLI entry point
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    dry = "--dry-run" in sys.argv

    cwd = Path.cwd()
    company_dir = cwd / ".company"
    if not company_dir.is_dir():
        company_dir = cwd.parent / ".company"
    if not company_dir.is_dir():
        print("Error: .company directory not found", file=sys.stderr)
        sys.exit(1)

    results = publish_approved(company_dir, dry_run=dry)

    if not results:
        print("No approved entries found.")
        sys.exit(0)

    for r in results:
        tag = (
            "[DRY]" if r.get("dry_run") else ("[OK]" if r.get("success") else "[FAIL]")
        )
        print(
            f"  {tag} {r['id']} ({r['platform']}): {r.get('post_url') or r.get('error') or ''}"
        )

    failed = [r for r in results if r.get("success") is False]
    sys.exit(1 if failed else 0)
