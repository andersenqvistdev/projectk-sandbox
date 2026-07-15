#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Feedback Monitor — Automated health-based corrective actions.

Implements closed feedback loops: monitors health, detects problems,
and queues corrective tasks automatically.

Rules:
    1. Health Drop Recovery  — score < 60, rate limit 6h
    2. Goal Stall Detection  — unchanged > 24h, rate limit 12h per goal
    3. Queue Starvation      — empty > 1h, rate limit 3h
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
_dashboard_aggregator = None
_goal_tracker = None


def _ensure_dashboard_import():
    """Lazily import dashboard_aggregator."""
    global _dashboard_aggregator
    if _dashboard_aggregator is not None:
        return

    try:
        from . import dashboard_aggregator as da

        _dashboard_aggregator = da
    except ImportError:
        import dashboard_aggregator as da  # type: ignore[no-redef]

        _dashboard_aggregator = da


def _ensure_goal_tracker_import():
    """Lazily import goal_tracker."""
    global _goal_tracker
    if _goal_tracker is not None:
        return

    try:
        from . import goal_tracker as gt

        _goal_tracker = gt
    except ImportError:
        import goal_tracker as gt  # type: ignore[no-redef]

        _goal_tracker = gt


# ---------------------------------------------------------------------------
# FeedbackMonitor
# ---------------------------------------------------------------------------


