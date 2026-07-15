#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# P17: Executive Loop
"""
Executive Loop — periodic activation of CEO/CTO for strategic thinking.

P17 implementation: Activates executives with full context (agent definition +
memory + company state) so they THINK and submit strategic work, closing the
"self-starting" gap.

This parallels P16 Employee Activation but for executives:
- P16: Tasks → matched employee → context → execution → memory update
- P17: Schedule/trigger → executive → context → decisions → work submitted

Usage:
    # Run executive loop (both CEO and CTO)
    python executive_loop.py run

    # Run single executive
    python executive_loop.py invoke --executive forge-ceo

    # Check if loop should run
    python executive_loop.py should-run

    # Show executive context (dry run)
    python executive_loop.py context --executive forge-ceo
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Lazy imports for sibling modules
company_resolver = None
input_channel = None
goal_tracker = None
memory_sync = None
efficiency_tracker = None  # WS-009-002: Token tracking
strategic_planner = None  # P29: Planning triggers
escalation = None  # Escalation manager

# Executive invocation order: CEO sets direction, CTO translates to technical work
EXECUTIVE_ORDER = ["forge-ceo", "forge-cto"]


def _ensure_imports():
    """Lazily import sibling modules."""
    global \
        company_resolver, \
        input_channel, \
        goal_tracker, \
        memory_sync, \
        efficiency_tracker, \
        strategic_planner, \
        escalation
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import efficiency_tracker as et  # WS-009-002
        from . import escalation as esc
        from . import goal_tracker as gt
        from . import input_channel as ic
        from . import memory_sync as ms
        from . import strategic_planner as sp  # P29

        company_resolver = cr
        input_channel = ic
        goal_tracker = gt
        memory_sync = ms
        efficiency_tracker = et
        strategic_planner = sp
        escalation = esc
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import efficiency_tracker as et  # type: ignore[no-redef]
        import escalation as esc  # type: ignore[no-redef]
        import goal_tracker as gt  # type: ignore[no-redef]
        import input_channel as ic  # type: ignore[no-redef]
        import memory_sync as ms  # type: ignore[no-redef]
        import strategic_planner as sp  # type: ignore[no-redef]

        company_resolver = cr
        input_channel = ic
        goal_tracker = gt
        memory_sync = ms
        efficiency_tracker = et
        strategic_planner = sp
        escalation = esc


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------


@dataclass
class ExecutiveContext:
    """Full context for executive activation."""

    executive_id: str
    name: str
    role: str  # "CEO" | "CTO"
    memory_content: str
    agent_definition: str
    # Current company state
    goal_assessments: list[dict] = field(default_factory=list)
    queue_status: dict = field(default_factory=dict)
    employee_status: list[dict] = field(default_factory=list)
    recent_decisions: list[dict] = field(default_factory=list)
    active_initiatives: list[dict] = field(default_factory=list)
    # P20: Planning authority
    pending_plans_ceo: list[dict] = field(default_factory=list)
    pending_plans_board: list[dict] = field(default_factory=list)
    active_board_sessions: list[dict] = field(default_factory=list)
    # Paths for reference
    memory_path: str | None = None
    agent_definition_path: str | None = None


@dataclass
class ExecutiveDecision:
    """A decision made by an executive."""

    # fmt: off
    decision_type: str  # "submit_work" | "update_vision" | "escalate" | "delegate" | "plan_decision" | "board_vote" | "planning_trigger"
    # fmt: on
    description: str
    rationale: str
    work_items: list[dict] = field(default_factory=list)
    requires_approval: bool = False
    priority: int = 3  # 1-5, 5 = critical
    # P20: Planning authority fields
    plan_id: str | None = None
    session_id: str | None = None
    plan_action: str | None = None  # "approve" | "revise" | "reject"
    plan_comments: str | None = None
    # P29: Planning trigger fields
    planning_cycle: str | None = None  # "weekly" | "daily"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutiveSession:
    """Record of an executive activation session."""

    executive_id: str
    started_at: str
    duration_seconds: float
    trigger: str  # "scheduled" | "empty_queue" | "manual"
    decisions: list[ExecutiveDecision] = field(default_factory=list)
    work_submitted: int = 0
    escalations: int = 0
    output_raw: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decisions"] = [dec.to_dict() for dec in self.decisions]
        return d


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def get_executive_loop_config() -> dict:
    """Load executive loop configuration from forge-config.json."""
    _ensure_imports()

    try:
        project_root = company_resolver.find_company_root()
        if not project_root:
            project_root = Path.cwd()

        config_path = project_root / "forge-config.json"
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
                return config.get("executiveLoop", {})
    except Exception:
        pass

    # Defaults
    return {
        "enabled": True,
        "intervalHours": 12,
        "triggerOnEmptyQueue": True,
        "skipIfQueueAbove": 5,
        "executives": ["forge-ceo", "forge-cto"],
        "maxWorkItemsPerSession": 5,
        "requireApprovalFor": ["vision_change", "hiring", "major_initiative"],
        "autoApproveFor": ["small_task", "bug_fix", "documentation"],
    }


# -----------------------------------------------------------------------------
# Context Loading
# -----------------------------------------------------------------------------


def get_executive_memory_path(executive_id: str) -> Path | None:
    """Get path to executive's memory file."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return None

        # Standard location for executive memory
        memory_path = company_dir / "agents" / executive_id / "memory.md"
        if memory_path.exists():
            return memory_path

        return None
    except Exception:
        return None


def get_executive_agent_definition_path(executive_id: str) -> Path | None:
    """Get path to executive's agent definition."""
    _ensure_imports()

    try:
        project_root = company_resolver.find_company_root()
        if not project_root:
            project_root = Path.cwd()

        # Check company agents directory
        agent_path = (
            project_root / ".claude" / "agents" / "company" / f"{executive_id}.md"
        )
        if agent_path.exists():
            return agent_path

        return None
    except Exception:
        return None


def get_executive_from_org(executive_id: str) -> dict | None:
    """Get executive record from org.json."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return None

        org_path = company_dir / "org.json"
        if not org_path.exists():
            return None

        with open(org_path, encoding="utf-8") as f:
            org = json.load(f)

        # Check employees/agents for the executive
        employees = org.get("employees", org.get("agents", []))
        for emp in employees:
            if emp.get("id") == executive_id:
                return emp

        return None
    except Exception:
        return None


def load_goal_assessments() -> list[dict]:
    """Load current goal assessments."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return []

        # Try strategic_state.json for latest assessments
        state_path = company_dir / "state/strategic_state.json"
        if state_path.exists():
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)

            snapshots = state.get("goal_snapshots", [])
            if snapshots:
                # Return most recent snapshot's assessments
                return snapshots[-1].get("assessments", [])

        # Fallback to goal_tracker (lightweight to avoid pytest in daemon)
        return goal_tracker.assess_all_goals(lightweight=True)

    except Exception:
        return []


