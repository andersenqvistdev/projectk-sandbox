#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Proactive Initiative Engine — P13 Implementation

The inflection point where the system starts *thinking* instead of just *doing*.

This module enables autonomous opportunity detection, proposal generation,
and task submission without human prompting.

Components:
1. Opportunity Detectors — Scan for improvement opportunities
2. Proposal Generator — Create structured proposals with ROI estimates
3. Approval Router — Determine if human approval needed
4. Task Submitter — Convert approved proposals to queue tasks

Usage:
    # Scan for opportunities
    proposals = scan_all_opportunities()

    # Process proposals
    for proposal in proposals:
        result = submit_proposal(proposal)
        print(f"{proposal.title}: {result.approved}")

    # Or via CLI
    python initiative_engine.py scan
    python initiative_engine.py scan --execute
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# =============================================================================
# Enums
# =============================================================================


class ProposalType(Enum):
    """Types of proactive proposals the system can generate."""

    TEST_COVERAGE_SPRINT = "test_coverage_sprint"
    DEPENDENCY_UPDATE_MINOR = "dependency_update_minor"
    DEPENDENCY_UPDATE_MAJOR = "dependency_update_major"
    TASK_INVESTIGATION = "task_investigation"
    EMPLOYEE_REASSIGNMENT = "employee_reassignment"
    DOCUMENTATION_UPDATE = "documentation_update"
    SECURITY_FIX = "security_fix"
    PERFORMANCE_OPTIMIZATION = "performance_optimization"
    CODE_QUALITY = "code_quality"
    ROADMAP_PHASE = "roadmap_phase"  # Self-evolution: propose next roadmap phase
    HIRING_RECOMMENDATION = (
        "hiring_recommendation"  # Suggest hiring for capability gaps
    )
    CAPABILITY_ENHANCEMENT = (
        "capability_enhancement"  # Self-improvement: enhance system capabilities
    )


class ApprovalTier(Enum):
    """Approval tiers for proposals.

    Determines whether human approval is required.
    """

    AUTO_APPROVE = "auto_approve"  # Low risk, execute automatically
    CONFIG_APPROVE = "config_approve"  # Check config flag
    HUMAN_APPROVE = "human_approve"  # Requires human sign-off


class ProposalStatus(Enum):
    """Status of a proposal in its lifecycle."""

    PENDING = "pending"  # Awaiting approval decision
    APPROVED = "approved"  # Approved, ready for execution
    REJECTED = "rejected"  # Rejected by human or policy
    EXECUTED = "executed"  # Converted to tasks and submitted
    EXPIRED = "expired"  # Timed out without decision


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class Proposal:
    """A proactive initiative proposal.

    Represents an opportunity the system has detected, with estimated
    value, effort, and approval requirements.
    """

    proposal_id: str
    proposal_type: ProposalType
    title: str
    description: str
    rationale: str
    estimated_effort_minutes: int
    estimated_value: float  # 0-1 scale, 1 = highest value
    approval_tier: ApprovalTier
    source_data: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: ProposalStatus = ProposalStatus.PENDING
    priority: int = 3  # 1=Critical, 2=High, 3=Normal, 4=Low

    @property
    def roi_score(self) -> float:
        """Calculate ROI as value per minute of effort.

        Higher score = better return on investment.
        """
        if self.estimated_effort_minutes <= 0:
            return 0.0
        return self.estimated_value / (self.estimated_effort_minutes / 60.0)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "proposal_id": self.proposal_id,
            "proposal_type": self.proposal_type.value,
            "title": self.title,
            "description": self.description,
            "rationale": self.rationale,
            "estimated_effort_minutes": self.estimated_effort_minutes,
            "estimated_value": self.estimated_value,
            "roi_score": self.roi_score,
            "approval_tier": self.approval_tier.value,
            "source_data": self.source_data,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Proposal:
        """Create from dictionary."""
        return cls(
            proposal_id=data["proposal_id"],
            proposal_type=ProposalType(data["proposal_type"]),
            title=data["title"],
            description=data["description"],
            rationale=data["rationale"],
            estimated_effort_minutes=data["estimated_effort_minutes"],
            estimated_value=data["estimated_value"],
            approval_tier=ApprovalTier(data["approval_tier"]),
            source_data=data.get("source_data", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            status=ProposalStatus(data.get("status", "pending")),
            priority=data.get("priority", 3),
        )


@dataclass
class ProposalResult:
    """Result of processing a proposal."""

    proposal: Proposal
    approved: bool
    approval_method: str  # "auto", "config", "human"
    task_ids: list[str] = field(default_factory=list)
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "proposal": self.proposal.to_dict(),
            "approved": self.approved,
            "approval_method": self.approval_method,
            "task_ids": self.task_ids,
            "rejection_reason": self.rejection_reason,
        }


# =============================================================================
# Configuration
# =============================================================================


DEFAULT_CONFIG = {
    "enabled": True,
    "scanIntervalMinutes": 60,
    "autoApprove": {
        "testSprints": True,
        "minorDependencyUpdates": True,
        "documentationFixes": True,
        "majorDependencyUpdates": False,
        "employeeReassignment": False,
        "securityFixes": True,
        "capabilityEnhancements": False,  # Never auto-approve capability changes
    },
    "thresholds": {
        "testCoverageMinimum": 0.5,
        "idleEmployeeHours": 24,
        "failedTaskRetries": 2,
    },
    "limits": {
        "maxProposalsPerScan": 10,
        "maxAutoApprovePerHour": 5,
    },
}


def _get_module_dir() -> Path:
    """Get the directory containing this module."""
    return Path(__file__).parent


def _get_config() -> dict:
    """Load proactive configuration from forge-config.json."""
    config_path = _get_module_dir().parent.parent / "forge-config.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                full_config = json.load(f)
            return full_config.get("proactive", DEFAULT_CONFIG)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CONFIG


def _get_company_dir() -> Path:
    """Get the company directory path."""
    # Try to use company_resolver if available
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        import company_resolver

        return company_resolver.get_company_dir()
    except (ImportError, Exception):
        # Fallback to default
        return Path.cwd() / ".company"


