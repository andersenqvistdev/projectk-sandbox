#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Improvement Detector — P30 Self-Improvement Loop Component.

Detects capability gaps, performance issues, and improvement opportunities
across the company system. This module powers the self-improvement loop by
identifying where the system needs enhancement.

Gap Types Detected:
    - missing_command: Frequent user patterns not covered by existing commands
    - underperforming_agent: Agents with low success rates or slow performance
    - repeated_escalation: Task types that frequently escalate
    - capability_mismatch: Tasks requiring capabilities no employee has
    - bottleneck: Resource contention or workflow bottlenecks

Usage:
    # Run all detectors and output JSON
    python improvement_detector.py scan

    # Run specific detector
    python improvement_detector.py scan --detector underperforming_agents

    # Get summary only
    python improvement_detector.py summary

    # Module import
    from improvement_detector import run_all_detections, DetectionResult
    results = run_all_detections()
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Gap Type Enum
# -----------------------------------------------------------------------------


class GapType(str, Enum):
    """Types of capability gaps the detector can identify."""

    MISSING_COMMAND = "missing_command"
    UNDERPERFORMING_AGENT = "underperforming_agent"
    REPEATED_ESCALATION = "repeated_escalation"
    CAPABILITY_MISMATCH = "capability_mismatch"
    BOTTLENECK = "bottleneck"


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class DetectionResult:
    """Result from a gap detection analysis.

    Represents a single detected capability gap or improvement opportunity.
    """

    gap_type: str  # GapType value
    severity: int  # 1-5, higher = more severe
    evidence: dict[str, Any]  # Raw data supporting detection
    suggested_action: str  # Human-readable recommendation
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    detection_id: str = ""  # Auto-generated unique ID

    def __post_init__(self):
        """Generate detection_id if not provided."""
        if not self.detection_id:
            # Generate unique ID from gap_type + evidence hash
            evidence_str = json.dumps(self.evidence, sort_keys=True, default=str)
            evidence_hash = hashlib.md5(evidence_str.encode()).hexdigest()[:8]
            self.detection_id = f"det-{self.gap_type[:4]}-{evidence_hash}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "detection_id": self.detection_id,
            "gap_type": self.gap_type,
            "severity": self.severity,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
            "detected_at": self.detected_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DetectionResult:
        """Create from dictionary."""
        return cls(
            gap_type=data["gap_type"],
            severity=data["severity"],
            evidence=data.get("evidence", {}),
            suggested_action=data["suggested_action"],
            detected_at=data.get("detected_at", datetime.now(timezone.utc).isoformat()),
            detection_id=data.get("detection_id", ""),
        )


# -----------------------------------------------------------------------------
# Lazy Imports
# -----------------------------------------------------------------------------

_company_resolver = None
_efficiency_tracker = None
_employee_activator = None


def _ensure_company_resolver():
    """Lazily import company_resolver module."""
    global _company_resolver
    if _company_resolver is not None:
        return _company_resolver

    try:
        from . import company_resolver as cr

        _company_resolver = cr
    except ImportError:
        try:
            import company_resolver as cr  # type: ignore[no-redef]

            _company_resolver = cr
        except ImportError:
            _company_resolver = None

    return _company_resolver


def _ensure_efficiency_tracker():
    """Lazily import efficiency_tracker module."""
    global _efficiency_tracker
    if _efficiency_tracker is not None:
        return _efficiency_tracker

    try:
        from . import efficiency_tracker as et

        _efficiency_tracker = et
    except ImportError:
        try:
            import efficiency_tracker as et  # type: ignore[no-redef]

            _efficiency_tracker = et
        except ImportError:
            _efficiency_tracker = None

    return _efficiency_tracker