def load_queue_status() -> dict:
    """Load current work queue status."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return {}

        queue_path = company_dir / "state/work_queue.json"
        if not queue_path.exists():
            return {"pending": 0, "in_progress": 0, "completed": 0, "blocked": 0}

        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)

        # Support both queue formats: list-based (queue["tasks"])
        # and dict-based (queue["pending"], queue["in_progress"], etc.)
        if "tasks" in queue:
            tasks = queue["tasks"]
            status_counts = {
                "pending": 0,
                "in_progress": 0,
                "completed": 0,
                "blocked": 0,
                "failed": 0,
            }
            completed_today = 0
            today = datetime.now(timezone.utc).date().isoformat()

            for task in tasks:
                status = task.get("status", "pending")
                if status in status_counts:
                    status_counts[status] += 1
                if status == "completed":
                    completed_at = task.get("completed_at", "")
                    if completed_at.startswith(today):
                        completed_today += 1

            status_counts["completed_today"] = completed_today
            status_counts["total"] = len(tasks)
            return status_counts
        else:
            # Dict-based queue format
            today = datetime.now(timezone.utc).date().isoformat()
            completed_list = queue.get("completed", [])
            completed_today = sum(
                1
                for t in completed_list
                if (t.get("completed_at") or "").startswith(today)
            )
            total = sum(
                len(queue.get(k, []))
                for k in ("pending", "in_progress", "completed", "blocked")
            )
            return {
                "pending": len(queue.get("pending", [])),
                "in_progress": len(queue.get("in_progress", [])),
                "completed": len(queue.get("completed", [])),
                "blocked": len(queue.get("blocked", [])),
                "failed": 0,
                "completed_today": completed_today,
                "total": total,
            }

    except Exception:
        return {}


def load_employee_status() -> list[dict]:
    """Load current employee status summary."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return []

        org_path = company_dir / "org.json"
        if not org_path.exists():
            return []

        with open(org_path, encoding="utf-8") as f:
            org = json.load(f)

        employees = org.get("employees", org.get("agents", []))
        summaries = []

        for emp in employees:
            # Skip executives
            if emp.get("id") in EXECUTIVE_ORDER:
                continue

            summaries.append(
                {
                    "id": emp.get("id"),
                    "name": emp.get("name"),
                    "department": emp.get("department"),
                    "status": emp.get("status", "available"),
                    "capabilities": emp.get("capabilities", []),
                }
            )

        return summaries

    except Exception:
        return []


def load_active_initiatives() -> list[dict]:
    """Load active strategic initiatives."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return []

        state_path = company_dir / "state/strategic_state.json"
        if not state_path.exists():
            return []

        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

        return state.get("active_initiatives", [])

    except Exception:
        return []


def load_recent_decisions(executive_id: str, limit: int = 5) -> list[dict]:
    """Load recent decisions from executive's memory."""
    memory_path = get_executive_memory_path(executive_id)
    if not memory_path or not memory_path.exists():
        return []

    try:
        content = memory_path.read_text(encoding="utf-8")

        # Parse "### YYYY-MM-DD - Executive Session" sections
        decisions = []
        session_pattern = r"### \d{4}-\d{2}-\d{2} - Executive Session"
        sections = re.split(session_pattern, content)

        for section in sections[1 : limit + 1]:  # Skip header, limit results
            # Extract decision summaries
            if "#### Decisions Made" in section:
                decisions.append(
                    {
                        "session": section[:100],  # First 100 chars as summary
                    }
                )

        return decisions

    except Exception:
        return []