def _create_proposal_reference_doc(proposal: "Proposal") -> str | None:
    """
    WS-108: Create a reference document for a proposal with full context.

    Returns the path to the created document, or None if creation fails.
    """
    company_dir = _get_company_dir()
    proposals_dir = company_dir / "proposals"
    proposals_dir.mkdir(exist_ok=True)

    doc_path = proposals_dir / f"{proposal.proposal_id}.md"

    # Build source data section
    source_lines = []
    for key, value in proposal.source_data.items():
        if isinstance(value, list):
            source_lines.append(f"**{key}:**")
            for item in value[:10]:  # Limit to first 10 items
                if isinstance(item, (list, tuple)):
                    source_lines.append(f"  - {' → '.join(str(x) for x in item)}")
                else:
                    source_lines.append(f"  - {item}")
            if len(value) > 10:
                source_lines.append(f"  - ... and {len(value) - 10} more")
        else:
            source_lines.append(f"**{key}:** {value}")
    source_section = "\n".join(source_lines) if source_lines else "No source data"

    # Extract values for shorter lines
    p_type = proposal.proposal_type
    type_val = p_type.value if hasattr(p_type, "value") else p_type
    status_val = (
        proposal.status.value if hasattr(proposal.status, "value") else proposal.status
    )
    created_val = (
        proposal.created_at.isoformat()
        if hasattr(proposal.created_at, "isoformat")
        else proposal.created_at
    )
    tier_val = (
        proposal.approval_tier.value
        if hasattr(proposal.approval_tier, "value")
        else proposal.approval_tier
    )

    doc_content = f"""# Proposal: {proposal.title}

**ID:** {proposal.proposal_id}
**Type:** {type_val}
**Status:** {status_val}
**Priority:** {proposal.priority}
**Created:** {created_val}

## Description

{proposal.description}

## Rationale

{proposal.rationale}

## Value Assessment

| Metric | Value |
|--------|-------|
| Estimated Value | {proposal.estimated_value:.2f}/1.0 |
| Estimated Effort | {proposal.estimated_effort_minutes} minutes |
| ROI Score | {proposal.roi_score:.3f} |
| Approval Tier | {tier_val} |

## Source Data

{source_section}

---
*Auto-generated proposal document. Reference for task implementation context.*
"""

    try:
        fd, tmp_path = tempfile.mkstemp(dir=proposals_dir, suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(doc_content)
        os.replace(tmp_path, doc_path)
        return str(doc_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None


def _get_project_root() -> Path:
    """Get the project root directory."""
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        import company_resolver

        company_root = company_resolver.find_company_root()
        return company_root if company_root else Path.cwd()
    except (ImportError, Exception):
        return Path.cwd()


# =============================================================================
# Proposal ID Generation
# =============================================================================


def _generate_proposal_id(proposal_type: ProposalType) -> str:
    """Generate a unique proposal ID."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    type_prefix = proposal_type.value[:4].upper()
    import hashlib

    hash_suffix = hashlib.md5(f"{timestamp}{type_prefix}".encode()).hexdigest()[:6]
    return f"prop-{type_prefix}-{timestamp}-{hash_suffix}"


# =============================================================================
# Proposal Content Fingerprinting (Deduplication)
# =============================================================================

# TTL in hours per proposal type — how long before the same proposal can be
# re-generated after one already exists (pending, executed, or rejected).
_DEDUP_TTL_HOURS_BY_TYPE: dict[str, int] = {
    ProposalType.DEPENDENCY_UPDATE_MINOR.value: 6,
    ProposalType.DEPENDENCY_UPDATE_MAJOR.value: 6,
    ProposalType.DOCUMENTATION_UPDATE.value: 24,
    ProposalType.TEST_COVERAGE_SPRINT.value: 12,
}
_DEDUP_DEFAULT_TTL_HOURS = 6


def _generate_content_fingerprint(proposal: "Proposal") -> str:
    """Return a stable SHA-256 fingerprint for a proposal's *content*.

    The fingerprint is based on the proposal type and the key identifiers
    from source_data — not the ID or timestamp, which change every scan.
    This detects duplicate proposals even when their titles differ slightly
    (e.g. different package counts) or after a prior proposal was actioned.
    """
    import hashlib

    ptype = proposal.proposal_type.value
    src = proposal.source_data

    if proposal.proposal_type in (
        ProposalType.DEPENDENCY_UPDATE_MINOR,
        ProposalType.DEPENDENCY_UPDATE_MAJOR,
    ):
        # Fingerprint on sorted package names; ignore versions which may vary
        # across scans while still referring to the same update opportunity.
        packages = src.get("packages", [])
        pkg_names = sorted(p[0].lower() for p in packages if p)
        key = ptype + ":pkgs:" + ",".join(pkg_names)

    elif proposal.proposal_type == ProposalType.DOCUMENTATION_UPDATE:
        # Fingerprint on sorted gap identifiers
        gaps = sorted(str(g).strip() for g in src.get("gaps", []))
        key = ptype + ":gaps:" + ",".join(gaps)

    elif proposal.proposal_type == ProposalType.TEST_COVERAGE_SPRINT:
        # Fingerprint on sorted low-coverage file names
        files = sorted(str(f).strip() for f in src.get("low_coverage_files", []))
        key = ptype + ":files:" + ",".join(files)

    else:
        # For other types use the type name alone — prevents same-type
        # proposals within the default TTL window.
        key = ptype

    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _get_dedup_path() -> Path:
    """Return the path to the proposal dedup index file."""
    return _get_company_dir() / "state/proposal_dedup.json"


def _load_dedup_index() -> dict[str, dict]:
    """Load the dedup index; return empty dict on any read/parse error."""
    path = _get_dedup_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("fingerprints", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_dedup_index(fingerprints: dict[str, dict]) -> None:
    """Atomically persist the dedup index."""
    path = _get_dedup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fingerprints": fingerprints}, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _dedup_entry_is_active(entry: dict) -> bool:
    """Return True if a dedup entry has not yet expired."""
    try:
        expires_at = datetime.fromisoformat(entry.get("expires_at", ""))
        return datetime.now(timezone.utc) < expires_at
    except (ValueError, TypeError):
        return False


def _is_duplicate_proposal(proposal: "Proposal") -> tuple[bool, str]:
    """Check if a matching unexpired proposal was recently recorded.

    Returns (is_duplicate, existing_proposal_id).
    """
    fingerprint = _generate_content_fingerprint(proposal)
    fingerprints = _load_dedup_index()
    entry = fingerprints.get(fingerprint)
    if entry is not None and _dedup_entry_is_active(entry):
        return True, entry.get("proposal_id", "")
    return False, ""


def _record_proposal_fingerprint(proposal: "Proposal") -> None:
    """Record a proposal's content fingerprint in the dedup index.

    Evicts expired entries on each write to keep the index small.
    """
    from datetime import timedelta

    fingerprint = _generate_content_fingerprint(proposal)
    ttl_hours = _DEDUP_TTL_HOURS_BY_TYPE.get(
        proposal.proposal_type.value, _DEDUP_DEFAULT_TTL_HOURS
    )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)

    fingerprints = _load_dedup_index()
    # Evict stale entries before writing
    fingerprints = {
        fp: entry for fp, entry in fingerprints.items() if _dedup_entry_is_active(entry)
    }
    fingerprints[fingerprint] = {
        "proposal_id": proposal.proposal_id,
        "proposal_type": proposal.proposal_type.value,
        "title": proposal.title,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    _save_dedup_index(fingerprints)


# =============================================================================
# Approval Tier Mapping
# =============================================================================

# Default tier mapping for each proposal type
PROPOSAL_TYPE_TIERS: dict[ProposalType, ApprovalTier] = {
    ProposalType.TEST_COVERAGE_SPRINT: ApprovalTier.AUTO_APPROVE,
    ProposalType.DEPENDENCY_UPDATE_MINOR: ApprovalTier.AUTO_APPROVE,
    ProposalType.DEPENDENCY_UPDATE_MAJOR: ApprovalTier.HUMAN_APPROVE,
    ProposalType.TASK_INVESTIGATION: ApprovalTier.AUTO_APPROVE,
    ProposalType.EMPLOYEE_REASSIGNMENT: ApprovalTier.CONFIG_APPROVE,
    ProposalType.DOCUMENTATION_UPDATE: ApprovalTier.HUMAN_APPROVE,  # Relevance gate (T-B3) not yet live
    ProposalType.SECURITY_FIX: ApprovalTier.AUTO_APPROVE,  # Urgent, auto-approve
    ProposalType.PERFORMANCE_OPTIMIZATION: ApprovalTier.CONFIG_APPROVE,
    ProposalType.CODE_QUALITY: ApprovalTier.AUTO_APPROVE,
    ProposalType.ROADMAP_PHASE: ApprovalTier.HUMAN_APPROVE,  # Self-evolution needs approval
    ProposalType.HIRING_RECOMMENDATION: ApprovalTier.HUMAN_APPROVE,  # Hiring always needs approval
    ProposalType.CAPABILITY_ENHANCEMENT: ApprovalTier.HUMAN_APPROVE,  # Capability changes always need human review
}


def get_approval_tier(proposal_type: ProposalType) -> ApprovalTier:
    """Get the approval tier for a proposal type."""
    return PROPOSAL_TYPE_TIERS.get(proposal_type, ApprovalTier.HUMAN_APPROVE)


# =============================================================================
# Opportunity Detectors
# =============================================================================


def detect_test_coverage_opportunity(threshold: float = 0.5) -> Proposal | None:
    """Detect if test coverage is below threshold.

    Returns a proposal for a test sprint if coverage is too low.
    """
    try:
        # Run pytest with coverage
        subprocess.run(
            ["uv", "run", "pytest", "--cov=.", "--cov-report=json", "-q"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=Path.cwd(),
        )

        # Parse coverage from JSON report
        cov_json = Path.cwd() / "coverage.json"
        if not cov_json.exists():
            return None

        with open(cov_json, encoding="utf-8") as f:
            cov_data = json.load(f)

        total_coverage = cov_data.get("totals", {}).get("percent_covered", 100) / 100

        if total_coverage >= threshold:
            return None

        # Find files with lowest coverage
        files = cov_data.get("files", {})
        low_coverage_files = sorted(
            [
                (f, d.get("summary", {}).get("percent_covered", 100))
                for f, d in files.items()
            ],
            key=lambda x: x[1],
        )[:5]

        return Proposal(
            proposal_id=_generate_proposal_id(ProposalType.TEST_COVERAGE_SPRINT),
            proposal_type=ProposalType.TEST_COVERAGE_SPRINT,
            title=f"Test Coverage Sprint: {total_coverage:.0%} → {threshold:.0%}",
            description=f"Current coverage is {total_coverage:.1%}, below target of {threshold:.0%}.",
            rationale=f"Low coverage increases bug risk. Files needing attention: {', '.join(f[0] for f in low_coverage_files[:3])}",
            estimated_effort_minutes=120,  # 2 hours
            estimated_value=0.8,  # High value
            approval_tier=get_approval_tier(ProposalType.TEST_COVERAGE_SPRINT),
            source_data={
                "current_coverage": total_coverage,
                "threshold": threshold,
                "low_coverage_files": low_coverage_files,
            },
            priority=2,  # High priority
        )

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def detect_dependency_updates() -> list[Proposal]:
    """Detect outdated dependencies.

    Returns proposals for minor and major updates separately.
    """
    proposals = []

    try:
        # Check for outdated packages using pip
        result = subprocess.run(
            ["uv", "pip", "list", "--outdated", "--format=json"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            return []

        outdated = json.loads(result.stdout) if result.stdout else []

        minor_updates = []
        major_updates = []

        for pkg in outdated:
            name = pkg.get("name", "")
            current = pkg.get("version", "")
            latest = pkg.get("latest_version", "")

            # Simple major version check
            current_major = current.split(".")[0] if current else "0"
            latest_major = latest.split(".")[0] if latest else "0"

            if current_major != latest_major:
                major_updates.append((name, current, latest))
            else:
                minor_updates.append((name, current, latest))

        # Create proposal for minor updates (auto-approve)
        if minor_updates:
            proposals.append(
                Proposal(
                    proposal_id=_generate_proposal_id(
                        ProposalType.DEPENDENCY_UPDATE_MINOR
                    ),
                    proposal_type=ProposalType.DEPENDENCY_UPDATE_MINOR,
                    title=f"Minor Dependency Updates ({len(minor_updates)} packages)",
                    description=f"Update {len(minor_updates)} packages to latest minor versions.",
                    rationale="Minor updates include bug fixes and security patches.",
                    estimated_effort_minutes=15,
                    estimated_value=0.5,
                    approval_tier=get_approval_tier(
                        ProposalType.DEPENDENCY_UPDATE_MINOR
                    ),
                    source_data={"packages": minor_updates},
                    priority=3,
                )
            )

        # Create proposal for major updates (human-approve)
        if major_updates:
            proposals.append(
                Proposal(
                    proposal_id=_generate_proposal_id(
                        ProposalType.DEPENDENCY_UPDATE_MAJOR
                    ),
                    proposal_type=ProposalType.DEPENDENCY_UPDATE_MAJOR,
                    title=f"Major Dependency Updates ({len(major_updates)} packages)",
                    description=f"Update {len(major_updates)} packages to new major versions.",
                    rationale="Major updates may include breaking changes. Review required.",
                    estimated_effort_minutes=60,
                    estimated_value=0.6,
                    approval_tier=get_approval_tier(
                        ProposalType.DEPENDENCY_UPDATE_MAJOR
                    ),
                    source_data={"packages": major_updates},
                    priority=3,
                )
            )

    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    return proposals


def detect_failed_task_patterns(queue_path: Path | None = None) -> list[Proposal]:
    """Detect tasks that have failed multiple times.

    Returns proposals to investigate root causes.
    """
    proposals = []
    config = _get_config()
    retry_threshold = config.get("thresholds", {}).get("failedTaskRetries", 2)

    if queue_path is None:
        queue_path = _get_company_dir() / "state/work_queue.json"

    if not queue_path.exists():
        return []

    try:
        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)

        # Check failed tasks
        failed_tasks = queue.get("failed", [])

        # Group by similar titles/patterns
        repeat_failures: dict[str, list] = {}
        for task in failed_tasks:
            # Simple grouping by title prefix
            title = task.get("title", "")[:30]
            if title not in repeat_failures:
                repeat_failures[title] = []
            repeat_failures[title].append(task)

        # Create proposals for patterns with multiple failures
        for pattern, tasks in repeat_failures.items():
            if len(tasks) >= retry_threshold:
                task_ids = [t.get("task_id", "unknown") for t in tasks]
                proposals.append(
                    Proposal(
                        proposal_id=_generate_proposal_id(
                            ProposalType.TASK_INVESTIGATION
                        ),
                        proposal_type=ProposalType.TASK_INVESTIGATION,
                        title=f"Investigate Repeated Failures: {pattern}...",
                        description=f"{len(tasks)} tasks matching this pattern have failed.",
                        rationale="Repeated failures suggest systemic issue requiring investigation.",
                        estimated_effort_minutes=30,
                        estimated_value=0.7,
                        approval_tier=get_approval_tier(
                            ProposalType.TASK_INVESTIGATION
                        ),
                        source_data={"task_ids": task_ids, "pattern": pattern},
                        priority=2,
                    )
                )

    except (json.JSONDecodeError, OSError):
        pass

    return proposals


def detect_idle_employees(hours_threshold: int = 24) -> list[Proposal]:
    """Detect employees with no recent activity.

    Returns proposals to reassign or check on idle employees.
    """
    proposals = []
    org_path = _get_company_dir() / "org.json"

    if not org_path.exists():
        return []

    try:
        with open(org_path, encoding="utf-8") as f:
            org = json.load(f)

        employees = org.get("employees", [])
        now = datetime.now(timezone.utc)
        threshold = now.timestamp() - (hours_threshold * 3600)

        idle_employees = []
        for emp in employees:
            last_active = emp.get("lastActive")
            if last_active is None:
                # Never active = idle
                idle_employees.append(emp)
            else:
                try:
                    last_ts = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                    if last_ts.timestamp() < threshold:
                        idle_employees.append(emp)
                except (ValueError, TypeError):
                    pass

        if idle_employees:
            emp_names = [e.get("name", e.get("id", "unknown")) for e in idle_employees]
            proposals.append(
                Proposal(
                    proposal_id=_generate_proposal_id(
                        ProposalType.EMPLOYEE_REASSIGNMENT
                    ),
                    proposal_type=ProposalType.EMPLOYEE_REASSIGNMENT,
                    title=f"Review Idle Employees ({len(idle_employees)})",
                    description=f"{len(idle_employees)} employees have no activity in {hours_threshold}h.",
                    rationale=f"Idle employees: {', '.join(emp_names[:3])}{'...' if len(emp_names) > 3 else ''}",
                    estimated_effort_minutes=15,
                    estimated_value=0.4,
                    approval_tier=get_approval_tier(ProposalType.EMPLOYEE_REASSIGNMENT),
                    source_data={
                        "idle_employee_ids": [e.get("id") for e in idle_employees],
                        "hours_threshold": hours_threshold,
                    },
                    priority=4,  # Low priority
                )
            )

    except (json.JSONDecodeError, OSError):
        pass

    return proposals


# Directories excluded from user-product documentation scanning.
# These are Forge framework internals or non-product paths that should
# never be surfaced as user documentation opportunities.
FRAMEWORK_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".claude",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".env",
        "node_modules",
        ".tox",
        ".pytest_cache",
        "build",
        "dist",
        ".eggs",
    }
)


def detect_documentation_gaps() -> list[Proposal]:
    """Detect missing module docstrings in user's product Python source.

    Scans Python files under the project root, skipping Forge framework
    internals (.claude/) and other non-product directories.  Routes to
    human approval because the relevance gate (T-B3) that would confirm
    a finding is truly about the user's product does not yet exist.

    Returns proposals for documentation improvements.
    """
    proposals = []
    gaps = []

    project_root = _get_project_root()

    for py_file in project_root.rglob("*.py"):
        try:
            rel_parts = py_file.relative_to(project_root).parts
        except ValueError:
            continue
        # Skip framework-internal and non-product directories
        if any(
            part in FRAMEWORK_EXCLUDE_DIRS or part.endswith(".egg-info")
            for part in rel_parts
        ):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            if not content.strip().startswith('"""') and not content.strip().startswith(
                "'''"
            ):
                gaps.append(f"Missing docstring: {py_file.relative_to(project_root)}")
        except OSError:
            pass

    if gaps:
        proposals.append(
            Proposal(
                proposal_id=_generate_proposal_id(ProposalType.DOCUMENTATION_UPDATE),
                proposal_type=ProposalType.DOCUMENTATION_UPDATE,
                title=f"Documentation Gaps ({len(gaps)} files)",
                description=f"Found {len(gaps)} files needing documentation improvements.",
                rationale=f"First gaps: {', '.join(gaps[:3])}",
                estimated_effort_minutes=45,
                estimated_value=0.4,
                approval_tier=get_approval_tier(ProposalType.DOCUMENTATION_UPDATE),
                source_data={"gaps": gaps},
                priority=4,
            )
        )

    return proposals


def detect_roadmap_opportunities() -> list[Proposal]:
    """Detect next roadmap phase to implement (self-evolution).

    Reads .planning/ROADMAP.md and proposes implementing the next
    incomplete phase. This is how the system evolves itself.

    Returns proposals for roadmap phases ready for implementation.
    """
    import re

    proposals = []

    # Find planning directory
    planning_dir = Path.cwd() / ".planning"
    roadmap_path = planning_dir / "ROADMAP.md"

    if not roadmap_path.exists():
        return []

    try:
        content = roadmap_path.read_text(encoding="utf-8")

        # Extract phases that are already COMPLETE to avoid re-proposing them
        # Look for patterns like: P14 ... COMPLETE or Status: COMPLETE
        completed_phases = set()

        # Match "## Current: P14" or "### P14:" with "COMPLETE" or "Status:** COMPLETE"
        completed_pattern = re.compile(
            r"(?:##\s*(?:Current|Previous):\s*(P\d+)|###\s*(P\d+):)[^\n]*"
            r"(?:\n.*?)?(?:COMPLETE|\*\*Status:\*\*\s*COMPLETE)",
            re.IGNORECASE | re.DOTALL,
        )
        for match in completed_pattern.finditer(content):
            phase_id = match.group(1) or match.group(2)
            if phase_id:
                completed_phases.add(phase_id.upper())

        # Also check for explicit "✓ COMPLETE" markers
        explicit_complete = re.compile(
            r"###\s*(P\d+):[^\n]*✓\s*COMPLETE", re.IGNORECASE
        )
        for match in explicit_complete.finditer(content):
            completed_phases.add(match.group(1).upper())

        # Parse future phases section
        future_match = re.search(r"## Future Phases.*?(?=\n## |$)", content, re.DOTALL)
        if not future_match:
            return []

        future_section = future_match.group(0)

        # Extract phase information using regex
        # Pattern: ### P14: Name (Date)
        phase_pattern = re.compile(
            r"### (P\d+): ([^\n(]+)\s*\(([^)]+)\)\s*\n"
            r"\*\*Owner:\*\* ([^\n]+)\s*\n"
            r"\s*\nCapabilities:\s*\n((?:- [^\n]+\n)+)",
            re.MULTILINE,
        )

        phases = []
        for match in phase_pattern.finditer(future_section):
            phase_id = match.group(1)
            name = match.group(2).strip()
            timeline = match.group(3).strip()
            owner = match.group(4).strip()
            capabilities_raw = match.group(5)

            # Parse capabilities
            capabilities = [
                line.strip("- \n")
                for line in capabilities_raw.split("\n")
                if line.strip().startswith("-")
            ]

            phases.append(
                {
                    "id": phase_id,
                    "name": name,
                    "timeline": timeline,
                    "owner": owner,
                    "capabilities": capabilities,
                }
            )

        if not phases:
            return []

        # Filter out phases that are already complete
        incomplete_phases = [
            p for p in phases if p["id"].upper() not in completed_phases
        ]

        if not incomplete_phases:
            return []  # All future phases are already complete

        # Get the next phase (first incomplete in the future list)
        next_phase = incomplete_phases[0]

        # Estimate effort based on capabilities count
        num_caps = len(next_phase["capabilities"])
        effort_minutes = num_caps * 60  # ~1 hour per capability

        # Create proposal for next phase
        proposals.append(
            Proposal(
                proposal_id=_generate_proposal_id(ProposalType.ROADMAP_PHASE),
                proposal_type=ProposalType.ROADMAP_PHASE,
                title=f"Implement {next_phase['id']}: {next_phase['name']}",
                description=(
                    f"Next phase in the autonomous evolution roadmap.\n\n"
                    f"Timeline: {next_phase['timeline']}\n"
                    f"Owner: {next_phase['owner']}\n\n"
                    f"Capabilities to implement:\n"
                    + "\n".join(f"- {cap}" for cap in next_phase["capabilities"])
                ),
                rationale=(
                    f"Self-evolution: {next_phase['id']} is the next phase in the "
                    f"journey to full autonomous operation. Implementing this "
                    f"expands the system's capabilities."
                ),
                estimated_effort_minutes=effort_minutes,
                estimated_value=0.9,  # High value - core evolution
                approval_tier=get_approval_tier(ProposalType.ROADMAP_PHASE),
                source_data={
                    "phase_id": next_phase["id"],
                    "phase_name": next_phase["name"],
                    "capabilities": next_phase["capabilities"],
                    "owner": next_phase["owner"],
                    "timeline": next_phase["timeline"],
                    "total_future_phases": len(phases),
                },
                priority=2,  # High priority - self-evolution
            )
        )

    except (OSError, re.error):
        pass

    return proposals


def detect_capability_gaps(min_gap_count: int = 3) -> list[Proposal]:
    """Detect capability gaps that suggest hiring needs.

    Reads capability gap data from employee_activator and proposes
    hiring employees with missing capabilities when gaps accumulate.

    Uses capability_gap_validator (P33) to:
    - Filter evidence to only gaps where the specific capability was missing
    - Auto-reject proposals when capability already exists in an employee
    - Recommend capability expansion over hiring when appropriate

    Args:
        min_gap_count: Minimum gap occurrences before proposing (default: 3)

    Returns:
        List of hiring recommendation proposals
    """
    proposals = []

    try:
        # Import employee_activator to get capability gap data
        import importlib.util

        activator_path = (
            _get_project_root() / ".claude/hooks/company/employee_activator.py"
        )
        if not activator_path.exists():
            return []

        spec = importlib.util.spec_from_file_location(
            "employee_activator", activator_path
        )
        if spec is None or spec.loader is None:
            return []

        employee_activator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(employee_activator)

        # Import capability_gap_validator (P33)
        validator_path = (
            _get_project_root() / ".claude/hooks/company/capability_gap_validator.py"
        )
        validator_available = False
        if validator_path.exists():
            validator_spec = importlib.util.spec_from_file_location(
                "capability_gap_validator", validator_path
            )
            if validator_spec and validator_spec.loader:
                capability_gap_validator = importlib.util.module_from_spec(
                    validator_spec
                )
                validator_spec.loader.exec_module(capability_gap_validator)
                validator_available = True

        # Get capability gap summary
        gap_summary = employee_activator.get_capability_gap_summary()
        recommendations = gap_summary.get("recommendations", [])

        if not recommendations:
            return []

        # Create proposal for each capability with significant gaps
        for rec in recommendations:
            cap = rec.get("capability", "unknown")
            gap_count = rec.get("gap_count", 0)

            if gap_count >= min_gap_count:
                # P33: Validate before creating proposal
                if validator_available:
                    validation = capability_gap_validator.validate_capability_proposal(
                        capability=cap,
                        gap_summary=gap_summary,
                        min_gap_count=min_gap_count,
                    )

                    if not validation.is_valid:
                        # Auto-reject: log and skip
                        capability_gap_validator.log_auto_rejection(
                            cap, validation.auto_reject_reason or "Unknown reason"
                        )
                        continue

                    # Use validated data
                    actual_gap_count = validation.actual_gap_count
                    filtered_gaps = validation.filtered_gaps[-5:]
                    recommended_action = validation.recommended_action

                    # Handle expansion recommendation
                    if (
                        recommended_action
                        == capability_gap_validator.RecommendedAction.EXPAND_EXISTING
                    ):
                        # Create capability_addition proposal instead of hiring
                        proposals.append(
                            Proposal(
                                proposal_id=_generate_proposal_id(
                                    ProposalType.CAPABILITY_ENHANCEMENT
                                ),
                                proposal_type=ProposalType.CAPABILITY_ENHANCEMENT,
                                title=f"Expand '{validation.expansion_candidate}' with '{cap}' capability",
                                description=(
                                    f"Employee '{validation.expansion_candidate}' handles '{cap}' tasks "
                                    f"with {validation.expansion_success_rate:.0%} concentration. "
                                    f"Consider adding '{cap}' to their capabilities."
                                ),
                                rationale=(
                                    f"Fallback employee '{validation.expansion_candidate}' consistently handles "
                                    f"tasks requiring '{cap}'. Adding this capability officially would improve "
                                    f"task routing accuracy without hiring overhead."
                                ),
                                estimated_effort_minutes=15,  # Quick capability addition
                                estimated_value=0.7,  # Higher value than hiring (lower cost)
                                approval_tier=ApprovalTier.CONFIG_APPROVE,  # Lower tier than hiring
                                source_data={
                                    "capability": cap,
                                    "gap_count": actual_gap_count,
                                    "recent_gaps": filtered_gaps,
                                    "expansion_candidate": validation.expansion_candidate,
                                    "success_rate": validation.expansion_success_rate,
                                    "action": "expand_existing",
                                },
                                priority=3,
                            )
                        )
                        continue

                    # Handle scaling recommendation (capability exists but overloaded)
                    if (
                        recommended_action
                        == capability_gap_validator.RecommendedAction.HIRE_FOR_SCALE
                    ):
                        covering = ", ".join(validation.covering_employees)
                        proposals.append(
                            Proposal(
                                proposal_id=_generate_proposal_id(
                                    ProposalType.HIRING_RECOMMENDATION
                                ),
                                proposal_type=ProposalType.HIRING_RECOMMENDATION,
                                title=f"Scale '{cap}' capability (hire additional specialist)",
                                description=(
                                    f"Capability '{cap}' exists in {covering} but workload is high "
                                    f"({validation.workload_ratio:.1f} tasks per employee). "
                                    f"Consider hiring additional specialist for scaling."
                                ),
                                rationale=(
                                    f"Scaling need detected: '{cap}' is covered but employees are overloaded. "
                                    f"Hiring an additional specialist would distribute workload and improve "
                                    f"throughput."
                                ),
                                estimated_effort_minutes=30,
                                estimated_value=0.5,  # Lower than new capability
                                approval_tier=ApprovalTier.HUMAN_APPROVE,
                                source_data={
                                    "capability": cap,
                                    "gap_count": actual_gap_count,
                                    "recent_gaps": filtered_gaps,
                                    "covering_employees": validation.covering_employees,
                                    "workload_ratio": validation.workload_ratio,
                                    "action": "hire_for_scale",
                                    "suggested_command": f"/company-hire {cap} specialist --department=engineering",
                                },
                                priority=4,  # Lower priority than new capability
                            )
                        )
                        continue
                else:
                    # Fallback: use unvalidated data (backward compatibility)
                    actual_gap_count = gap_count
                    filtered_gaps = gap_summary.get("gaps", [])[-5:]

                # Create hiring proposal with validated/filtered data
                proposals.append(
                    Proposal(
                        proposal_id=_generate_proposal_id(
                            ProposalType.HIRING_RECOMMENDATION
                        ),
                        proposal_type=ProposalType.HIRING_RECOMMENDATION,
                        title=f"Hire employee with '{cap}' capability",
                        description=(
                            f"Tasks requiring '{cap}' have been routed to fallback employees "
                            f"{actual_gap_count} times. Consider hiring a specialist."
                        ),
                        rationale=(
                            f"Capability gap detected: '{cap}' is frequently needed but not covered "
                            f"by current employees. Hiring a specialist would improve task routing "
                            f"and execution quality."
                        ),
                        estimated_effort_minutes=30,  # Time to hire via /company-hire
                        estimated_value=0.6,  # Medium-high value
                        approval_tier=ApprovalTier.HUMAN_APPROVE,  # Always human approval for hiring
                        source_data={
                            "capability": cap,
                            "gap_count": actual_gap_count,
                            "recent_gaps": filtered_gaps,  # P33: Now filtered!
                            "suggested_command": f"/company-hire {cap} specialist --department=engineering",
                        },
                        priority=3,  # Medium priority
                    )
                )

    except Exception:
        # Don't fail the scan if capability gap detection fails
        pass

    return proposals


def detect_improvement_opportunities() -> list[Proposal]:
    """Detect self-improvement opportunities via improvement_detector and capability_proposer.

    Uses lazy imports to avoid circular dependencies with modules being developed in parallel.

    Returns:
        List of CAPABILITY_ENHANCEMENT proposals for system improvements.
    """
    proposals: list[Proposal] = []
    module_dir = _get_module_dir()

    try:
        # Lazy import improvement_detector
        import importlib.util

        detector_path = module_dir / "improvement_detector.py"
        if not detector_path.exists():
            return []

        spec = importlib.util.spec_from_file_location(
            "improvement_detector", detector_path
        )
        if spec is None or spec.loader is None:
            return []

        improvement_detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(improvement_detector)

        # Lazy import capability_proposer
        proposer_path = module_dir / "capability_proposer.py"
        if not proposer_path.exists():
            return []

        spec2 = importlib.util.spec_from_file_location(
            "capability_proposer", proposer_path
        )
        if spec2 is None or spec2.loader is None:
            return []

        capability_proposer = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(capability_proposer)

        # Run improvement detections
        detections = improvement_detector.run_all_detections()

        # Generate proposals from detections
        raw_proposals = capability_proposer.generate_proposals(detections)

        # Convert capability_proposer proposals to initiative_engine Proposal format
        for raw in raw_proposals:
            # Map enhancement_type to estimated_complexity
            enhancement_type = raw.get("enhancement_type", "unknown")
            complexity_map = {
                "hook": "standard",
                "agent": "standard",
                "command": "complex",
                "workflow": "complex",
                "configuration": "trivial",
            }
            estimated_complexity = complexity_map.get(enhancement_type, "standard")

            # Build description with implementation hints
            description = raw.get("description", "")
            impl_hints = raw.get("implementation_hints", [])
            if impl_hints:
                description += "\n\nImplementation hints:\n" + "\n".join(
                    f"- {hint}" for hint in impl_hints
                )

            proposal = Proposal(
                proposal_id=_generate_proposal_id(ProposalType.CAPABILITY_ENHANCEMENT),
                proposal_type=ProposalType.CAPABILITY_ENHANCEMENT,
                title=raw.get("title", "Capability Enhancement"),
                description=description,
                rationale=raw.get("rationale", ""),
                estimated_effort_minutes=raw.get("estimated_effort_minutes", 60),
                estimated_value=raw.get("estimated_value", 0.5),
                approval_tier=get_approval_tier(ProposalType.CAPABILITY_ENHANCEMENT),
                source_data={
                    "enhancement_type": enhancement_type,
                    "detection_source": raw.get("detection_source", {}),
                    "implementation_hints": impl_hints,
                    "required_capabilities": raw.get("required_capabilities", []),
                    "estimated_complexity": estimated_complexity,
                },
                priority=raw.get("priority", 3),
            )
            proposals.append(proposal)

    except Exception:
        # Don't fail the scan if improvement detection fails
        pass

    return proposals


def scan_all_opportunities() -> list[Proposal]:
    """Run all opportunity detectors and return ranked proposals.

    Returns proposals sorted by ROI score (highest first).
    """
    config = _get_config()
    all_proposals: list[Proposal] = []

    # Run all detectors
    test_proposal = detect_test_coverage_opportunity(
        config.get("thresholds", {}).get("testCoverageMinimum", 0.5)
    )
    if test_proposal:
        all_proposals.append(test_proposal)

    all_proposals.extend(detect_dependency_updates())

    all_proposals.extend(detect_failed_task_patterns())

    all_proposals.extend(
        detect_idle_employees(config.get("thresholds", {}).get("idleEmployeeHours", 24))
    )

    all_proposals.extend(detect_documentation_gaps())

    # Self-evolution: detect next roadmap phase to implement
    all_proposals.extend(detect_roadmap_opportunities())

    # Capability gaps: suggest hiring for missing capabilities
    all_proposals.extend(
        detect_capability_gaps(
            config.get("thresholds", {}).get("capabilityGapMinCount", 3)
        )
    )

    # Self-improvement: detect capability enhancement opportunities
    all_proposals.extend(detect_improvement_opportunities())

    # Phase 3 learning loop: drop proposals that match a confirmed-phantom class
    # BEFORE they are ranked/submitted (reinforces task_admission at generation time).
    all_proposals = _filter_anti_patterns(all_proposals)

    # Sort by ROI score (highest first)
    all_proposals.sort(key=lambda p: p.roi_score, reverse=True)

    # Apply limit
    max_proposals = config.get("limits", {}).get("maxProposalsPerScan", 10)
    return all_proposals[:max_proposals]


def _filter_anti_patterns(proposals: list[Proposal]) -> list[Proposal]:
    """Remove proposals matching a learned anti-pattern (confirmed phantom class).

    Best-effort and fail-OPEN: any error loading/matching leaves the proposals
    untouched (a learning-loop bug must never starve the generator).
    """
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        repo_root = _get_company_dir().parent
        antipatterns = la.load_antipatterns(repo_root)
        if not antipatterns:
            return proposals
        kept: list[Proposal] = []
        for p in proposals:
            reason = la.match_task({"title": p.title}, antipatterns)
            if reason is None:
                kept.append(p)
        return kept
    except Exception:
        return proposals


# =============================================================================
# Approval Router
# =============================================================================


def check_approval(proposal: Proposal) -> tuple[bool, str]:
    """Check if a proposal should be approved.

    Returns:
        Tuple of (approved: bool, method: str)
        method is one of: "auto", "config", "human_required"
    """
    config = _get_config()
    auto_approve_config = config.get("autoApprove", {})

    tier = proposal.approval_tier

    if tier == ApprovalTier.AUTO_APPROVE:
        return True, "auto"

    elif tier == ApprovalTier.CONFIG_APPROVE:
        # Check specific config flags
        if proposal.proposal_type == ProposalType.EMPLOYEE_REASSIGNMENT:
            if auto_approve_config.get("employeeReassignment", False):
                return True, "config"
        elif proposal.proposal_type == ProposalType.DEPENDENCY_UPDATE_MAJOR:
            if auto_approve_config.get("majorDependencyUpdates", False):
                return True, "config"
        elif proposal.proposal_type == ProposalType.PERFORMANCE_OPTIMIZATION:
            # Default to manual for performance changes
            pass

        return False, "human_required"

    else:  # HUMAN_APPROVE
        return False, "human_required"


def request_human_approval(proposal: Proposal) -> None:
    """Add proposal to pending approvals for human review.

    Makes proposal visible via /pending command.
    """
    pending_path = _get_company_dir() / "state/pending_approvals.json"

    pending = {"proposals": []}
    if pending_path.exists():
        try:
            with open(pending_path, encoding="utf-8") as f:
                pending = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Add proposal
    if "proposals" not in pending:
        pending["proposals"] = []

    # Check for duplicates — by ID and by semantic match (same type + title)
    existing_ids = {p.get("proposal_id") for p in pending["proposals"]}
    existing_signatures = {
        (p.get("proposal_type"), p.get("title")) for p in pending["proposals"]
    }
    proposal_signature = (proposal.proposal_type.value, proposal.title)

    if (
        proposal.proposal_id not in existing_ids
        and proposal_signature not in existing_signatures
    ):
        pending["proposals"].append(proposal.to_dict())

        # Write back atomically (prevents corruption under parallel workers)
        fd, tmp_path = tempfile.mkstemp(dir=str(pending_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pending, f, indent=2)
            os.replace(tmp_path, str(pending_path))
        except BaseException:
            os.unlink(tmp_path)
            raise

        # Record fingerprint so scans within the TTL window won't duplicate
        _record_proposal_fingerprint(proposal)
        print(f"  PENDING APPROVAL: {proposal.title}")
        print("                    Use /pending to review, /respond to approve")


# =============================================================================
# Proposal to Task Conversion
# =============================================================================


def proposal_to_tasks(proposal: Proposal) -> list[dict]:
    """Convert an approved proposal to work queue tasks.

    WS-108: Enhanced to preserve full proposal context in tasks.

    Returns list of task dictionaries ready for submission.
    """
    tasks = []
    datetime.now(timezone.utc).isoformat()

    # WS-108: Create proposal reference document with full context
    ref_doc_path = _create_proposal_reference_doc(proposal)

    # WS-108: Build context footer for all tasks
    context_footer = [
        "",
        "---",
        f"**Proposal ID:** {proposal.proposal_id}",
        f"**Rationale:** {proposal.rationale}",
        f"**Estimated Value:** {proposal.estimated_value:.2f}/1.0",
        f"**Estimated Effort:** {proposal.estimated_effort_minutes} minutes",
        f"**ROI Score:** {proposal.roi_score:.3f}",
    ]
    if ref_doc_path:
        context_footer.append(f"**Full Context:** See `{ref_doc_path}`")
    context_footer_str = "\n".join(context_footer)

    if proposal.proposal_type == ProposalType.TEST_COVERAGE_SPRINT:
        # Create test sprint task
        low_cov_files = proposal.source_data.get("low_coverage_files", [])
        base_desc = f"{proposal.description}\n\nFiles to focus on:\n" + "\n".join(
            f"- {f[0]}: {f[1]:.0f}%" for f in low_cov_files[:5]
        )
        tasks.append(
            {
                "title": proposal.title,
                "description": base_desc + context_footer_str,
                "priority": proposal.priority,
                "required_capabilities": ["python", "testing", "pytest"],
                "source": "proactive",
                "estimated_complexity": "standard",
            }
        )

    elif proposal.proposal_type in (
        ProposalType.DEPENDENCY_UPDATE_MINOR,
        ProposalType.DEPENDENCY_UPDATE_MAJOR,
    ):
        packages = proposal.source_data.get("packages", [])
        base_desc = f"{proposal.description}\n\nPackages:\n" + "\n".join(
            f"- {p[0]}: {p[1]} → {p[2]}" for p in packages
        )
        tasks.append(
            {
                "title": proposal.title,
                "description": base_desc + context_footer_str,
                "priority": proposal.priority,
                "required_capabilities": ["python", "dependencies"],
                "source": "proactive",
                "estimated_complexity": "trivial"
                if proposal.proposal_type == ProposalType.DEPENDENCY_UPDATE_MINOR
                else "standard",
            }
        )

    elif proposal.proposal_type == ProposalType.TASK_INVESTIGATION:
        task_ids = proposal.source_data.get("task_ids", [])
        base_desc = f"{proposal.description}\n\nFailed task IDs: {', '.join(task_ids)}"
        tasks.append(
            {
                "title": proposal.title,
                "description": base_desc + context_footer_str,
                "priority": 2,  # High priority for investigations
                "required_capabilities": ["debugging", "analysis"],
                "source": "proactive",
                "estimated_complexity": "standard",
            }
        )

    elif proposal.proposal_type == ProposalType.EMPLOYEE_REASSIGNMENT:
        idle_ids = proposal.source_data.get("idle_employee_ids", [])
        base_desc = (
            f"{proposal.description}\n\nEmployees to review: {', '.join(idle_ids)}"
        )
        tasks.append(
            {
                "title": proposal.title,
                "description": base_desc + context_footer_str,
                "priority": 4,
                "required_capabilities": ["management"],
                "source": "proactive",
                "estimated_complexity": "trivial",
            }
        )

    elif proposal.proposal_type == ProposalType.DOCUMENTATION_UPDATE:
        gaps = proposal.source_data.get("gaps", [])
        base_desc = f"{proposal.description}\n\nGaps:\n" + "\n".join(
            f"- {g}" for g in gaps
        )
        tasks.append(
            {
                "title": proposal.title,
                "description": base_desc + context_footer_str,
                "priority": 4,
                "required_capabilities": ["documentation", "technical-writing"],
                "source": "proactive",
                "estimated_complexity": "standard",
            }
        )

    elif proposal.proposal_type == ProposalType.SECURITY_FIX:
        tasks.append(
            {
                "title": proposal.title,
                "description": proposal.description + context_footer_str,
                "priority": 1,  # Critical for security
                "required_capabilities": ["security"],
                "source": "proactive",
                "estimated_complexity": "standard",
            }
        )

    elif proposal.proposal_type == ProposalType.CAPABILITY_ENHANCEMENT:
        # Extract data from source_data
        required_caps = proposal.source_data.get("required_capabilities", [])
        if not required_caps:
            # Default capabilities for self-improvement tasks
            required_caps = ["python", "development"]
        estimated_complexity = proposal.source_data.get(
            "estimated_complexity", "standard"
        )
        impl_hints = proposal.source_data.get("implementation_hints", [])

        # Build task description with implementation hints
        description = proposal.description
        if impl_hints and "\nImplementation hints:" not in description:
            description += "\n\nImplementation hints:\n" + "\n".join(
                f"- {hint}" for hint in impl_hints
            )
        # WS-108: Add context footer
        description += context_footer_str

        tasks.append(
            {
                "title": proposal.title,
                "description": description,
                "priority": proposal.priority,
                "required_capabilities": required_caps,
                "source": "proactive",
                "estimated_complexity": estimated_complexity,
            }
        )

    else:
        # Generic task conversion - WS-108: Add context footer
        tasks.append(
            {
                "title": proposal.title,
                "description": proposal.description + context_footer_str,
                "priority": proposal.priority,
                "source": "proactive",
                "estimated_complexity": "standard",
            }
        )

    return tasks


def submit_proposal(proposal: Proposal, dry_run: bool = False) -> ProposalResult:
    """Process a proposal through approval and submission.

    Args:
        proposal: The proposal to process
        dry_run: If True, don't actually submit tasks

    Returns:
        ProposalResult with approval status and task IDs
    """
    # Deduplication check — skip proposals that duplicate a recently-created one.
    # This prevents the engine from re-generating identical proposals every scan
    # cycle after the previous one was approved, rejected, or expired from pending.
    is_dup, existing_id = _is_duplicate_proposal(proposal)
    if is_dup:
        print(f"  SKIPPED (duplicate): {proposal.title}")
        print(f"                       Existing proposal: {existing_id}")
        return ProposalResult(
            proposal=proposal,
            approved=False,
            approval_method="deduplicated",
            rejection_reason=f"Duplicate of existing proposal {existing_id}",
        )

    # Check approval
    approved, method = check_approval(proposal)

    if not approved:
        # Request human approval
        if not dry_run:
            request_human_approval(proposal)
        return ProposalResult(
            proposal=proposal,
            approved=False,
            approval_method=method,
            rejection_reason="Awaiting human approval",
        )

    # Convert to tasks
    tasks = proposal_to_tasks(proposal)

    if dry_run:
        return ProposalResult(
            proposal=proposal,
            approved=True,
            approval_method=method,
            task_ids=[f"dry-run-{i}" for i in range(len(tasks))],
        )

    # Submit tasks to work queue
    task_ids = []
    try:
        # Import work_allocator
        module_dir = _get_module_dir()
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))

        import work_allocator

        for task in tasks:
            result = work_allocator.add_task(**task)
            if result.get("success"):
                task_ids.append(result.get("task_id", "unknown"))

    except ImportError:
        safe_title = proposal.title.replace("\n", "\\n").replace("\r", "\\r")
        print(
            f"warning: work_allocator not importable; proposal '{safe_title}'"
            " approved but tasks NOT added to the work queue."
            " Dedup fingerprint will be recorded — re-submission is blocked.",
            file=sys.stderr,
        )

    # Record fingerprint so future scans won't regenerate the same proposal
    _record_proposal_fingerprint(proposal)

    # Update proposal status
    proposal.status = ProposalStatus.EXECUTED

    return ProposalResult(
        proposal=proposal,
        approved=True,
        approval_method=method,
        task_ids=task_ids,
    )