class FeedbackMonitor:
    """Monitors health metrics and queues corrective tasks when needed."""

    def __init__(self, company_dir: Path):
        self.company_dir = company_dir
        self.state_file = company_dir / "state/feedback_state.json"
        self.state = self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_and_respond(self) -> dict:
        """Main entry point. Check health and take corrective action if needed.

        Returns:
            Dict with ``actions_taken``, ``details``, and ``health_score``.
        """
        actions_taken = 0
        details: list[str] = []

        # Get current health
        health = self._get_health()
        if health is None:
            return {"actions_taken": 0, "error": "Could not get health data"}

        score: int = health.get("health_score", 100)

        # Track health history (rolling 24 entries)
        self._record_health(score)

        # Rule 1: Health Drop Recovery (score < 60, rate limit 6h)
        if score < 60 and not self._is_rate_limited("health-recovery", 6):
            factors = health.get("factors", [])
            worst = (
                min(factors, key=lambda f: f.get("score", 100))
                if factors
                else {"name": "unknown", "score": 0}
            )
            self._queue_corrective_task(
                "health-recovery",
                f"[AUTO] Health Recovery: fix {worst['name']} (score: {worst['score']})",
                (
                    f"Health score dropped to {score}. "
                    f"Worst factor: {worst['name']} at {worst['score']}%. "
                    f"Diagnose root cause and implement fix. "
                    f"Description: {worst.get('description', '')}"
                ),
                priority=90,
            )
            actions_taken += 1
            details.append(f"Queued health recovery for {worst['name']}")

        # Rule 2: Goal Stall Detection (unchanged > 24h, rate limit 12h per goal)
        goal_stalls = self._detect_goal_stalls()
        for stall in goal_stalls:
            action_key = f"goal-unstall-{stall['goal_id']}"
            if not self._is_rate_limited(action_key, 12):
                self._queue_corrective_task(
                    action_key,
                    f"[AUTO] Unstall goal: {stall['goal_name']} (stuck at {stall['progress']}%)",
                    (
                        f"Goal {stall['goal_id']} ({stall['goal_name']}) has been at "
                        f"{stall['progress']}% for over 24 hours. Analyze blockers, "
                        f"generate new tasks, and reallocate priority."
                    ),
                    priority=80,
                )
                actions_taken += 1
                details.append(f"Queued unstall for {stall['goal_id']}")

        # Rule 3: Queue Starvation (empty > 1h, rate limit 3h)
        if self._is_queue_starved() and not self._is_rate_limited("queue-fill", 3):
            self._queue_corrective_task(
                "queue-fill",
                "[AUTO] Queue starvation: generate new work items",
                (
                    "Work queue has been empty for over 1 hour. Run strategic planner "
                    "to identify gaps, check employee ideas for promotable items, "
                    "run self-improvement scan for proposals."
                ),
                priority=70,
            )
            actions_taken += 1
            details.append("Queued queue-fill task")

        self._save_state()
        return {
            "actions_taken": actions_taken,
            "details": details,
            "health_score": score,
        }

    # ------------------------------------------------------------------
    # Health data
    # ------------------------------------------------------------------

    def _get_health(self) -> dict | None:
        """Get current health from dashboard aggregator."""
        try:
            _ensure_dashboard_import()
            return _dashboard_aggregator.aggregate_health(None)  # type: ignore[union-attr]
        except Exception:
            return None

    def _record_health(self, score: int) -> None:
        """Add current health score to rolling history (max 24 entries)."""
        history: list[dict[str, Any]] = self.state.get("health_history", [])
        history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "score": score,
            }
        )
        self.state["health_history"] = history[-24:]

    # ------------------------------------------------------------------
    # Goal stall detection
    # ------------------------------------------------------------------

    def _detect_goal_stalls(self) -> list[dict]:
        """Detect goals whose progress hasn't changed in 24+ hours."""
        stalls: list[dict] = []
        try:
            _ensure_goal_tracker_import()
            gt = _goal_tracker
            if gt is None:
                return stalls

            # Try multiple vision.md locations
            vision_path: Path | None = None
            candidates = [
                self.company_dir / "vision.md",
                self.company_dir.parent / "vision.md",
                Path(__file__).parent.parent.parent.parent / ".company" / "vision.md",
            ]
            for vp in candidates:
                if vp.exists():
                    vision_path = vp
                    break

            if vision_path is None:
                return stalls

            goals = gt.parse_goals_from_vision(str(vision_path))
            goal_snapshots: dict[str, dict] = self.state.get("goal_snapshots", {})
            now = time.time()

            for g in goals:
                if g.id not in gt.GOAL_ASSESSORS:
                    continue
                try:
                    result = gt.GOAL_ASSESSORS[g.id](
                        g, self.company_dir, lightweight=True
                    )
                    current_progress = result.progress_percent

                    snap = goal_snapshots.get(g.id)
                    if snap and snap["progress"] == current_progress:
                        # Same progress — check how long
                        age_hours = (now - snap["timestamp"]) / 3600
                        if age_hours > 24:
                            stalls.append(
                                {
                                    "goal_id": g.id,
                                    "goal_name": g.name,
                                    "progress": current_progress,
                                    "stalled_hours": round(age_hours, 1),
                                }
                            )
                    else:
                        # Progress changed — update snapshot
                        goal_snapshots[g.id] = {
                            "progress": current_progress,
                            "timestamp": now,
                        }
                except Exception:
                    pass

            self.state["goal_snapshots"] = goal_snapshots
        except Exception:
            pass
        return stalls

    # ------------------------------------------------------------------
    # Queue starvation
    # ------------------------------------------------------------------

    def _is_queue_starved(self) -> bool:
        """Check if work queue has been empty for > 1 hour."""
        queue_path = self.company_dir / "state/work_queue.json"
        try:
            with open(queue_path) as f:
                q = json.load(f)
            pending = q.get("pending", [])
            if len(pending) > 0:
                # Queue has items — reset starvation timer
                self.state["queue_empty_since"] = None
                return False

            # Queue is empty
            empty_since = self.state.get("queue_empty_since")
            if empty_since is None:
                self.state["queue_empty_since"] = time.time()
                return False

            return (time.time() - empty_since) > 3600  # > 1 hour
        except (json.JSONDecodeError, OSError):
            return False

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _is_rate_limited(self, action_type: str, hours: float) -> bool:
        """Check if action was taken recently (within *hours* window).

        Uses two mechanisms for resilience:
        1. In-memory state (last_corrective_actions timestamp)
        2. Queue check: if a matching auto-task already exists in pending,
           treat as rate-limited (prevents duplicates if state persistence fails)
        """
        # Check 1: Timestamp-based rate limit
        actions: dict[str, float] = self.state.get("last_corrective_actions", {})
        last = actions.get(action_type)
        if last is not None and (time.time() - last) < (hours * 3600):
            return True

        # Check 2: Queue-based dedup (resilient fallback)
        # If a matching auto-task already exists in pending, skip
        queue_path = self.company_dir / "state/work_queue.json"
        try:
            with open(queue_path) as f:
                queue = json.load(f)
            task_prefix = f"auto-{action_type}-"
            for t in queue.get("pending", []):
                if str(t.get("task_id", "")).startswith(task_prefix):
                    return True
        except (json.JSONDecodeError, OSError):
            pass

        return False

    # ------------------------------------------------------------------
    # Corrective task queuing
    # ------------------------------------------------------------------

    def _queue_corrective_task(
        self,
        action_type: str,
        title: str,
        description: str,
        priority: int,
    ) -> None:
        """Queue a corrective task into work_queue.json (with QueueLock)."""
        queue_path = self.company_dir / "state/work_queue.json"
        lock_path = self.company_dir / "runtime/queue.lock"

        # Import QueueLock from work_allocator
        try:
            import sys

            hooks_dir = str(self.company_dir.parent / "hooks" / "company")
            if hooks_dir not in sys.path:
                sys.path.insert(0, hooks_dir)
            from work_allocator import QueueLock
        except ImportError:
            # Fallback: try parent-relative import
            try:
                parent = Path(__file__).resolve().parent
                if str(parent) not in sys.path:
                    sys.path.insert(0, str(parent))
                from work_allocator import QueueLock
            except ImportError:
                # Last resort: no locking (preserve old behavior)
                QueueLock = None

        # Use QueueLock to prevent concurrent write races (P50 fix)
        if QueueLock is not None:
            lock_ctx = QueueLock(lock_path)
        else:
            from contextlib import nullcontext

            lock_ctx = nullcontext()

        with lock_ctx:
            try:
                with open(queue_path) as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, OSError):
                queue = {
                    "pending": [],
                    "completed": [],
                    "in_progress": [],
                    "blocked": [],
                }

            # Dedup: skip if a similar auto-generated task is already pending
            # This prevents feedback loops (e.g. agent_utilization recovery
            # re-queued every cycle because the metric can't improve)
            task_prefix = f"auto-{action_type}-"
            for t in queue.get("pending", []):
                if str(t.get("task_id", "")).startswith(task_prefix):
                    return  # Already queued, don't duplicate

            task_id = f"auto-{action_type}-{int(time.time())}"
            task: dict[str, Any] = {
                "task_id": task_id,
                "title": title,
                "description": description,
                "priority": priority,
                "status": "pending",
                "source": "feedback_monitor",
                "auto_generated": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "capabilities": ["architecture", "debugging"],
            }

            if "pending" not in queue:
                queue["pending"] = []
            queue["pending"].append(task)

            # Atomic write (tempfile + os.replace)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.company_dir), suffix=".json"
            )
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(queue, f, indent=2, default=str)
                    f.write("\n")
                os.replace(tmp_path, str(queue_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        # Record action timestamp for rate limiting
        if "last_corrective_actions" not in self.state:
            self.state["last_corrective_actions"] = {}
        self.state["last_corrective_actions"][action_type] = time.time()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        """Load feedback state from file."""
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {
                "health_history": [],
                "goal_snapshots": {},
                "last_corrective_actions": {},
                "queue_empty_since": None,
                "last_check": None,
            }

    def _save_state(self) -> None:
        """Save state atomically (tempfile + os.replace)."""
        self.state["last_check"] = datetime.now(timezone.utc).isoformat()
        state_file = str(self.state_file.resolve())
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.company_dir.resolve()), suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
                f.write("\n")
            os.replace(tmp_path, state_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