def load_pending_plans() -> dict:
    """Load pending plans from planning authority (P20)."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        if not company_dir:
            return {"ceo": [], "board": [], "sessions": []}

        state_path = company_dir / "planning_approvals.json"
        if not state_path.exists():
            return {"ceo": [], "board": [], "sessions": []}

        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

        # Get pending CEO review plans
        pending_ceo_ids = set(state.get("pending_ceo_review", []))
        ceo_plans = [
            p for p in state.get("plans", []) if p.get("plan_id") in pending_ceo_ids
        ]

        # Get pending board review plans
        pending_board_ids = set(state.get("pending_board_review", []))
        board_plans = [
            p for p in state.get("plans", []) if p.get("plan_id") in pending_board_ids
        ]

        # Get active board sessions
        active_sessions = [
            s
            for s in state.get("board_sessions", [])
            if s.get("status") in ["scheduled", "in_progress"]
        ]

        return {"ceo": ceo_plans, "board": board_plans, "sessions": active_sessions}

    except Exception:
        return {"ceo": [], "board": [], "sessions": []}


def load_executive_context(executive_id: str) -> ExecutiveContext | None:
    """Load full context for executive activation."""
    _ensure_imports()

    # Get executive record
    executive = get_executive_from_org(executive_id)
    if not executive:
        return None

    # Determine role
    role = "CEO" if "ceo" in executive_id.lower() else "CTO"

    # Load memory
    memory_path = get_executive_memory_path(executive_id)
    memory_content = ""
    if memory_path and memory_path.exists():
        memory_content = memory_path.read_text(encoding="utf-8")

    # Load agent definition
    agent_path = get_executive_agent_definition_path(executive_id)
    agent_definition = ""
    if agent_path and agent_path.exists():
        agent_definition = agent_path.read_text(encoding="utf-8")

    # If no agent definition, can't proceed meaningfully
    if not agent_definition:
        return None

    # P20: Load pending plans for planning authority
    pending_plans = load_pending_plans()

    return ExecutiveContext(
        executive_id=executive_id,
        name=executive.get("name", executive_id),
        role=role,
        memory_content=memory_content,
        agent_definition=agent_definition,
        goal_assessments=load_goal_assessments(),
        queue_status=load_queue_status(),
        employee_status=load_employee_status(),
        recent_decisions=load_recent_decisions(executive_id),
        active_initiatives=load_active_initiatives(),
        pending_plans_ceo=pending_plans.get("ceo", []),
        pending_plans_board=pending_plans.get("board", []),
        active_board_sessions=pending_plans.get("sessions", []),
        memory_path=str(memory_path) if memory_path else None,
        agent_definition_path=str(agent_path) if agent_path else None,
    )


# -----------------------------------------------------------------------------
# Prompt Building
# -----------------------------------------------------------------------------


def format_goal_assessments(assessments: list[dict]) -> str:
    """Format goal assessments for prompt."""
    if not assessments:
        return "No goal assessments available."

    lines = [
        "| Goal | Status | Progress | Notes |",
        "|------|--------|----------|-------|",
    ]

    for a in assessments:
        goal_id = a.get("goal_id", "?")
        goal_name = a.get("goal_name", "")
        status = a.get("status", "unknown")
        progress = a.get("progress_percent", 0)
        reason = a.get("status_reason", "")[:50]

        lines.append(f"| {goal_id}: {goal_name} | {status} | {progress}% | {reason} |")

    return "\n".join(lines)


def format_employee_status(employees: list[dict]) -> str:
    """Format employee status for prompt."""
    if not employees:
        return "No employees available."

    lines = [
        "| Employee | Department | Status | Capabilities |",
        "|----------|------------|--------|--------------|",
    ]

    for e in employees:
        emp_id = e.get("id", "?")
        dept = e.get("department", "?")
        status = e.get("status", "?")
        caps = ", ".join(e.get("capabilities", [])[:3])

        lines.append(f"| {emp_id} | {dept} | {status} | {caps} |")

    return "\n".join(lines)


def format_initiatives(initiatives: list[dict]) -> str:
    """Format active initiatives for prompt."""
    if not initiatives:
        return "No active initiatives."

    lines = [
        "| Initiative | Goal | Status | Owner |",
        "|------------|------|--------|-------|",
    ]

    for i in initiatives:
        title = i.get("title", "?")[:30]
        goals = ", ".join(i.get("goal_alignment", []))
        status = i.get("status", "?")
        owner = i.get("owner", "?")

        lines.append(f"| {title} | {goals} | {status} | {owner} |")

    return "\n".join(lines)


def format_pending_plans(
    ceo_plans: list[dict], board_plans: list[dict], sessions: list[dict]
) -> str:
    """Format pending plans for executive prompt (P20)."""
    lines = []

    if ceo_plans:
        lines.append("**Awaiting CEO Review:**")
        lines.append("| Plan ID | Title | Type | Proposed By |")
        lines.append("|---------|-------|------|-------------|")
        for p in ceo_plans:
            plan_id = p.get("plan_id", "?")
            title = p.get("title", "?")[:40]
            plan_type = p.get("plan_type", "?")
            proposed_by = p.get("proposed_by", "?")
            lines.append(f"| {plan_id} | {title} | {plan_type} | {proposed_by} |")
        lines.append("")

    if board_plans:
        lines.append("**Awaiting Board Review:**")
        lines.append("| Plan ID | Title | CEO Decision |")
        lines.append("|---------|-------|--------------|")
        for p in board_plans:
            plan_id = p.get("plan_id", "?")
            title = p.get("title", "?")[:40]
            ceo_decision = p.get("ceo_decision", "pending")
            lines.append(f"| {plan_id} | {title} | {ceo_decision} |")
        lines.append("")

    if sessions:
        lines.append("**Active Board Sessions:**")
        lines.append("| Session ID | Status | Agenda |")
        lines.append("|------------|--------|--------|")
        for s in sessions:
            session_id = s.get("session_id", "?")
            status = s.get("status", "?")
            agenda = ", ".join(s.get("agenda_items", []))[:30]
            lines.append(f"| {session_id} | {status} | {agenda} |")
        lines.append("")

    if not lines:
        return "No pending planning reviews."

    return "\n".join(lines)


def format_planning_status(planning_context: dict) -> str:
    """Format planning cycle status for executive prompt (P29)."""
    lines = []

    if planning_context.get("weekly_due"):
        days = planning_context.get("days_since_weekly")
        if days is not None:
            lines.append(f"- **Weekly planning cycle DUE** ({days} days since last)")
        else:
            lines.append("- **Weekly planning cycle DUE** (never run)")
    else:
        days = planning_context.get("days_since_weekly")
        if days is not None:
            lines.append(
                f"- Weekly planning: {days} days since last (due in {7 - days:.1f} days)"
            )

    if planning_context.get("daily_due"):
        hours = planning_context.get("hours_since_daily")
        if hours is not None:
            lines.append(f"- **Daily planning cycle DUE** ({hours} hours since last)")
        else:
            lines.append("- **Daily planning cycle DUE** (never run)")
    else:
        hours = planning_context.get("hours_since_daily")
        if hours is not None:
            lines.append(
                f"- Daily planning: {hours} hours since last (due in {24 - hours:.1f} hours)"
            )

    if not lines:
        return "No planning cycles pending."

    return "\n".join(lines)


def build_executive_prompt(
    context: ExecutiveContext, situation: str, planning_context: dict | None = None
) -> str:
    """Build the full prompt for executive invocation."""
    queue = context.queue_status

    # Format planning status if context provided
    planning_status = ""
    if planning_context:
        planning_status = f"""
### Planning Cycles (P29)
{format_planning_status(planning_context)}
"""

    return f"""{context.agent_definition}

## Your Current Memory
{context.memory_content}

## Current Company State

### Goal Progress
{format_goal_assessments(context.goal_assessments)}

### Work Queue Status
- Pending tasks: {queue.get("pending", 0)}
- In progress: {queue.get("in_progress", 0)}
- Blocked: {queue.get("blocked", 0)}
- Completed today: {queue.get("completed_today", 0)}
- Total in queue: {queue.get("total", 0)}

### Employee Status
{format_employee_status(context.employee_status)}

### Active Initiatives
{format_initiatives(context.active_initiatives)}

### Planning Authority (P20)
{format_pending_plans(context.pending_plans_ceo, context.pending_plans_board, context.active_board_sessions)}
{planning_status}
## Situation
{situation}

## Your Task

As {context.role}, assess the current state and decide what work should be prioritized.

You may:
1. **Submit work** — Create tasks for the work queue
2. **Delegate** — Assign strategic direction to department heads
3. **Escalate** — Flag items requiring human attention
4. **Update vision** — Propose changes to company direction (requires approval)
5. **Plan decision** — Approve, revise, or reject pending plans (CEO only)
6. **Board vote** — Cast vote in active board sessions
7. **Planning trigger** — Trigger weekly or daily planning cycle when due

## Output Format

Respond with structured decisions in this format:

### Analysis
[Your strategic analysis of the current situation - 2-3 sentences]

### Decisions