def _ensure_employee_activator():
    """Lazily import employee_activator module."""
    global _employee_activator
    if _employee_activator is not None:
        return _employee_activator

    try:
        from . import employee_activator as ea

        _employee_activator = ea
    except ImportError:
        try:
            import employee_activator as ea  # type: ignore[no-redef]

            _employee_activator = ea
        except ImportError:
            _employee_activator = None

    return _employee_activator


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def _get_company_dir() -> Path:
    """Get the company directory path."""
    cr = _ensure_company_resolver()
    if cr:
        return cr.get_company_dir()
    return Path.cwd() / ".company"


def _get_commands_dir() -> Path:
    """Get the commands directory path."""
    return Path(__file__).parent.parent / "commands"


def _get_escalations_dir() -> Path:
    """Get the escalations directory path."""
    return _get_company_dir() / "escalations"


# -----------------------------------------------------------------------------
# Data Loading Utilities
# -----------------------------------------------------------------------------


def _load_json_file(path: Path, default: Any = None) -> Any:
    """Safely load a JSON file with fallback to default."""
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _load_work_queue() -> dict:
    """Load work queue data."""
    return _load_json_file(
        _get_company_dir() / "state/work_queue.json",
        {"pending": [], "in_progress": [], "completed": [], "failed": []},
    )


def _load_efficiency_data() -> dict:
    """Load efficiency tracking data."""
    return _load_json_file(
        _get_company_dir() / "state/efficiency_data.json",
        {"task_executions": [], "capability_gaps": {}},
    )


def _load_org() -> dict:
    """Load organization data."""
    return _load_json_file(
        _get_company_dir() / "org.json", {"employees": [], "company": {}}
    )


def _get_existing_commands() -> set[str]:
    """Get set of existing command names."""
    commands_dir = _get_commands_dir()
    if not commands_dir.exists():
        return set()
    return {f.stem for f in commands_dir.glob("*.md")}


# -----------------------------------------------------------------------------
# Detector: Missing Commands
# -----------------------------------------------------------------------------


def detect_missing_commands(
    error_log_path: Path | None = None,
) -> list[DetectionResult]:
    """Detect potential missing commands based on patterns and errors.

    Compares frequent user request patterns against existing commands
    and checks error logs for "command not found" patterns.

    Args:
        error_log_path: Optional path to error log file

    Returns:
        List of DetectionResult for potential new commands
    """
    results: list[DetectionResult] = []
    existing_commands = _get_existing_commands()

    # Check efficiency data for task patterns that might suggest missing commands
    efficiency_data = _load_efficiency_data()
    task_patterns = efficiency_data.get("task_patterns", [])

    # Look for recurring task types that don't map to commands
    pattern_counts: dict[str, int] = {}
    for pattern in task_patterns:
        pattern_name = pattern.get("pattern", "")
        sample_size = pattern.get("sample_size", 0)
        if pattern_name and sample_size >= 3:
            # Normalize pattern name to potential command name
            potential_cmd = pattern_name.lower().replace(" ", "-").replace("_", "-")
            if potential_cmd not in existing_commands:
                pattern_counts[pattern_name] = sample_size

    # Check work queue for recurring manual task types
    work_queue = _load_work_queue()
    all_tasks = (
        work_queue.get("completed", [])
        + work_queue.get("pending", [])
        + work_queue.get("in_progress", [])
    )

    # Group tasks by title prefix (first 3 words)
    title_groups: dict[str, int] = {}
    for task in all_tasks:
        title = task.get("title", "")
        if title:
            # Extract first few words as potential command pattern
            words = title.split()[:3]
            if len(words) >= 2:
                prefix = " ".join(words).lower()
                title_groups[prefix] = title_groups.get(prefix, 0) + 1

    # Filter to frequently occurring patterns (5+ occurrences)
    for prefix, count in title_groups.items():
        if count >= 5:
            potential_cmd = prefix.replace(" ", "-")
            if potential_cmd not in existing_commands:
                results.append(
                    DetectionResult(
                        gap_type=GapType.MISSING_COMMAND.value,
                        severity=2 if count < 10 else 3,
                        evidence={
                            "pattern": prefix,
                            "occurrence_count": count,
                            "suggested_command_name": potential_cmd,
                            "source": "work_queue_patterns",
                        },
                        suggested_action=(
                            f"Consider creating '/{potential_cmd}' command to automate "
                            f"the recurring '{prefix}' workflow ({count} occurrences)"
                        ),
                    )
                )

    # Check for error log patterns if provided
    if error_log_path and error_log_path.exists():
        try:
            with open(error_log_path, encoding="utf-8") as f:
                content = f.read()
            # Look for "command not found" or similar patterns
            import re

            not_found = re.findall(
                r"(?:command|skill)\s+['\"]?(\w[\w-]+)['\"]?\s+not\s+found",
                content,
                re.IGNORECASE,
            )
            missing_counts: dict[str, int] = {}
            for cmd in not_found:
                missing_counts[cmd] = missing_counts.get(cmd, 0) + 1

            for cmd, count in missing_counts.items():
                if count >= 2 and cmd not in existing_commands:
                    results.append(
                        DetectionResult(
                            gap_type=GapType.MISSING_COMMAND.value,
                            severity=3,
                            evidence={
                                "command_name": cmd,
                                "error_count": count,
                                "source": "error_logs",
                            },
                            suggested_action=(
                                f"Command '/{cmd}' was requested {count} times but not found. "
                                f"Consider implementing it."
                            ),
                        )
                    )
        except OSError:
            pass  # Ignore file read errors

    return results