def execute_proposal_batch(
    proposals: list[Proposal], dry_run: bool = False
) -> list[ProposalResult]:
    """Process multiple proposals with rate limiting.

    Args:
        proposals: List of proposals to process
        dry_run: If True, don't actually submit tasks

    Returns:
        List of ProposalResult for each proposal
    """
    config = _get_config()
    max_auto = config.get("limits", {}).get("maxAutoApprovePerHour", 5)

    results = []
    auto_approved_count = 0

    for proposal in proposals:
        # Check rate limit for auto-approvals
        approved, method = check_approval(proposal)
        if approved and method in ("auto", "config"):
            if auto_approved_count >= max_auto:
                # Rate limit exceeded, defer to human
                results.append(
                    ProposalResult(
                        proposal=proposal,
                        approved=False,
                        approval_method="rate_limited",
                        rejection_reason=f"Auto-approve limit ({max_auto}/hour) reached",
                    )
                )
                continue
            auto_approved_count += 1

        result = submit_proposal(proposal, dry_run=dry_run)
        results.append(result)

    return results


# =============================================================================
# CLI
# =============================================================================


def cmd_scan(args: argparse.Namespace) -> None:
    """Scan for opportunities and show/execute proposals."""
    print("Scanning for improvement opportunities...\n")

    proposals = scan_all_opportunities()

    if not proposals:
        print("No improvement opportunities detected.")
        return

    print(f"Found {len(proposals)} opportunities:\n")

    # Display proposals
    for i, p in enumerate(proposals, 1):
        approved, method = check_approval(p)
        status = "✓ auto-approve" if approved else "⏳ needs approval"
        print(f"{i}. [{p.proposal_type.value}] {p.title}")
        print(
            f"   ROI: {p.roi_score:.2f} | Effort: {p.estimated_effort_minutes}min | {status}"
        )
        print(f"   {p.rationale[:80]}...")
        print()

    if args.execute:
        print("\nExecuting eligible proposals...\n")
        results = execute_proposal_batch(proposals, dry_run=args.dry_run)

        approved_count = sum(1 for r in results if r.approved)
        print(f"\nResults: {approved_count}/{len(results)} proposals processed")

        for r in results:
            status = "✓" if r.approved else "⏳"
            tasks = f" -> {len(r.task_ids)} tasks" if r.task_ids else ""
            reason = f" ({r.rejection_reason})" if r.rejection_reason else ""
            print(f"  {status} {r.proposal.title}{tasks}{reason}")