#### Decision 1
- **Type:** submit_work | delegate | escalate | update_vision | plan_decision | board_vote | planning_trigger
- **Description:** [What you're deciding]
- **Rationale:** [Why this is the right call]
- **Priority:** 1-5 (5 = critical)
- **Requires Approval:** yes | no
- **Work Items:** (if submit_work type)
  - Task: [task description]
    Capabilities: [comma-separated required skills]
    Priority: [1-5]
- **Plan Decision:** (if plan_decision type)
  - Plan ID: [plan-xxx]
  - Action: approve | revise | reject
  - Comments: [feedback or reason]
- **Board Vote:** (if board_vote type)
  - Session ID: [board-xxx]
  - Plan ID: [plan-xxx]
  - Vote: approve | revise | reject
  - Comments: [rationale]
- **Planning Trigger:** (if planning_trigger type)
  - Cycle: weekly | daily
  - Comments: [optional context]

#### Decision 2
[Continue if more decisions needed]

### Summary
[1-2 sentence summary of actions taken]
"""


# -----------------------------------------------------------------------------
# Executive Invocation
# -----------------------------------------------------------------------------


def invoke_executive(
    executive_id: str,
    context: ExecutiveContext,
    situation: str,
    timeout: int = 300,
) -> tuple[str, int, float, int]:
    """
    Invoke Claude with executive context.

    Returns:
        Tuple of (output, exit_code, duration_seconds, prompt_chars)
    """
    _ensure_imports()

    # P29: Get planning context for executives
    planning_context = None
    try:
        company_dir = company_resolver.get_company_dir()
        if company_dir:
            planning_context = get_pending_planning_context(company_dir)
    except Exception:
        pass

    prompt = build_executive_prompt(context, situation, planning_context)
    prompt_chars = len(prompt)

    # Get project root for cwd
    project_root = company_resolver.find_company_root()
    if not project_root:
        project_root = Path.cwd()

    start_time = time.time()

    try:
        # Use -p flag for non-interactive print mode
        # The prompt is passed as a positional argument after the flag
        # stdin must be closed to prevent Claude from waiting for input
        # See: https://code.claude.com/docs/en/cli-reference.md
        exec_config = get_executive_loop_config()
        # WS-040: Resolve executive model from profile, then config override
        model = exec_config.get("model", "claude-sonnet-5")
        try:
            from employee_activator import load_config as _load_forge_config

            _fc = _load_forge_config()
            _profile_name = _fc.get("modelProfile")
            if _profile_name:
                _profiles = _fc.get("modelProfiles", {}).get("profiles", {})
                _profile = _profiles.get(_profile_name, {})
                if "executive" in _profile:
                    model = _profile["executive"]
            # Explicit executiveLoop.model still overrides profile
            if "model" in exec_config:
                model = exec_config["model"]
        except Exception:
            pass  # Fall back to default if config resolution fails
        # P54 FIX: Strip ALL Claude-related env vars to prevent nested session detection.
        # Previously only stripped CLAUDECODE (PR #124), but Claude CLI also checks
        # CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_MAX_OUTPUT_TOKENS, and potentially others.
        import os as _os

        child_env = dict(_os.environ)

        # Remove problematic env vars (P54/P57/P71 fixes)
        problematic_prefixes = ("UV_", "VIRTUAL_ENV", "CLAUDECODE", "CLAUDE_CODE_")
        for key in list(child_env.keys()):
            if key.startswith(problematic_prefixes):
                del child_env[key]

        # Clean PATH of UV-managed environments
        original_path = child_env.get("PATH", "")
        clean_path_parts = [
            p
            for p in original_path.split(":")
            if not p.startswith(_os.path.expanduser("~/.cache/uv/environments"))
            and "virtualenv" not in p.lower()
        ]
        child_env["PATH"] = (
            ":".join(clean_path_parts) if clean_path_parts else "/usr/bin:/bin"
        )

        # Set safe terminal defaults
        child_env["TERM"] = "xterm-256color"
        child_env["LANG"] = "en_US.UTF-8"
        child_env = {k: v for k, v in child_env.items() if v}

        # 2026-07-06 fork-bomb guard: executive invocation launches a real claude.
        assert_spawn_allowed("executive_loop.invoke_executive", subprocess.run)

        result = subprocess.run(
            [
                "uv",
                "run",
                "claude",
                "--model",
                model,
                "--setting-sources",
                "user",
                "-p",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,  # Close stdin to prevent blocking
            env=child_env,
        )
        duration = time.time() - start_time

        return result.stdout, result.returncode, duration, prompt_chars

    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return (
            f"Executive invocation timed out after {timeout}s",
            124,
            duration,
            prompt_chars,
        )

    except Exception as e:
        duration = time.time() - start_time
        return f"Executive invocation failed: {e}", 1, duration, prompt_chars


# -----------------------------------------------------------------------------
# Decision Parsing
# -----------------------------------------------------------------------------


def parse_decision_section(section: str) -> ExecutiveDecision | None:
    """Parse a single decision section into an ExecutiveDecision."""
    try:
        # Extract type
        type_match = re.search(r"\*\*Type:\*\*\s*(\w+)", section)
        decision_type = type_match.group(1) if type_match else "unknown"

        # Extract description
        desc_match = re.search(r"\*\*Description:\*\*\s*(.+?)(?:\n|$)", section)
        description = desc_match.group(1).strip() if desc_match else ""

        # Extract rationale
        rat_match = re.search(r"\*\*Rationale:\*\*\s*(.+?)(?:\n|$)", section)
        rationale = rat_match.group(1).strip() if rat_match else ""

        # Extract priority
        pri_match = re.search(r"\*\*Priority:\*\*\s*(\d)", section)
        priority = int(pri_match.group(1)) if pri_match else 3

        # Extract approval requirement
        app_match = re.search(r"\*\*Requires Approval:\*\*\s*(yes|no)", section, re.I)
        requires_approval = app_match.group(1).lower() == "yes" if app_match else False

        # Extract work items if submit_work type
        work_items = []
        if decision_type == "submit_work":
            # Find work items section
            items_match = re.search(
                r"\*\*Work Items:\*\*(.*?)(?=####|\Z)", section, re.DOTALL
            )
            if items_match:
                items_text = items_match.group(1)
                # Parse each task
                task_pattern = r"-\s*Task:\s*(.+?)(?:\n\s*Capabilities:\s*(.+?))?(?:\n\s*Priority:\s*(\d))?(?=\n\s*-|\Z)"
                for task_match in re.finditer(task_pattern, items_text, re.DOTALL):
                    task_desc = task_match.group(1).strip()
                    caps_str = task_match.group(2) or ""
                    task_pri = (
                        int(task_match.group(3)) if task_match.group(3) else priority
                    )

                    capabilities = [c.strip() for c in caps_str.split(",") if c.strip()]

                    work_items.append(
                        {
                            "description": task_desc,
                            "capabilities": capabilities,
                            "priority": task_pri,
                        }
                    )

        # P20: Extract plan decision fields
        plan_id = None
        plan_action = None
        plan_comments = None
        session_id = None

        if decision_type in ("plan_decision", "board_vote"):
            # Extract Plan ID
            plan_id_match = re.search(r"Plan ID:\s*(plan-[a-zA-Z0-9-]+)", section)
            if plan_id_match:
                plan_id = plan_id_match.group(1)

            # Extract Session ID (for board_vote)
            session_id_match = re.search(
                r"Session ID:\s*(board-[a-zA-Z0-9-]+)", section
            )
            if session_id_match:
                session_id = session_id_match.group(1)

            # Extract Action/Vote
            action_match = re.search(
                r"(?:Action|Vote):\s*(approve|revise|reject)", section, re.I
            )
            if action_match:
                plan_action = action_match.group(1).lower()

            # Extract Comments
            comments_match = re.search(
                r"Comments:\s*(.+?)(?=\n\s*-|\n####|\Z)", section, re.DOTALL
            )
            if comments_match:
                plan_comments = comments_match.group(1).strip()

        # P29: Extract planning trigger fields
        planning_cycle = None
        if decision_type == "planning_trigger":
            cycle_match = re.search(r"Cycle:\s*(weekly|daily)", section, re.I)
            if cycle_match:
                planning_cycle = cycle_match.group(1).lower()

            # Also extract comments if present
            comments_match = re.search(
                r"Comments:\s*(.+?)(?=\n\s*-|\n####|\Z)", section, re.DOTALL
            )
            if comments_match:
                plan_comments = comments_match.group(1).strip()

        if not description:
            return None

        return ExecutiveDecision(
            decision_type=decision_type,
            description=description,
            rationale=rationale,
            work_items=work_items,
            requires_approval=requires_approval,
            priority=priority,
            plan_id=plan_id,
            session_id=session_id,
            plan_action=plan_action,
            plan_comments=plan_comments,
            planning_cycle=planning_cycle,
        )

    except Exception:
        return None


def parse_executive_output(output: str) -> list[ExecutiveDecision]:
    """Parse natural language executive output into structured decisions."""
    decisions = []

    # Split by "#### Decision" sections
    decision_sections = re.split(r"####\s+Decision\s+\d+", output)

    for section in decision_sections[1:]:  # Skip pre-decision content
        decision = parse_decision_section(section)
        if decision:
            decisions.append(decision)

    return decisions


# -----------------------------------------------------------------------------
# Decision Processing
# -----------------------------------------------------------------------------


def _queue_work_items_for_approval(
    executive_id: str,
    decision: "ExecutiveDecision",
) -> int:
    """Queue work items for human approval via /pending.

    Returns number of items queued.
    """
    import os
    import tempfile
    import uuid

    company_dir = company_resolver.get_company_dir() if company_resolver else None
    if not company_dir:
        return 0

    pending_path = Path(company_dir) / "state/pending_approvals.json"

    try:
        if pending_path.exists():
            with open(pending_path, encoding="utf-8") as f:
                pending = json.load(f)
        else:
            pending = {"proposals": []}
    except (json.JSONDecodeError, OSError):
        pending = {"proposals": []}

    if "proposals" not in pending:
        pending["proposals"] = []

    existing_ids = {p.get("proposal_id") for p in pending["proposals"]}
    queued = 0

    for item in decision.work_items:
        proposal_id = f"exec-approval-{uuid.uuid4().hex[:12]}"
        desc = item.get("description", decision.description)
        title = (desc[:60] + "...") if len(desc) > 60 else desc

        proposal = {
            "proposal_id": proposal_id,
            "proposal_type": "executive_work_item",
            "title": title,
            "description": desc,
            "rationale": decision.rationale,
            "estimated_effort_minutes": 60,
            "estimated_value": 0.5,
            "roi_score": 1.0,
            "approval_tier": "executive",
            "approval_required": ["human"],
            "source_data": {
                "executive_id": executive_id,
                "decision_type": decision.decision_type,
                "work_item": item,
                "priority": item.get("priority", decision.priority),
                "capabilities": item.get("capabilities", []),
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "priority": item.get("priority", decision.priority),
        }

        if proposal_id not in existing_ids:
            pending["proposals"].append(proposal)
            existing_ids.add(proposal_id)
            queued += 1

    if queued > 0:
        fd, tmp_path = tempfile.mkstemp(dir=pending_path.parent, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pending, f, indent=2)
            os.replace(tmp_path, pending_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return queued


def _queue_vision_update_for_approval(
    executive_id: str,
    decision: "ExecutiveDecision",
) -> int:
    """Queue a vision update for human approval via /pending.

    Vision updates always require human approval and are tracked
    in pending_approvals.json for later review by humans.

    Returns 1 if queued, 0 if failed or no company dir.
    """
    import os
    import tempfile
    import uuid

    company_dir = company_resolver.get_company_dir() if company_resolver else None
    if not company_dir:
        return 0

    pending_path = Path(company_dir) / "state/pending_approvals.json"

    try:
        if pending_path.exists():
            with open(pending_path, encoding="utf-8") as f:
                pending = json.load(f)
        else:
            pending = {"proposals": []}
    except (json.JSONDecodeError, OSError):
        pending = {"proposals": []}

    if "proposals" not in pending:
        pending["proposals"] = []

    # Create a single vision update proposal
    proposal_id = f"vision-update-{uuid.uuid4().hex[:12]}"
    title = "Vision Update Review"
    if decision.description:
        title_content = (
            decision.description[:60] + "..."
            if len(decision.description) > 60
            else decision.description
        )
        title = f"Vision Update: {title_content}"

    proposal = {
        "proposal_id": proposal_id,
        "proposal_type": "vision_update",
        "title": title,
        "description": decision.description or "Vision update",
        "rationale": decision.rationale or "Executive decision",
        "estimated_effort_minutes": 30,
        "estimated_value": 0.8,
        "roi_score": 1.0,
        "approval_tier": "executive",
        "approval_required": ["human"],
        "source_data": {
            "executive_id": executive_id,
            "decision_type": "update_vision",
            "priority": decision.priority,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "priority": decision.priority,
    }

    # Check if this vision update already exists
    existing_ids = {p.get("proposal_id") for p in pending["proposals"]}
    if proposal_id in existing_ids:
        return 0

    pending["proposals"].append(proposal)

    # Atomic write with tempfile
    fd, tmp_path = tempfile.mkstemp(dir=pending_path.parent, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=2)
        os.replace(tmp_path, pending_path)
        return 1
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return 0


def submit_executive_work(
    executive_id: str,
    work_items: list[dict],
    max_items: int = 5,
) -> list[str]:
    """
    Submit work items to the queue on behalf of an executive.

    Returns list of created task IDs.
    """
    _ensure_imports()

    task_ids = []
    items_to_submit = work_items[:max_items]  # Rate limit

    for item in items_to_submit:
        try:
            desc = item.get("description", "")
            # Create a short title from the description
            title = desc[:60] + "..." if len(desc) > 60 else desc

            result = input_channel.submit_work_request(
                title=title,
                description=desc,
                priority=item.get("priority", 3),
                required_capabilities=item.get("capabilities", []),
                requested_by=executive_id,
            )
            if result and result.get("task_id"):
                task_ids.append(result["task_id"])
        except Exception:
            continue

    return task_ids


def process_executive_decisions(
    executive_id: str,
    decisions: list[ExecutiveDecision],
    config: dict,
) -> dict:
    """
    Process decisions made by an executive.

    Returns dict with processing results.
    """
    results = {
        "work_submitted": 0,
        "escalations": 0,
        "pending_approval": 0,
        "task_ids": [],
    }

    max_work_items = config.get("maxWorkItemsPerSession", 5)
    require_approval_for = config.get("requireApprovalFor", [])
    auto_approve_for = config.get("autoApproveFor", [])

    for decision in decisions:
        if decision.decision_type == "submit_work":
            # Check if work items need approval
            needs_approval = decision.requires_approval
            if decision.decision_type in require_approval_for:
                needs_approval = True

            if needs_approval and decision.decision_type not in auto_approve_for:
                queued = _queue_work_items_for_approval(executive_id, decision)
                results["pending_approval"] += queued
            else:
                # Submit work items
                task_ids = submit_executive_work(
                    executive_id,
                    decision.work_items,
                    max_items=max_work_items - results["work_submitted"],
                )
                results["work_submitted"] += len(task_ids)
                results["task_ids"].extend(task_ids)

        elif decision.decision_type == "escalate":
            results["escalations"] += 1
            # Create escalation via escalation.py
            try:
                _ensure_imports()
                if escalation is not None:
                    notes = f"{decision.description} — {decision.rationale}"
                    escalated_task_ids = []

                    # Escalate specific tasks if provided in work_items
                    for item in decision.work_items:
                        task_id = item.get("id") or item.get("task_id")
                        if task_id:
                            reason = item.get("reason", "explicit_block")
                            escalation.escalate(task_id, reason, notes)
                            escalated_task_ids.append(task_id)

                    # If no task IDs in work_items, escalate with description as context
                    if not escalated_task_ids:
                        escalation.escalate(
                            f"exec-{executive_id}-escalation",
                            "explicit_block",
                            notes,
                        )
            except Exception:
                pass

        elif decision.decision_type == "delegate":
            # Delegation creates work assigned to specific employee
            if decision.work_items:
                task_ids = submit_executive_work(
                    executive_id,
                    decision.work_items,
                    max_items=max_work_items - results["work_submitted"],
                )
                results["work_submitted"] += len(task_ids)
                results["task_ids"].extend(task_ids)

        elif decision.decision_type == "update_vision":
            # Vision updates always need approval
            queued = _queue_vision_update_for_approval(executive_id, decision)
            results["pending_approval"] += queued

        elif decision.decision_type == "plan_decision":
            # P20: CEO plan review decision
            if decision.plan_id and decision.plan_action:
                try:
                    # Import planning_authority module
                    try:
                        from . import planning_authority
                    except ImportError:
                        import planning_authority  # type: ignore[no-redef]

                    result = planning_authority.ceo_review(
                        plan_id=decision.plan_id,
                        decision=decision.plan_action,
                        comments=decision.plan_comments or "",
                        reviewer=executive_id,
                    )
                    if not result.get("error"):
                        if "plan_decisions" not in results:
                            results["plan_decisions"] = 0
                        results["plan_decisions"] += 1
                except Exception:
                    pass

        elif decision.decision_type == "board_vote":
            # P20: Board session vote
            if decision.session_id and decision.plan_id and decision.plan_action:
                try:
                    try:
                        from . import planning_authority
                    except ImportError:
                        import planning_authority  # type: ignore[no-redef]

                    result = planning_authority.record_board_decision(
                        session_id=decision.session_id,
                        executive_id=executive_id,
                        plan_id=decision.plan_id,
                        decision=decision.plan_action,
                        comments=decision.plan_comments or "",
                    )
                    if not result.get("error"):
                        if "board_votes" not in results:
                            results["board_votes"] = 0
                        results["board_votes"] += 1
                except Exception:
                    pass

        elif decision.decision_type == "planning_trigger":
            # P29: Trigger planning cycle
            if decision.planning_cycle:
                try:
                    company_dir = company_resolver.get_company_dir()
                    if company_dir:
                        if decision.planning_cycle == "weekly":
                            trigger_result = trigger_weekly_planning(company_dir)
                        elif decision.planning_cycle == "daily":
                            trigger_result = trigger_daily_planning(company_dir)
                        else:
                            trigger_result = {
                                "triggered": False,
                                "error": "unknown_cycle",
                            }

                        if trigger_result.get("triggered"):
                            if "planning_triggers" not in results:
                                results["planning_triggers"] = 0
                            results["planning_triggers"] += 1
                except Exception:
                    pass

    return results


# -----------------------------------------------------------------------------
# Memory Updates
# -----------------------------------------------------------------------------


def update_executive_memory(
    executive_id: str,
    session: ExecutiveSession,
) -> bool:
    """Update executive's memory with session record."""
    memory_path = get_executive_memory_path(executive_id)
    if not memory_path:
        return False

    try:
        # Format session entry
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        duration = f"{session.duration_seconds:.1f}"

        # Format decisions table
        decision_rows = []
        for i, dec in enumerate(session.decisions, 1):
            outcome = f"{len(dec.work_items)} tasks" if dec.work_items else "noted"
            decision_rows.append(
                f"| {i} | {dec.decision_type} | {dec.description[:30]} | {outcome} |"
            )

        decisions_table = (
            "\n".join(decision_rows)
            if decision_rows
            else "| - | - | No decisions made | - |"
        )

        # Format work submitted
        work_lines = []
        if hasattr(session, "task_ids") and session.task_ids:
            for task_id in session.task_ids[:5]:  # Limit display
                work_lines.append(f"- {task_id}")
        work_submitted = "\n".join(work_lines) if work_lines else "- None"

        entry = f"""

### {date} - Executive Session
**Duration:** {duration}s
**Trigger:** {session.trigger}

#### Decisions Made
| # | Type | Description | Outcome |
|---|------|-------------|---------|
{decisions_table}

#### Work Submitted
{work_submitted}

"""

        # Append to memory file
        current_content = memory_path.read_text(encoding="utf-8")

        # Insert after "## Recent Interactions" if it exists
        if "## Recent Interactions" in current_content:
            parts = current_content.split("## Recent Interactions", 1)
            new_content = (
                parts[0] + "## Recent Interactions\n" + entry + parts[1].lstrip("\n")
            )
        else:
            # Append at end
            new_content = (
                current_content.rstrip() + "\n\n## Recent Interactions" + entry
            )

        memory_path.write_text(new_content, encoding="utf-8")
        return True

    except Exception:
        return False


# -----------------------------------------------------------------------------
# Main Executive Loop
# -----------------------------------------------------------------------------


def run_single_executive(
    executive_id: str,
    situation: str,
    trigger: str = "manual",
) -> ExecutiveSession:
    """Run executive activation for a single executive."""
    _ensure_imports()

    config = get_executive_loop_config()
    started_at = datetime.now(timezone.utc).isoformat()

    # Load context
    context = load_executive_context(executive_id)
    if not context:
        return ExecutiveSession(
            executive_id=executive_id,
            started_at=started_at,
            duration_seconds=0,
            trigger=trigger,
            error=f"Could not load context for {executive_id}",
        )

    # Invoke executive
    output, exit_code, duration, prompt_chars = invoke_executive(
        executive_id=executive_id,
        context=context,
        situation=situation,
    )

    session = ExecutiveSession(
        executive_id=executive_id,
        started_at=started_at,
        duration_seconds=duration,
        trigger=trigger,
        output_raw=output,
    )

    if exit_code != 0:
        session.error = f"Invocation failed with exit code {exit_code}"
        # Still record token usage for failed invocations (WS-009-002)
        efficiency_tracker.record_executive_session(
            executive_id=executive_id,
            trigger=trigger,
            duration_seconds=duration,
            prompt_chars=prompt_chars,
            output_chars=len(output),
            decisions_count=0,
            work_submitted=0,
            success=False,
        )
        return session

    # Parse decisions
    decisions = parse_executive_output(output)
    session.decisions = decisions

    # Process decisions
    results = process_executive_decisions(executive_id, decisions, config)
    session.work_submitted = results["work_submitted"]
    session.escalations = results["escalations"]

    # Attach task IDs for memory update
    session.task_ids = results.get("task_ids", [])  # type: ignore

    # Update memory
    update_executive_memory(executive_id, session)

    # Record token usage (WS-009-002)
    efficiency_tracker.record_executive_session(
        executive_id=executive_id,
        trigger=trigger,
        duration_seconds=duration,
        prompt_chars=prompt_chars,
        output_chars=len(output),
        decisions_count=len(decisions),
        work_submitted=session.work_submitted,
        success=True,
    )

    return session


def run_executive_loop(
    trigger: str = "scheduled",
    situation: str | None = None,
) -> dict:
    """
    Run the full executive loop (CEO, then CTO).

    Returns dict with loop results.
    """
    _ensure_imports()

    config = get_executive_loop_config()

    if not config.get("enabled", True):
        return {
            "success": False,
            "error": "Executive loop is disabled",
            "executives_invoked": 0,
        }

    executives = config.get("executives", EXECUTIVE_ORDER)
    queue_status = load_queue_status()

    # Build situation description
    if not situation:
        pending = queue_status.get("pending", 0)
        blocked = queue_status.get("blocked", 0)
        completed_today = queue_status.get("completed_today", 0)

        if pending == 0 and blocked == 0:
            situation = "The work queue is empty. Assess company state and decide what work should be prioritized next."
        elif blocked > 0:
            situation = f"There are {blocked} blocked tasks. Assess blockers and decide how to unblock progress."
        else:
            situation = f"Regular check-in. {pending} tasks pending, {completed_today} completed today. Assess progress and priorities."

    results = {
        "success": True,
        "trigger": trigger,
        "executives_invoked": 0,
        "decisions_made": 0,
        "work_submitted": 0,
        "escalations": 0,
        "sessions": [],
    }

    for executive_id in executives:
        session = run_single_executive(
            executive_id=executive_id,
            situation=situation,
            trigger=trigger,
        )

        results["executives_invoked"] += 1
        results["decisions_made"] += len(session.decisions)
        results["work_submitted"] += session.work_submitted
        results["escalations"] += session.escalations
        results["sessions"].append(session.to_dict())

        # Update situation for next executive based on decisions
        if session.decisions and executive_id == "forge-ceo":
            # CTO should know what CEO decided
            ceo_decisions = ", ".join([d.description for d in session.decisions[:3]])
            situation = f"CEO has decided: {ceo_decisions}. Translate these strategic decisions into technical work."

    return results


def should_run_executive_loop(
    last_run: str | None,
    interval_hours: int = 4,
    trigger_on_empty: bool = True,
) -> tuple[bool, str]:
    """
    Check if executive loop should run.

    Returns:
        Tuple of (should_run, trigger_reason)
    """
    # Check if first run
    if not last_run:
        return True, "first_run"

    # Check interval
    try:
        last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        elapsed_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600

        if elapsed_hours >= interval_hours:
            return True, "scheduled"
    except (ValueError, TypeError):
        return True, "invalid_timestamp"

    # Check empty queue trigger
    if trigger_on_empty:
        queue_status = load_queue_status()
        pending = queue_status.get("pending", 0)
        in_progress = queue_status.get("in_progress", 0)

        if pending == 0 and in_progress == 0:
            return True, "empty_queue"

    return False, ""


# -----------------------------------------------------------------------------
# Planning Triggers (P29)
# -----------------------------------------------------------------------------


def _load_planning_timestamps(company_dir: Path) -> dict:
    """Load planning cycle timestamps from strategic_state.json."""
    state_path = company_dir / "state/strategic_state.json"
    if not state_path.exists():
        return {}

    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        return {
            "last_weekly_cycle": state.get("last_weekly_cycle"),
            "last_daily_cycle": state.get("last_daily_cycle"),
            "last_planning_run": state.get("last_planning_run"),
        }
    except Exception:
        return {}


def _save_planning_timestamp(company_dir: Path, key: str, timestamp: str) -> None:
    """Save a planning timestamp to strategic_state.json.

    WS-089-002: Uses atomic write (tempfile + os.replace) to prevent
    corruption when concurrent readers access the file during write.
    """
    import os
    import tempfile

    state_path = company_dir / "state/strategic_state.json"
    try:
        if state_path.exists():
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        else:
            state = {}

        state[key] = timestamp

        # Atomic write: write to temp file, then rename
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, str(state_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        pass


def trigger_weekly_planning(company_dir: Path) -> dict:
    """
    Check if weekly planning cycle is due and run if so.

    Returns:
        dict with "triggered" (bool) and "result" (planning result or None)
    """
    _ensure_imports()

    timestamps = _load_planning_timestamps(company_dir)
    last_weekly = timestamps.get("last_weekly_cycle")

    # Check if 7 days have passed
    should_run = False
    if not last_weekly:
        should_run = True
        reason = "first_weekly_run"
    else:
        try:
            last_dt = datetime.fromisoformat(last_weekly.replace("Z", "+00:00"))
            elapsed_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / (
                24 * 3600
            )
            if elapsed_days >= 7:
                should_run = True
                reason = f"interval_elapsed ({elapsed_days:.1f} days)"
        except (ValueError, TypeError):
            should_run = True
            reason = "invalid_timestamp"

    if not should_run:
        return {"triggered": False, "result": None, "reason": "not_due"}

    # Run weekly cycle (function added by Task 29.4)
    try:
        # Check if run_weekly_cycle exists (added by parallel task 29.4)
        if hasattr(strategic_planner, "run_weekly_cycle"):
            result = strategic_planner.run_weekly_cycle(company_dir)
        else:
            # Fallback to run_planning_cycle if weekly not yet available
            result = strategic_planner.run_planning_cycle(company_dir)

        # Update timestamp
        _save_planning_timestamp(
            company_dir, "last_weekly_cycle", datetime.now(timezone.utc).isoformat()
        )

        return {"triggered": True, "result": result, "reason": reason}

    except Exception as e:
        return {"triggered": False, "result": None, "error": str(e)}


def trigger_daily_planning(company_dir: Path) -> dict:
    """
    Check if daily planning cycle is due and run if so.

    Returns:
        dict with "triggered" (bool) and "result" (planning result or None)
    """
    _ensure_imports()

    timestamps = _load_planning_timestamps(company_dir)
    last_daily = timestamps.get("last_daily_cycle")

    # Check if 24 hours have passed
    should_run = False
    if not last_daily:
        should_run = True
        reason = "first_daily_run"
    else:
        try:
            last_dt = datetime.fromisoformat(last_daily.replace("Z", "+00:00"))
            elapsed_hours = (
                datetime.now(timezone.utc) - last_dt
            ).total_seconds() / 3600
            if elapsed_hours >= 24:
                should_run = True
                reason = f"interval_elapsed ({elapsed_hours:.1f} hours)"
        except (ValueError, TypeError):
            should_run = True
            reason = "invalid_timestamp"

    if not should_run:
        return {"triggered": False, "result": None, "reason": "not_due"}

    # Run daily cycle (function added by Task 29.4)
    try:
        # Check if run_daily_cycle exists (added by parallel task 29.4)
        if hasattr(strategic_planner, "run_daily_cycle"):
            result = strategic_planner.run_daily_cycle(company_dir)
        else:
            # Daily cycle not yet available - just log status
            result = {"message": "daily_cycle not yet implemented"}

        # Update timestamp
        _save_planning_timestamp(
            company_dir, "last_daily_cycle", datetime.now(timezone.utc).isoformat()
        )

        return {"triggered": True, "result": result, "reason": reason}

    except Exception as e:
        return {"triggered": False, "result": None, "error": str(e)}


def get_pending_planning_context(company_dir: Path) -> dict:
    """
    Get context about pending planning decisions for executive prompt.

    Returns:
        dict with planning status information
    """
    _ensure_imports()

    timestamps = _load_planning_timestamps(company_dir)
    now = datetime.now(timezone.utc)

    context = {
        "weekly_due": False,
        "daily_due": False,
        "days_since_weekly": None,
        "hours_since_daily": None,
    }

    # Check weekly cycle
    last_weekly = timestamps.get("last_weekly_cycle")
    if last_weekly:
        try:
            last_dt = datetime.fromisoformat(last_weekly.replace("Z", "+00:00"))
            elapsed_days = (now - last_dt).total_seconds() / (24 * 3600)
            context["days_since_weekly"] = round(elapsed_days, 1)
            context["weekly_due"] = elapsed_days >= 7
        except (ValueError, TypeError):
            context["weekly_due"] = True
    else:
        context["weekly_due"] = True

    # Check daily cycle
    last_daily = timestamps.get("last_daily_cycle")
    if last_daily:
        try:
            last_dt = datetime.fromisoformat(last_daily.replace("Z", "+00:00"))
            elapsed_hours = (now - last_dt).total_seconds() / 3600
            context["hours_since_daily"] = round(elapsed_hours, 1)
            context["daily_due"] = elapsed_hours >= 24
        except (ValueError, TypeError):
            context["daily_due"] = True
    else:
        context["daily_due"] = True

    return context


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Executive Loop — periodic activation of CEO/CTO"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run command
    run_parser = subparsers.add_parser("run", help="Run full executive loop")
    run_parser.add_argument(
        "--situation", type=str, help="Override situation description"
    )
    run_parser.add_argument(
        "--trigger",
        type=str,
        default="manual",
        choices=["scheduled", "empty_queue", "manual"],
        help="Trigger reason",
    )

    # invoke command
    invoke_parser = subparsers.add_parser("invoke", help="Invoke single executive")
    invoke_parser.add_argument(
        "--executive",
        type=str,
        required=True,
        help="Executive ID (e.g., forge-ceo)",
    )
    invoke_parser.add_argument("--situation", type=str, help="Situation description")

    # should-run command
    subparsers.add_parser("should-run", help="Check if loop should run")

    # context command
    context_parser = subparsers.add_parser("context", help="Show executive context")
    context_parser.add_argument(
        "--executive",
        type=str,
        required=True,
        help="Executive ID",
    )

    # config command
    subparsers.add_parser("config", help="Show executive loop configuration")

    args = parser.parse_args()

    if args.command == "run":
        result = run_executive_loop(
            trigger=args.trigger,
            situation=args.situation,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "invoke":
        situation = (
            args.situation or "Manual executive check-in. Assess state and priorities."
        )
        session = run_single_executive(
            executive_id=args.executive,
            situation=situation,
            trigger="manual",
        )
        print(json.dumps(session.to_dict(), indent=2, default=str))

    elif args.command == "should-run":
        config = get_executive_loop_config()
        # In real usage, last_run would come from daemon state
        should_run, reason = should_run_executive_loop(
            last_run=None,  # Assume first run for CLI test
            interval_hours=config.get("intervalHours", 4),
            trigger_on_empty=config.get("triggerOnEmptyQueue", True),
        )
        print(
            json.dumps(
                {
                    "should_run": should_run,
                    "reason": reason,
                },
                indent=2,
            )
        )

    elif args.command == "context":
        context = load_executive_context(args.executive)
        if context:
            print(f"Executive: {context.executive_id}")
            print(f"Role: {context.role}")
            print(f"Name: {context.name}")
            print(f"\nAgent Definition: {len(context.agent_definition)} chars")
            print(f"Memory: {len(context.memory_content)} chars")
            print(f"\nGoals: {len(context.goal_assessments)} assessments")
            print(f"Queue: {context.queue_status}")
            print(f"Employees: {len(context.employee_status)} available")
            print(f"Initiatives: {len(context.active_initiatives)} active")
        else:
            print(f"Could not load context for {args.executive}")
            sys.exit(1)

    elif args.command == "config":
        config = get_executive_loop_config()
        print(json.dumps(config, indent=2))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