# -----------------------------------------------------------------------------
# Detector: Underperforming Agents
# -----------------------------------------------------------------------------


def detect_underperforming_agents(
    success_rate_threshold: float = 0.7,
    duration_multiplier_threshold: float = 2.0,
) -> list[DetectionResult]:
    """Detect agents with performance issues.

    Identifies agents with:
    - Success rate below threshold (default 0.7)
    - Average duration > threshold multiplier of median (default 2x)

    Args:
        success_rate_threshold: Minimum acceptable success rate (0-1)
        duration_multiplier_threshold: Max acceptable duration vs median

    Returns:
        List of DetectionResult for underperforming agents
    """
    results: list[DetectionResult] = []

    # Try to use efficiency_tracker for accurate data
    et = _ensure_efficiency_tracker()
    if et:
        try:
            report = et.get_efficiency_report()
            employee_breakdown = report.get("employee_breakdown", [])

            # Calculate median score for comparison
            scores = [
                e.get("score", 0)
                for e in employee_breakdown
                if e.get("tasks_completed", 0) > 0
            ]
            if scores:
                median_score = sorted(scores)[len(scores) // 2]
            else:
                median_score = 1.0

            for emp in employee_breakdown:
                emp_id = emp.get("employee_id", "unknown")
                score = emp.get("score", 0)
                tasks_completed = emp.get("tasks_completed", 0)
                trend = emp.get("trend", "stable")

                # Skip employees with insufficient data
                if tasks_completed < 3:
                    continue

                # Check for underperformance
                issues = []
                severity = 2

                if score < success_rate_threshold * median_score:
                    issues.append(f"efficiency score {score:.2f} below threshold")
                    severity = max(severity, 3)

                if trend == "declining":
                    issues.append("declining performance trend")
                    severity = max(severity, 3)

                if issues:
                    results.append(
                        DetectionResult(
                            gap_type=GapType.UNDERPERFORMING_AGENT.value,
                            severity=severity,
                            evidence={
                                "employee_id": emp_id,
                                "efficiency_score": score,
                                "median_score": median_score,
                                "tasks_completed": tasks_completed,
                                "trend": trend,
                                "issues": issues,
                            },
                            suggested_action=(
                                f"Review agent '{emp_id}' performance: {'; '.join(issues)}. "
                                f"Consider retraining, reassigning, or adjusting capabilities."
                            ),
                        )
                    )
            return results
        except Exception:
            pass  # Fall back to direct data loading

    # Fallback: Load efficiency data directly
    efficiency_data = _load_efficiency_data()
    executions = efficiency_data.get("task_executions", [])

    if not executions:
        return results

    # Group executions by employee
    emp_stats: dict[str, dict[str, Any]] = {}
    for exec_data in executions:
        emp_id = exec_data.get("employee_id", "unknown")
        if emp_id not in emp_stats:
            emp_stats[emp_id] = {
                "total": 0,
                "successful": 0,
                "durations": [],
            }

        emp_stats[emp_id]["total"] += 1
        if exec_data.get("success", False):
            emp_stats[emp_id]["successful"] += 1
        duration = exec_data.get("duration_minutes", 0)
        if duration > 0:
            emp_stats[emp_id]["durations"].append(duration)

    # Calculate overall median duration
    all_durations = [d for stats in emp_stats.values() for d in stats["durations"]]
    if all_durations:
        median_duration = sorted(all_durations)[len(all_durations) // 2]
    else:
        median_duration = 30  # Default 30 min

    # Analyze each employee
    for emp_id, stats in emp_stats.items():
        if stats["total"] < 3:
            continue  # Insufficient data

        success_rate = stats["successful"] / stats["total"]
        avg_duration = (
            sum(stats["durations"]) / len(stats["durations"])
            if stats["durations"]
            else 0
        )

        issues = []
        severity = 2

        if success_rate < success_rate_threshold:
            issues.append(
                f"success rate {success_rate:.0%} below {success_rate_threshold:.0%}"
            )
            severity = max(severity, 3 if success_rate < 0.5 else 2)

        if avg_duration > median_duration * duration_multiplier_threshold:
            issues.append(
                f"avg duration {avg_duration:.0f}min is {avg_duration / median_duration:.1f}x median"
            )
            severity = max(severity, 2)

        if issues:
            results.append(
                DetectionResult(
                    gap_type=GapType.UNDERPERFORMING_AGENT.value,
                    severity=severity,
                    evidence={
                        "employee_id": emp_id,
                        "success_rate": round(success_rate, 2),
                        "success_threshold": success_rate_threshold,
                        "tasks_total": stats["total"],
                        "tasks_successful": stats["successful"],
                        "avg_duration_minutes": round(avg_duration, 1),
                        "median_duration_minutes": round(median_duration, 1),
                        "issues": issues,
                    },
                    suggested_action=(
                        f"Agent '{emp_id}' shows performance issues: {'; '.join(issues)}. "
                        f"Consider capability review or additional training."
                    ),
                )
            )

    return results


# -----------------------------------------------------------------------------
# Detector: Escalation Patterns
# -----------------------------------------------------------------------------


def detect_escalation_patterns(
    escalation_threshold: int = 3,
) -> list[DetectionResult]:
    """Detect task types that frequently escalate.

    Reads escalation history and identifies patterns where certain
    task types or capability requirements lead to repeated escalations.

    Args:
        escalation_threshold: Minimum escalations to flag (default 3)

    Returns:
        List of DetectionResult for escalation patterns
    """
    results: list[DetectionResult] = []
    escalations_dir = _get_escalations_dir()

    if not escalations_dir.exists():
        return results

    # Load all escalation files
    escalation_data: list[dict] = []
    try:
        for esc_file in escalations_dir.glob("*.json"):
            data = _load_json_file(esc_file)
            if data:
                escalation_data.append(data)
    except OSError:
        return results

    if not escalation_data:
        # Also check work queue for retry history as proxy for escalations
        work_queue = _load_work_queue()
        failed_tasks = work_queue.get("failed", [])

        # Group failed tasks by capability requirements
        cap_failures: dict[str, list[dict]] = {}
        for task in failed_tasks:
            caps = tuple(sorted(task.get("required_capabilities", [])))
            caps_key = ",".join(caps) if caps else "none"
            if caps_key not in cap_failures:
                cap_failures[caps_key] = []
            cap_failures[caps_key].append(task)

        for caps_key, tasks in cap_failures.items():
            if len(tasks) >= escalation_threshold:
                results.append(
                    DetectionResult(
                        gap_type=GapType.REPEATED_ESCALATION.value,
                        severity=3 if len(tasks) >= 5 else 2,
                        evidence={
                            "capability_requirements": caps_key.split(",")
                            if caps_key != "none"
                            else [],
                            "failure_count": len(tasks),
                            "task_ids": [t.get("task_id") for t in tasks[:5]],
                            "source": "failed_tasks",
                        },
                        suggested_action=(
                            f"Tasks requiring [{caps_key}] capabilities have failed {len(tasks)} times. "
                            f"Consider hiring an agent with these capabilities or improving existing agents."
                        ),
                    )
                )
        return results

    # Group escalations by reason and capability
    reason_groups: dict[str, list[dict]] = {}
    cap_groups: dict[str, list[dict]] = {}

    for esc in escalation_data:
        reason = esc.get("reason", esc.get("trigger", "unknown"))
        if reason not in reason_groups:
            reason_groups[reason] = []
        reason_groups[reason].append(esc)

        # Extract capabilities if available
        task_caps = esc.get("required_capabilities", [])
        if task_caps:
            caps_key = ",".join(sorted(task_caps))
            if caps_key not in cap_groups:
                cap_groups[caps_key] = []
            cap_groups[caps_key].append(esc)

    # Check for patterns by reason
    for reason, escs in reason_groups.items():
        if len(escs) >= escalation_threshold:
            severity = 3 if len(escs) >= 5 else 2
            if reason in ("capability_mismatch", "repeated_failure"):
                severity = min(severity + 1, 5)

            results.append(
                DetectionResult(
                    gap_type=GapType.REPEATED_ESCALATION.value,
                    severity=severity,
                    evidence={
                        "escalation_reason": reason,
                        "escalation_count": len(escs),
                        "task_ids": [e.get("task_id") for e in escs[:5]],
                        "source": "escalation_logs",
                    },
                    suggested_action=(
                        f"Escalation reason '{reason}' occurred {len(escs)} times. "
                        f"Investigate root cause and implement systemic fix."
                    ),
                )
            )

    # Check for patterns by capability
    for caps_key, escs in cap_groups.items():
        if len(escs) >= escalation_threshold:
            results.append(
                DetectionResult(
                    gap_type=GapType.REPEATED_ESCALATION.value,
                    severity=3,
                    evidence={
                        "capability_requirements": caps_key.split(","),
                        "escalation_count": len(escs),
                        "task_ids": [e.get("task_id") for e in escs[:5]],
                        "source": "escalation_logs",
                    },
                    suggested_action=(
                        f"Tasks requiring [{caps_key}] escalate frequently ({len(escs)} times). "
                        f"Consider adding dedicated agent for these capabilities."
                    ),
                )
            )

    return results


# -----------------------------------------------------------------------------
# Detector: Capability Gaps
# -----------------------------------------------------------------------------


def detect_capability_gaps(
    gap_threshold: int = 3,
) -> list[DetectionResult]:
    """Detect missing capabilities based on routing gaps.

    Uses capability gap data from employee_activator to identify
    capabilities that are frequently needed but not available.

    Uses capability_gap_validator (P33) to:
    - Filter evidence to only gaps where the specific capability was missing
    - Skip detection when capability already exists in an employee
    - Indicate expansion vs hiring recommendation

    Args:
        gap_threshold: Minimum gap count to flag (default 3)

    Returns:
        List of DetectionResult for capability gaps
    """
    results: list[DetectionResult] = []

    # Try to import capability_gap_validator (P33)
    validator = None
    try:
        import importlib.util
        from pathlib import Path

        validator_path = Path(__file__).parent / "capability_gap_validator.py"
        if validator_path.exists():
            spec = importlib.util.spec_from_file_location(
                "capability_gap_validator", validator_path
            )
            if spec and spec.loader:
                validator = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(validator)
    except Exception:
        pass  # Validator not available, use fallback

    # Try to use employee_activator for accurate data
    ea = _ensure_employee_activator()
    if ea:
        try:
            gap_summary = ea.get_capability_gap_summary()
            recommendations = gap_summary.get("recommendations", [])

            for rec in recommendations:
                cap = rec.get("capability", "unknown")
                gap_count = rec.get("gap_count", 0)

                if gap_count >= gap_threshold:
                    # P33: Validate before creating detection
                    if validator:
                        validation = validator.validate_capability_proposal(
                            capability=cap,
                            gap_summary=gap_summary,
                            min_gap_count=gap_threshold,
                        )

                        if not validation.is_valid:
                            # Skip invalid detections
                            validator.log_auto_rejection(
                                cap, validation.auto_reject_reason or "Unknown reason"
                            )
                            continue

                        # Use validated data
                        actual_gap_count = validation.actual_gap_count
                        filtered_gaps = validation.filtered_gaps[-5:]
                        recommended_action = validation.recommended_action

                        # Determine action string
                        if (
                            recommended_action
                            == validator.RecommendedAction.EXPAND_EXISTING
                        ):
                            action = (
                                f"Capability '{cap}' handled by '{validation.expansion_candidate}' "
                                f"({validation.expansion_success_rate:.0%} concentration). "
                                f"Recommend adding '{cap}' to their capabilities."
                            )
                        else:
                            action = (
                                f"Capability '{cap}' is needed but missing ({actual_gap_count} gaps). "
                                f"Hire an agent with this capability."
                            )
                    else:
                        # Fallback: use unvalidated data
                        actual_gap_count = gap_count
                        filtered_gaps = gap_summary.get("gaps", [])[-5:]
                        action = (
                            f"Capability '{cap}' is needed but missing ({gap_count} gaps). "
                            f"Hire an agent with this capability or expand existing agent skills."
                        )

                    severity = (
                        2
                        if actual_gap_count < 5
                        else (3 if actual_gap_count < 10 else 4)
                    )
                    results.append(
                        DetectionResult(
                            gap_type=GapType.CAPABILITY_MISMATCH.value,
                            severity=severity,
                            evidence={
                                "capability": cap,
                                "gap_count": actual_gap_count,
                                "recent_gaps": filtered_gaps,  # P33: Now filtered!
                                "source": "employee_activator",
                            },
                            suggested_action=action,
                        )
                    )
            return results
        except Exception:
            pass  # Fall back to direct data loading

    # Fallback: Load efficiency data directly for capability_gaps
    efficiency_data = _load_efficiency_data()
    capability_gaps = efficiency_data.get("capability_gaps", {})
    summary = capability_gaps.get("summary", {})

    for cap, count in summary.items():
        if count >= gap_threshold:
            severity = 2 if count < 5 else (3 if count < 10 else 4)
            results.append(
                DetectionResult(
                    gap_type=GapType.CAPABILITY_MISMATCH.value,
                    severity=severity,
                    evidence={
                        "capability": cap,
                        "gap_count": count,
                        "source": "efficiency_data",
                    },
                    suggested_action=(
                        f"Capability '{cap}' has been needed {count} times with no matching agent. "
                        f"Consider hiring a specialist."
                    ),
                )
            )

    return results


# -----------------------------------------------------------------------------
# Detector: Bottlenecks
# -----------------------------------------------------------------------------


def detect_bottlenecks() -> list[DetectionResult]:
    """Detect workflow bottlenecks and resource contention.

    Identifies:
    - Employees with excessive workload (many in-progress tasks)
    - Queue backlog patterns
    - Frequently blocked tasks

    Returns:
        List of DetectionResult for bottlenecks
    """
    results: list[DetectionResult] = []
    work_queue = _load_work_queue()
    org = _load_org()

    # Check for overloaded employees
    in_progress = work_queue.get("in_progress", [])
    emp_load: dict[str, int] = {}
    for task in in_progress:
        assigned = task.get("assigned_to") or task.get("claimed_by")
        if assigned:
            emp_load[assigned] = emp_load.get(assigned, 0) + 1

    for emp_id, load in emp_load.items():
        if load >= 3:  # 3+ concurrent tasks is concerning
            results.append(
                DetectionResult(
                    gap_type=GapType.BOTTLENECK.value,
                    severity=3 if load >= 5 else 2,
                    evidence={
                        "employee_id": emp_id,
                        "concurrent_tasks": load,
                        "bottleneck_type": "overloaded_employee",
                    },
                    suggested_action=(
                        f"Employee '{emp_id}' has {load} concurrent tasks. "
                        f"Consider redistributing workload or hiring additional agents."
                    ),
                )
            )

    # Check for queue backlog
    pending = work_queue.get("pending", [])
    if len(pending) >= 10:
        # Group by priority
        high_priority = [t for t in pending if t.get("priority", 3) <= 2]
        severity = 3 if len(high_priority) >= 5 else 2

        results.append(
            DetectionResult(
                gap_type=GapType.BOTTLENECK.value,
                severity=severity,
                evidence={
                    "pending_count": len(pending),
                    "high_priority_count": len(high_priority),
                    "bottleneck_type": "queue_backlog",
                },
                suggested_action=(
                    f"Work queue has {len(pending)} pending tasks ({len(high_priority)} high priority). "
                    f"Increase agent capacity or prioritize work."
                ),
            )
        )

    # Check for blocked tasks
    blocked = work_queue.get("blocked", [])
    if len(blocked) >= 3:
        results.append(
            DetectionResult(
                gap_type=GapType.BOTTLENECK.value,
                severity=3,
                evidence={
                    "blocked_count": len(blocked),
                    "task_ids": [t.get("task_id") for t in blocked[:5]],
                    "bottleneck_type": "blocked_tasks",
                },
                suggested_action=(
                    f"{len(blocked)} tasks are blocked. "
                    f"Review blockers and resolve dependencies."
                ),
            )
        )

    # Check for employees with no recent activity (potential idle capacity)
    employees = org.get("employees", [])
    active_employees = set(emp_load.keys())
    idle_employees = [
        e.get("id")
        for e in employees
        if e.get("id") not in active_employees and e.get("status") == "active"
    ]

    if len(pending) >= 5 and len(idle_employees) >= 2:
        results.append(
            DetectionResult(
                gap_type=GapType.BOTTLENECK.value,
                severity=2,
                evidence={
                    "pending_tasks": len(pending),
                    "idle_employees": idle_employees[:5],
                    "bottleneck_type": "assignment_mismatch",
                },
                suggested_action=(
                    f"{len(idle_employees)} employees appear idle while {len(pending)} tasks pending. "
                    f"Review task routing and capability matching."
                ),
            )
        )

    return results


# -----------------------------------------------------------------------------
# Aggregator
# -----------------------------------------------------------------------------


def run_all_detections(
    detectors: list[str] | None = None,
) -> list[DetectionResult]:
    """Run all detectors and aggregate results.

    Runs all enabled detectors, deduplicates results, and returns
    a sorted list by severity (highest first).

    Args:
        detectors: Optional list of specific detectors to run.
                   Valid values: "missing_commands", "underperforming_agents",
                   "escalation_patterns", "capability_gaps", "bottlenecks"
                   If None, runs all detectors.

    Returns:
        List of DetectionResult sorted by severity descending
    """
    all_results: list[DetectionResult] = []

    detector_map = {
        "missing_commands": detect_missing_commands,
        "underperforming_agents": detect_underperforming_agents,
        "escalation_patterns": detect_escalation_patterns,
        "capability_gaps": detect_capability_gaps,
        "bottlenecks": detect_bottlenecks,
    }

    # Determine which detectors to run
    if detectors is None:
        detectors = list(detector_map.keys())

    # Run each detector
    for name in detectors:
        if name in detector_map:
            try:
                results = detector_map[name]()
                all_results.extend(results)
            except Exception as e:
                # Log error but continue with other detectors
                print(f"Warning: Detector '{name}' failed: {e}", file=sys.stderr)

    # Deduplicate by detection_id (gap_type + evidence hash)
    seen_ids: set[str] = set()
    unique_results: list[DetectionResult] = []
    for result in all_results:
        if result.detection_id not in seen_ids:
            seen_ids.add(result.detection_id)
            unique_results.append(result)

    # Sort by severity (highest first), then by gap_type for consistency
    unique_results.sort(key=lambda r: (-r.severity, r.gap_type))

    return unique_results


def get_detection_summary() -> dict[str, Any]:
    """Get a summary of all detections.

    Returns:
        Dict with detection summary including counts by type and severity
    """
    results = run_all_detections()

    # Count by gap type
    by_type: dict[str, int] = {}
    for r in results:
        by_type[r.gap_type] = by_type.get(r.gap_type, 0) + 1

    # Count by severity
    by_severity: dict[int, int] = {}
    for r in results:
        by_severity[r.severity] = by_severity.get(r.severity, 0) + 1

    return {
        "total_detections": len(results),
        "by_gap_type": by_type,
        "by_severity": by_severity,
        "highest_severity": max((r.severity for r in results), default=0),
        "detections": [r.to_dict() for r in results],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> None:
    """Run detection scan and output results."""
    detectors = [args.detector] if args.detector else None
    results = run_all_detections(detectors=detectors)

    if args.json:
        output = {
            "detections": [r.to_dict() for r in results],
            "count": len(results),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(output, indent=2))
    else:
        if not results:
            print("No improvement opportunities detected.")
            return

        print(f"Detected {len(results)} improvement opportunities:\n")
        for i, r in enumerate(results, 1):
            severity_marker = "!" * r.severity
            print(f"{i}. [{r.gap_type}] {severity_marker}")
            print(f"   {r.suggested_action}")
            print(f"   Evidence: {json.dumps(r.evidence, default=str)[:100]}...")
            print()


def cmd_summary(args: argparse.Namespace) -> None:
    """Output detection summary."""
    summary = get_detection_summary()

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("Improvement Detection Summary")
        print("=" * 40)
        print(f"Total detections: {summary['total_detections']}")
        print(f"Highest severity: {summary['highest_severity']}")
        print()
        print("By gap type:")
        for gap_type, count in summary["by_gap_type"].items():
            print(f"  {gap_type}: {count}")
        print()
        print("By severity:")
        for sev, count in sorted(summary["by_severity"].items(), reverse=True):
            print(f"  Level {sev}: {count}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Improvement Detector - Detect capability gaps and improvement opportunities"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Run detection scan")
    scan_parser.add_argument(
        "--detector",
        choices=[
            "missing_commands",
            "underperforming_agents",
            "escalation_patterns",
            "capability_gaps",
            "bottlenecks",
        ],
        help="Run specific detector only",
    )
    scan_parser.add_argument("--json", action="store_true", help="Output as JSON")
    scan_parser.set_defaults(func=cmd_scan)

    # summary command
    summary_parser = subparsers.add_parser("summary", help="Show detection summary")
    summary_parser.add_argument("--json", action="store_true", help="Output as JSON")
    summary_parser.set_defaults(func=cmd_summary)

    args = parser.parse_args()

    if hasattr(args, "func"):
        args.func(args)
    else:
        # Default: run scan with JSON output
        args.json = True
        args.detector = None
        cmd_scan(args)


if __name__ == "__main__":
    main()
