#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Self-Improvement Loop Orchestrator — P30 Component.

Orchestrates the full improvement cycle: detection -> proposal -> submission.
This module ties together improvement_detector, capability_proposer, and
initiative_engine into a cohesive self-improvement pipeline.

Cycle Flow:
    1. Run improvement detections (via improvement_detector)
    2. Generate proposals from detections (via capability_proposer)
    3. Convert proposals to initiative format
    4. Submit via initiative_engine (respecting approval tiers)
    5. Log cycle results to .company/improvement_cycles.json

Usage:
    # Run improvement cycle
    python self_improvement_loop.py run

    # Preview without side effects
    python self_improvement_loop.py run --dry-run

    # Check improvement status
    python self_improvement_loop.py status

    # View cycle history
    python self_improvement_loop.py history
    python self_improvement_loop.py history --days 7

    # Module import
    from self_improvement_loop import run_improvement_cycle, ImprovementCycleResult
    result = run_improvement_cycle(dry_run=True)
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# =============================================================================
# Configuration
# =============================================================================

CYCLES_FILE = "improvement_cycles.json"
DEFAULT_CYCLE_INTERVAL_DAYS = 7
MAX_PENDING_PROPOSALS = 5

# =============================================================================
# Lazy Imports
# =============================================================================

_company_resolver = None
_improvement_detector = None
_capability_proposer = None
_initiative_engine = None


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


def _ensure_improvement_detector():
    """Lazily import improvement_detector module."""
    global _improvement_detector
    if _improvement_detector is not None:
        return _improvement_detector

    try:
        from . import improvement_detector as id_mod

        _improvement_detector = id_mod
    except ImportError:
        try:
            import improvement_detector as id_mod  # type: ignore[no-redef]

            _improvement_detector = id_mod
        except ImportError:
            _improvement_detector = None

    return _improvement_detector


def _ensure_capability_proposer():
    """Lazily import capability_proposer module."""
    global _capability_proposer
    if _capability_proposer is not None:
        return _capability_proposer

    try:
        from . import capability_proposer as cp

        _capability_proposer = cp
    except ImportError:
        try:
            import capability_proposer as cp  # type: ignore[no-redef]

            _capability_proposer = cp
        except ImportError:
            _capability_proposer = None

    return _capability_proposer


def _ensure_initiative_engine():
    """Lazily import initiative_engine module."""
    global _initiative_engine
    if _initiative_engine is not None:
        return _initiative_engine

    try:
        from . import initiative_engine as ie

        _initiative_engine = ie
    except ImportError:
        try:
            import initiative_engine as ie  # type: ignore[no-redef]

            _initiative_engine = ie
        except ImportError:
            _initiative_engine = None

    return _initiative_engine


# =============================================================================
# Path Utilities
# =============================================================================


def _get_company_dir() -> Path:
    """Get the company directory path."""
    cr = _ensure_company_resolver()
    if cr:
        return cr.get_company_dir()
    return Path.cwd() / ".company"


def _get_cycles_path() -> Path:
    """Get the improvement cycles file path."""
    return _get_company_dir() / CYCLES_FILE


def _ensure_company_dir() -> None:
    """Ensure company directory exists."""
    company_dir = _get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ImprovementCycleResult:
    """Result from a complete improvement cycle execution.

    Attributes:
        cycle_id: Unique identifier for this cycle
        timestamp: ISO timestamp when cycle was executed
        detections_count: Number of improvement opportunities detected
        proposals_generated: Number of proposals generated from detections
        proposals_submitted: Number of proposals submitted to initiative engine
        proposals_auto_approved: Number of proposals auto-approved
        proposals_pending_human: Number of proposals awaiting human approval
        errors: List of error messages encountered during cycle
    """

    cycle_id: str
    timestamp: str
    detections_count: int
    proposals_generated: int
    proposals_submitted: int
    proposals_auto_approved: int
    proposals_pending_human: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "cycle_id": self.cycle_id,
            "timestamp": self.timestamp,
            "detections_count": self.detections_count,
            "proposals_generated": self.proposals_generated,
            "proposals_submitted": self.proposals_submitted,
            "proposals_auto_approved": self.proposals_auto_approved,
            "proposals_pending_human": self.proposals_pending_human,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImprovementCycleResult:
        """Create from dictionary."""
        return cls(
            cycle_id=data["cycle_id"],
            timestamp=data["timestamp"],
            detections_count=data.get("detections_count", 0),
            proposals_generated=data.get("proposals_generated", 0),
            proposals_submitted=data.get("proposals_submitted", 0),
            proposals_auto_approved=data.get("proposals_auto_approved", 0),
            proposals_pending_human=data.get("proposals_pending_human", 0),
            errors=data.get("errors", []),
        )