def cmd_status(args: argparse.Namespace) -> None:
    """Show proactive engine status."""
    config = _get_config()

    print("Proactive Initiative Engine Status")
    print("=" * 50)
    print(f"Enabled: {config.get('enabled', False)}")
    print(f"Scan Interval: {config.get('scanIntervalMinutes', 60)} minutes")
    print()

    print("Auto-Approve Settings:")
    auto = config.get("autoApprove", {})
    for key, value in auto.items():
        print(f"  {key}: {'✓' if value else '✗'}")
    print()

    print("Thresholds:")
    thresholds = config.get("thresholds", {})
    for key, value in thresholds.items():
        print(f"  {key}: {value}")
    print()

    # Check pending approvals
    pending_path = _get_company_dir() / "state/pending_approvals.json"
    if pending_path.exists():
        try:
            with open(pending_path, encoding="utf-8") as f:
                pending = json.load(f)
            count = len(pending.get("proposals", []))
            print(f"Pending Approvals: {count}")
        except (json.JSONDecodeError, OSError):
            pass


def cmd_respond(args: argparse.Namespace) -> None:
    """Respond to a pending proposal (approve/reject)."""
    proposal_id = args.proposal_id
    action = args.action.lower()
    reason = args.reason

    if action not in ("approve", "reject"):
        print(f"Invalid action: {action}. Use 'approve' or 'reject'.", file=sys.stderr)
        sys.exit(1)

    pending_path = _get_company_dir() / "state/pending_approvals.json"
    if not pending_path.exists():
        print("No pending approvals found.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(pending_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Failed to read pending approvals: {e}", file=sys.stderr)
        sys.exit(1)

    proposals = data.get("proposals", [])
    rejected = data.get("rejected", [])

    # Find the proposal
    found_idx = None
    found_proposal = None
    for i, p in enumerate(proposals):
        if p.get("proposal_id") == proposal_id:
            found_idx = i
            found_proposal = p
            break

    if found_proposal is None:
        print(f"Proposal not found: {proposal_id}", file=sys.stderr)
        print(f"Pending proposals: {[p.get('proposal_id') for p in proposals]}")
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()

    if action == "approve":
        # Convert to task
        proposal_obj = Proposal(
            proposal_id=found_proposal["proposal_id"],
            proposal_type=ProposalType(found_proposal["proposal_type"]),
            title=found_proposal["title"],
            description=found_proposal.get("description", ""),
            rationale=found_proposal.get("rationale", ""),
            estimated_effort_minutes=found_proposal.get("estimated_effort_minutes", 30),
            estimated_value=found_proposal.get("estimated_value", 0.5),
            approval_tier=ApprovalTier(
                found_proposal.get("approval_tier", "human_approve")
            ),
            source_data=found_proposal.get("source_data", {}),
            created_at=datetime.fromisoformat(
                found_proposal.get("created_at", now).replace("Z", "+00:00")
            ),
            status=ProposalStatus.APPROVED,
        )

        # Submit the proposal
        result = submit_proposal(proposal_obj, dry_run=False)

        # Remove from pending
        proposals.pop(found_idx)

        # Track approved
        approved_list = data.get("approved", [])
        approved_list.append(
            {
                "proposal_id": proposal_id,
                "approved_at": now,
                "task_ids": result.task_ids,
            }
        )
        data["approved"] = approved_list

        print(f"✓ Approved: {found_proposal['title']}")
        if result.task_ids:
            print(f"  Created tasks: {', '.join(result.task_ids)}")

    else:  # reject
        # Remove from pending
        proposals.pop(found_idx)

        # Add to rejected
        rejected.append(
            {
                "proposal_id": proposal_id,
                "proposal_type": found_proposal.get("proposal_type"),
                "title": found_proposal.get("title"),
                "rejected_at": now,
                "rejected_reason": reason or "Rejected by human",
            }
        )

        print(f"✗ Rejected: {found_proposal['title']}")
        if reason:
            print(f"  Reason: {reason}")

    # Save updated data
    data["proposals"] = proposals
    data["rejected"] = rejected

    # Write atomically (prevents corruption under parallel workers)
    fd, tmp_path = tempfile.mkstemp(dir=str(pending_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(pending_path))
    except BaseException:
        os.unlink(tmp_path)
        raise


def cmd_pending(args: argparse.Namespace) -> None:
    """List pending proposals awaiting human approval."""
    pending_path = _get_company_dir() / "state/pending_approvals.json"

    if not pending_path.exists():
        print(json.dumps({"proposals": [], "count": 0}))
        return

    try:
        with open(pending_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(json.dumps({"proposals": [], "count": 0}))
        return

    proposals = data.get("proposals", [])

    if args.json:
        print(json.dumps({"proposals": proposals, "count": len(proposals)}, indent=2))
    else:
        if not proposals:
            print("No pending proactive proposals.")
            return

        print(f"Pending Proactive Proposals ({len(proposals)}):\n")
        for p in proposals:
            print(f"  ID: {p.get('proposal_id')}")
            print(f"  Type: {p.get('proposal_type')}")
            print(f"  Title: {p.get('title')}")
            print(f"  ROI: {p.get('roi_score', 0):.2f}")
            print(f"  Effort: {p.get('estimated_effort_minutes', '?')} min")
            print(f"  Created: {p.get('created_at', 'unknown')}")
            print()
            print(
                f"  To approve: initiative_engine.py respond {p.get('proposal_id')} approve"
            )
            print(
                f'  To reject:  initiative_engine.py respond {p.get("proposal_id")} reject "reason"'
            )
            print("-" * 60)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Proactive Initiative Engine — Autonomous opportunity detection"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for opportunities")
    scan_parser.add_argument(
        "--execute", action="store_true", help="Execute eligible proposals"
    )
    scan_parser.add_argument(
        "--dry-run", action="store_true", help="Preview without executing"
    )
    scan_parser.set_defaults(func=cmd_scan)

    # status command
    status_parser = subparsers.add_parser("status", help="Show engine status")
    status_parser.set_defaults(func=cmd_status)

    # respond command
    respond_parser = subparsers.add_parser(
        "respond", help="Respond to a pending proposal"
    )
    respond_parser.add_argument("proposal_id", help="Proposal ID to respond to")
    respond_parser.add_argument(
        "action", choices=["approve", "reject"], help="Action to take"
    )
    respond_parser.add_argument(
        "reason", nargs="?", default="", help="Reason for rejection"
    )
    respond_parser.set_defaults(func=cmd_respond)

    # pending command
    pending_parser = subparsers.add_parser("pending", help="List pending proposals")
    pending_parser.add_argument("--json", action="store_true", help="Output as JSON")
    pending_parser.set_defaults(func=cmd_pending)

    args = parser.parse_args()

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