# =============================================================================
# Cycle ID Generation
# =============================================================================


def _generate_cycle_id() -> str:
    """Generate a unique cycle ID.

    Format: cycle-{timestamp}-{hash}
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    hash_input = f"{timestamp}{id(object())}"
    hash_suffix = hashlib.md5(hash_input.encode()).hexdigest()[:6]
    return f"cycle-{timestamp}-{hash_suffix}"


# =============================================================================
# Cycle Storage
# =============================================================================


def _load_cycles() -> list[dict[str, Any]]:
    """Load stored improvement cycles."""
    path = _get_cycles_path()

    if not path.exists():
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("cycles", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_cycle(result: ImprovementCycleResult) -> None:
    """Save a cycle result to the cycles file."""
    _ensure_company_dir()
    path = _get_cycles_path()

    # Load existing cycles
    cycles = _load_cycles()

    # Add new cycle
    cycles.append(result.to_dict())

    # Keep only last 100 cycles to prevent unbounded growth
    cycles = cycles[-100:]

    data = {
        "cycles": cycles,
        "metadata": {
            "count": len(cycles),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        },
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# =============================================================================
# Pending Proposals Check
# =============================================================================


def _count_pending_proposals() -> int:
    """Count the number of pending proposals awaiting human approval."""
    pending_path = _get_company_dir() / "state/pending_approvals.json"

    if not pending_path.exists():
        return 0

    try:
        with open(pending_path, encoding="utf-8") as f:
            data = json.load(f)
            return len(data.get("proposals", []))
    except (json.JSONDecodeError, OSError):
        return 0


# =============================================================================
# Main Functions
# =============================================================================


def run_improvement_cycle(dry_run: bool = False) -> ImprovementCycleResult:
    """Run a complete improvement cycle.

    Executes the full detection -> proposal -> submit flow:
    1. Run all improvement detections
    2. Generate proposals from detections
    3. Convert each proposal to initiative format
    4. Submit via initiative_engine (respecting dry_run flag)
    5. Log cycle results

    Args:
        dry_run: If True, preview without side effects (no submissions)

    Returns:
        ImprovementCycleResult with aggregated results
    """
    errors: list[str] = []
    cycle_id = _generate_cycle_id()
    timestamp = datetime.now(timezone.utc).isoformat()

    detections_count = 0
    proposals_generated = 0
    proposals_submitted = 0
    proposals_auto_approved = 0
    proposals_pending_human = 0

    # Step 1: Run improvement detections
    detector = _ensure_improvement_detector()
    if detector is None:
        errors.append("Failed to load improvement_detector module")
        result = ImprovementCycleResult(
            cycle_id=cycle_id,
            timestamp=timestamp,
            detections_count=0,
            proposals_generated=0,
            proposals_submitted=0,
            proposals_auto_approved=0,
            proposals_pending_human=0,
            errors=errors,
        )
        if not dry_run:
            _save_cycle(result)
        return result

    try:
        detections = detector.run_all_detections()
        detections_count = len(detections)
    except Exception as e:
        errors.append(f"Detection failed: {e}")
        detections = []

    if not detections:
        result = ImprovementCycleResult(
            cycle_id=cycle_id,
            timestamp=timestamp,
            detections_count=detections_count,
            proposals_generated=0,
            proposals_submitted=0,
            proposals_auto_approved=0,
            proposals_pending_human=0,
            errors=errors,
        )
        if not dry_run:
            _save_cycle(result)
        return result

    # Step 2: Generate proposals from detections
    proposer = _ensure_capability_proposer()
    if proposer is None:
        errors.append("Failed to load capability_proposer module")
        result = ImprovementCycleResult(
            cycle_id=cycle_id,
            timestamp=timestamp,
            detections_count=detections_count,
            proposals_generated=0,
            proposals_submitted=0,
            proposals_auto_approved=0,
            proposals_pending_human=0,
            errors=errors,
        )
        if not dry_run:
            _save_cycle(result)
        return result

    try:
        # Convert DetectionResult objects to dicts for the proposer
        detection_dicts = [d.to_dict() for d in detections]
        proposals = proposer.generate_proposals(detection_dicts)
        proposals_generated = len(proposals)
    except Exception as e:
        errors.append(f"Proposal generation failed: {e}")
        proposals = []

    if not proposals:
        result = ImprovementCycleResult(
            cycle_id=cycle_id,
            timestamp=timestamp,
            detections_count=detections_count,
            proposals_generated=proposals_generated,
            proposals_submitted=0,
            proposals_auto_approved=0,
            proposals_pending_human=0,
            errors=errors,
        )
        if not dry_run:
            _save_cycle(result)
        return result

    # Step 3 & 4: Convert to initiative format and submit
    engine = _ensure_initiative_engine()
    if engine is None:
        errors.append("Failed to load initiative_engine module")
        result = ImprovementCycleResult(
            cycle_id=cycle_id,
            timestamp=timestamp,
            detections_count=detections_count,
            proposals_generated=proposals_generated,
            proposals_submitted=0,
            proposals_auto_approved=0,
            proposals_pending_human=0,
            errors=errors,
        )
        if not dry_run:
            _save_cycle(result)
        return result

    for proposal in proposals:
        try:
            # Convert EnhancementProposal to initiative format
            initiative_data = proposer.convert_to_initiative(proposal)

            # Create initiative_engine.Proposal from the dict
            initiative_proposal = engine.Proposal.from_dict(initiative_data)

            # Submit the proposal
            result_obj = engine.submit_proposal(initiative_proposal, dry_run=dry_run)
            proposals_submitted += 1

            if result_obj.approved:
                proposals_auto_approved += 1
            else:
                proposals_pending_human += 1

        except Exception as e:
            errors.append(f"Proposal submission failed for {proposal.proposal_id}: {e}")

    # Build final result
    result = ImprovementCycleResult(
        cycle_id=cycle_id,
        timestamp=timestamp,
        detections_count=detections_count,
        proposals_generated=proposals_generated,
        proposals_submitted=proposals_submitted,
        proposals_auto_approved=proposals_auto_approved,
        proposals_pending_human=proposals_pending_human,
        errors=errors,
    )

    # Log cycle (unless dry run)
    if not dry_run:
        _save_cycle(result)

    return result


def get_recent_cycles(days: int = 30) -> list[ImprovementCycleResult]:
    """Get improvement cycles from the last N days.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        List of ImprovementCycleResult sorted by timestamp descending
    """
    cycles_data = _load_cycles()

    if not cycles_data:
        return []

    # Calculate cutoff time
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    # Filter cycles within time window
    recent = []
    for cycle_dict in cycles_data:
        timestamp = cycle_dict.get("timestamp", "")
        if timestamp >= cutoff_iso:
            recent.append(ImprovementCycleResult.from_dict(cycle_dict))

    # Sort by timestamp descending (most recent first)
    recent.sort(key=lambda c: c.timestamp, reverse=True)

    return recent


def get_improvement_status() -> dict[str, Any]:
    """Get a summary of improvement loop status.

    Returns:
        Dict with:
            - last_cycle_timestamp: When the last cycle ran
            - total_detections_30d: Total detections in last 30 days
            - total_proposals_submitted: Total proposals submitted
            - pending_proposals_count: Currently pending proposals
            - implemented_improvements_count: Proposals that were auto-approved
            - should_run: Whether a new cycle should run
            - should_run_reason: Explanation for should_run decision
    """
    cycles = get_recent_cycles(days=30)

    # Aggregate stats
    total_detections = sum(c.detections_count for c in cycles)
    total_submitted = sum(c.proposals_submitted for c in cycles)
    total_auto_approved = sum(c.proposals_auto_approved for c in cycles)

    # Get last cycle timestamp
    last_timestamp = cycles[0].timestamp if cycles else None

    # Check pending proposals
    pending_count = _count_pending_proposals()

    # Check if should run
    should_run, reason = should_run_improvement_cycle()

    return {
        "last_cycle_timestamp": last_timestamp,
        "total_detections_30d": total_detections,
        "total_proposals_submitted": total_submitted,
        "pending_proposals_count": pending_count,
        "implemented_improvements_count": total_auto_approved,
        "cycles_in_last_30d": len(cycles),
        "should_run": should_run,
        "should_run_reason": reason,
    }


def should_run_improvement_cycle(
    interval_days: int = DEFAULT_CYCLE_INTERVAL_DAYS,
    max_pending: int = MAX_PENDING_PROPOSALS,
) -> tuple[bool, str]:
    """Check if an improvement cycle should run.

    Args:
        interval_days: Minimum days between cycles (default: 7)
        max_pending: Maximum pending proposals before skipping (default: 5)

    Returns:
        Tuple of (should_run: bool, reason: str)
    """
    # Check pending proposals threshold
    pending_count = _count_pending_proposals()
    if pending_count >= max_pending:
        return (
            False,
            f"Too many pending proposals ({pending_count} >= {max_pending}). "
            f"Review pending proposals before running another cycle.",
        )

    # Check last cycle timestamp
    cycles = _load_cycles()
    if not cycles:
        return (True, "No previous cycles found. First run.")

    # Get most recent cycle
    latest = cycles[-1]
    last_timestamp = latest.get("timestamp", "")

    if not last_timestamp:
        return (True, "No valid timestamp in last cycle. Running fresh.")

    try:
        last_dt = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        elapsed = now - last_dt
        interval = timedelta(days=interval_days)

        if elapsed < interval:
            days_remaining = (interval - elapsed).days
            hours_remaining = ((interval - elapsed).seconds // 3600) % 24
            return (
                False,
                f"Last cycle was {elapsed.days}d {elapsed.seconds // 3600}h ago. "
                f"Next cycle in ~{days_remaining}d {hours_remaining}h.",
            )
        else:
            return (
                True,
                f"Last cycle was {elapsed.days} days ago (> {interval_days} day interval).",
            )

    except (ValueError, TypeError) as e:
        return (True, f"Could not parse last cycle timestamp: {e}. Running fresh.")


# =============================================================================
# CLI Interface
# =============================================================================


def cmd_run(args: argparse.Namespace) -> None:
    """Run an improvement cycle."""
    dry_run = args.dry_run

    if dry_run:
        print("Running improvement cycle (DRY RUN - no side effects)...\n")
    else:
        print("Running improvement cycle...\n")

    result = run_improvement_cycle(dry_run=dry_run)

    # Display results
    print("Improvement Cycle Results")
    print("=" * 50)
    print(f"Cycle ID: {result.cycle_id}")
    print(f"Timestamp: {result.timestamp}")
    print()
    print(f"Detections found: {result.detections_count}")
    print(f"Proposals generated: {result.proposals_generated}")
    print(f"Proposals submitted: {result.proposals_submitted}")
    print(f"  - Auto-approved: {result.proposals_auto_approved}")
    print(f"  - Pending human review: {result.proposals_pending_human}")

    if result.errors:
        print()
        print("Errors encountered:")
        for err in result.errors:
            print(f"  - {err}")

    if dry_run:
        print()
        print("(DRY RUN - no changes were made)")

    # Output JSON if requested
    if args.json:
        print()
        print("JSON output:")
        print(json.dumps(result.to_dict(), indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    """Show improvement loop status."""
    status = get_improvement_status()

    if args.json:
        print(json.dumps(status, indent=2))
        return

    print("Self-Improvement Loop Status")
    print("=" * 50)

    if status["last_cycle_timestamp"]:
        print(f"Last cycle: {status['last_cycle_timestamp']}")
    else:
        print("Last cycle: Never run")

    print()
    print("Last 30 Days:")
    print(f"  Cycles run: {status['cycles_in_last_30d']}")
    print(f"  Detections: {status['total_detections_30d']}")
    print(f"  Proposals submitted: {status['total_proposals_submitted']}")
    print(f"  Auto-approved (implemented): {status['implemented_improvements_count']}")

    print()
    print(f"Pending proposals: {status['pending_proposals_count']}")

    print()
    should_run_icon = "Yes" if status["should_run"] else "No"
    print(f"Should run new cycle: {should_run_icon}")
    print(f"  Reason: {status['should_run_reason']}")


def cmd_history(args: argparse.Namespace) -> None:
    """Show cycle history."""
    days = args.days
    cycles = get_recent_cycles(days=days)

    if args.json:
        output = {
            "days": days,
            "count": len(cycles),
            "cycles": [c.to_dict() for c in cycles],
        }
        print(json.dumps(output, indent=2))
        return

    print(f"Improvement Cycle History (last {days} days)")
    print("=" * 60)

    if not cycles:
        print("No cycles found in this time period.")
        return

    print(f"Found {len(cycles)} cycle(s):\n")

    for cycle in cycles:
        print(f"Cycle: {cycle.cycle_id}")
        print(f"  Time: {cycle.timestamp}")
        print(f"  Detections: {cycle.detections_count}")
        print(
            f"  Proposals: {cycle.proposals_generated} generated, {cycle.proposals_submitted} submitted"
        )
        print(
            f"  Results: {cycle.proposals_auto_approved} auto-approved, {cycle.proposals_pending_human} pending"
        )
        if cycle.errors:
            print(f"  Errors: {len(cycle.errors)}")
        print()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Self-Improvement Loop Orchestrator - P30 Component"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run command
    run_parser = subparsers.add_parser("run", help="Run an improvement cycle")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without side effects",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    run_parser.set_defaults(func=cmd_run)

    # status command
    status_parser = subparsers.add_parser("status", help="Show improvement status")
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    status_parser.set_defaults(func=cmd_status)

    # history command
    history_parser = subparsers.add_parser("history", help="Show cycle history")
    history_parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=30,
        help="Number of days to look back (default: 30)",
    )
    history_parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    history_parser.set_defaults(func=cmd_history)

    args = parser.parse_args()

    if hasattr(args, "func"):
        args.func(args)
    else:
        # Default: show status
        args.json = False
        cmd_status(args)


if __name__ == "__main__":
    main()
