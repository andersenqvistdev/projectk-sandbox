# /pending — List Items Requiring Human Attention

Display all items requiring human attention, including escalations awaiting response and blocked tasks that need intervention.

## Input
$ARGUMENTS

## Command Syntax

```
/pending                    # Show all pending items
/pending --type escalations # Show only escalations
/pending --type blocked     # Show only blocked tasks
/pending --type proposals   # Show only proactive proposals
```

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the filter mode:

| Pattern | Mode | Filter |
|---------|------|--------|
| (empty) | ALL | Show all pending items |
| `--type escalations` | ESCALATIONS | Only Tier 4 escalations |
| `--type blocked` | BLOCKED | Only blocked tasks |
| `--type proposals` | PROPOSALS | Only proactive proposals |

**If empty arguments:** Show all pending items
**If `--type` flag provided:** Filter to specified type

---

## Step 1: Gather Pending Data

### 1.1: Get Escalations Requiring Human Attention

Call the input channel to get Tier 4 (Human) escalations:

```bash
uv run .claude/hooks/company/input_channel.py pending
```

Parse the JSON response:

```json
{
  "success": true,
  "escalations": [...],
  "count": 2,
  "tier_info": {
    "tier": 4,
    "name": "Human",
    "description": "Escalations requiring human intervention"
  },
  "queried_at": "2025-02-09T..."
}
```

Store `escalations` list and `count`.

### 1.2: Get Blocked Tasks (if not filtering to escalations only)

If not filtering to `--type escalations`, also get blocked tasks:

```bash
uv run .claude/hooks/company/work_allocator.py list --status blocked
```

Parse the JSON response:

```json
{
  "success": true,
  "blocked": [...],
  "counts": {
    "blocked": 3
  }
}
```

Store `blocked` tasks list.

### 1.3: Get Proactive Proposals (if not filtering to escalations/blocked only)

If not filtering to `--type escalations` or `--type blocked`, also get proactive proposals:

```bash
uv run .claude/hooks/company/initiative_engine.py pending --json
```

Parse the JSON response:

```json
{
  "proposals": [
    {
      "proposal_id": "prop-XXX-...",
      "proposal_type": "employee_reassignment",
      "title": "Review Idle Employees (10)",
      "roi_score": 1.6,
      "estimated_effort_minutes": 15,
      "created_at": "2026-02-12T..."
    }
  ],
  "count": 1
}
```

Store `proposals` list.

---

## Step 2: Apply Filters

### 2.1: Filter by Type

| Flag | Include Escalations | Include Blocked | Include Proposals |
|------|---------------------|-----------------|-------------------|
| (none) | Yes | Yes | Yes |
| `--type escalations` | Yes | No | No |
| `--type blocked` | No | Yes | No |
| `--type proposals` | No | No | Yes |

---

## Step 3: Render Output

### 3.1: Header

```
================================================================================
  ITEMS REQUIRING HUMAN ATTENTION                           [pending review]
================================================================================

Generated: YYYY-MM-DD HH:MM UTC
```

### 3.2: Summary Section

```
### Summary

| Type | Count | Priority |
|------|-------|----------|
| Escalations (Tier 4) | [count] | Requires immediate response |
| Proactive Proposals | [count] | Review ROI-based suggestions |
| Blocked Tasks | [count] | May need unblocking action |

Total items pending: [total_count]
```

### 3.3: Escalations Section (if included)

**If escalations exist:**

```
================================================================================
  ESCALATIONS AWAITING RESPONSE                              [tier 4: human]
================================================================================

These escalations have reached Tier 4 and require human intervention to proceed.

+------------------------------------------------------------------------------+
|  ESCALATION QUEUE                                                            |
+------------------------------------------------------------------------------+

| # | Task ID | Title | Reason | Escalated At | Attempts |
|---|---------|-------|--------|--------------|----------|
| 1 | [id] | [title] | [reason] | [timestamp] | [count] |
| 2 | [id] | [title] | [reason] | [timestamp] | [count] |

### Escalation Details

#### 1. [task_id]: [title]

**Reason:** [escalation_reason]
**Escalated By:** [escalated_by]
**Escalated At:** [timestamp]
**Original Assignment:** [original_assignee]
**Attempts:** [attempt_count]

**Context:**
[escalation context if available]

**Suggested Action:**
- Review the escalation reason and context
- Provide guidance, approval, or reassignment decision
- Use `/respond [task_id] "your response"` to resolve

---

#### 2. [task_id]: [title]
...
```

**If no escalations:**

```
================================================================================
  ESCALATIONS AWAITING RESPONSE                              [tier 4: human]
================================================================================

No escalations currently require human attention.

All work is being handled within Tiers 1-3 (Peer, Department, Coordinator).
```

### 3.35: Proactive Proposals Section (if included)

**If proactive proposals exist:**

```
================================================================================
  PROACTIVE PROPOSALS                                    [initiative engine]
================================================================================

These proposals were generated by the Proactive Initiative Engine (P13).
Review and approve/reject based on ROI and priority.

+------------------------------------------------------------------------------+
|  PROPOSAL QUEUE                                                              |
+------------------------------------------------------------------------------+

| # | ID | Type | Title | ROI | Effort | Created |
|---|-----|------|-------|-----|--------|---------|
| 1 | [id] | [type] | [title] | [roi] | [min]min | [timestamp] |
| 2 | [id] | [type] | [title] | [roi] | [min]min | [timestamp] |

### Proposal Details

#### 1. [proposal_id]: [title]

**Type:** [proposal_type]
**ROI Score:** [roi_score] (value/effort ratio)
**Estimated Effort:** [estimated_effort_minutes] minutes
**Created:** [created_at]

**Rationale:**
[rationale text]

**Suggested Action:**
- To approve: `/respond [proposal_id] approve`
- To reject: `/respond [proposal_id] reject "reason"`

---

#### 2. [proposal_id]: [title]
...
```

**If no proactive proposals:**

```
================================================================================
  PROACTIVE PROPOSALS                                    [initiative engine]
================================================================================

No proactive proposals currently await approval.

The initiative engine auto-approves low-risk proposals. Only higher-risk
proposals that require human judgment appear here.

Run `/proactive` to manually trigger an opportunity scan.
```

### 3.4: Blocked Tasks Section (if included)

**If blocked tasks exist:**

```
================================================================================
  BLOCKED TASKS                                               [needs attention]
================================================================================

These tasks are blocked and may require human intervention to unblock.

+------------------------------------------------------------------------------+
|  BLOCKED QUEUE                                                               |
+------------------------------------------------------------------------------+

| # | Task ID | Title | Dependencies | Blocked Since |
|---|---------|-------|--------------|---------------|
| 1 | [id] | [title] | [dep_count] deps | [timestamp] |
| 2 | [id] | [title] | [dep_count] deps | [timestamp] |

### Blocked Task Details

#### 1. [task_id]: [title]

**Priority:** [priority] ([priority_label])
**Department:** [department]
**Blocked Since:** [timestamp]
**Dependencies:**
- [dep-1]: [status]
- [dep-2]: [status]

**Suggested Action:**
- Check if blocking dependencies can be prioritized
- Consider manual unblocking if dependencies are external
- Use `/submit` to add work that resolves blockers

---

#### 2. [task_id]: [title]
...
```

**If no blocked tasks:**

```
================================================================================
  BLOCKED TASKS                                               [needs attention]
================================================================================

No tasks are currently blocked in the work queue.

All tasks have their dependencies satisfied.
```

### 3.5: Actions Section

```
================================================================================
  SUGGESTED ACTIONS
================================================================================

### For Escalations

| Action | Command | When to Use |
|--------|---------|-------------|
| Respond | `/respond [task-id] "message"` | Provide guidance or approval |
| View Details | `/respond` | See full escalation queue |
| Check Status | `/company-status` | Overall company health |

### For Blocked Tasks

| Action | Command | When to Use |
|--------|---------|-------------|
| Submit Work | `/submit "unblock [task]"` | Add work to resolve blockers |
| Check Queue | `/company-status` | See full work queue status |
| View Employee | `/employee-status` | Check who can help |

### Quick Commands

- `/respond` - Respond to escalations
- `/submit` - Submit new work requests
- `/dashboard` - Quick operational snapshot
- `/company-status` - Full company status

================================================================================
```

---

## Step 4: Empty State

If no escalations AND no blocked tasks:

```
================================================================================
  NO ITEMS REQUIRING HUMAN ATTENTION                          [all clear]
================================================================================

There are no items currently requiring human attention.

### What This Means

- No escalations have reached Tier 4 (Human)
- No tasks are blocked waiting on dependencies
- All work is progressing normally

### Check Status

/dashboard          - Quick operational snapshot
/company-status     - Full company status
/employee-status    - Check employee workloads

================================================================================
```

---

## Step 5: Error Handling

### 5.1: Invalid Type Filter

If `--type` value is not `escalations` or `blocked`:

```
## Invalid Type Filter

The --type flag accepts: escalations, blocked

**Usage:**
  /pending                     # Show all pending items
  /pending --type escalations  # Show only escalations
  /pending --type blocked      # Show only blocked tasks

You provided: --type [value]
```

### 5.2: Input Channel Not Found

```
## Input Channel Not Found

The input channel script was not found at:
  .claude/hooks/company/input_channel.py

This may indicate:
1. Forge is not properly installed
2. The hooks directory is missing

**To fix:**
1. Verify Forge installation
2. Check that .claude/hooks/company/ exists
3. Run `/prime` to reload project context
```

### 5.3: Work Allocator Error

```
## Work Allocator Error

Failed to retrieve blocked tasks from the work queue.

**Error:** [error_message]

**Partial Results:**
Escalations are still shown if available.

**To troubleshoot:**
1. Check .company/work_queue.json exists
2. Verify file permissions
3. Run `/company-status` to check company health
```

### 5.4: Company Not Initialized

```
## Company Not Initialized

The company structure has not been initialized.

Pending items require an active company with:
- Work queue for blocked tasks
- Escalation system for human-tier items

**To initialize:**
/company-init       # Single-project company
/company-bootstrap  # Intelligent setup with templates
```

---

## Examples

### Example 1: Show All Pending Items

```
/pending

================================================================================
  ITEMS REQUIRING HUMAN ATTENTION                           [pending review]
================================================================================

Generated: 2025-02-09 15:30 UTC

### Summary

| Type | Count | Priority |
|------|-------|----------|
| Escalations (Tier 4) | 2 | Requires immediate response |
| Blocked Tasks | 1 | May need unblocking action |

Total items pending: 3

================================================================================
  ESCALATIONS AWAITING RESPONSE                              [tier 4: human]
================================================================================

These escalations have reached Tier 4 and require human intervention to proceed.

+------------------------------------------------------------------------------+
|  ESCALATION QUEUE                                                            |
+------------------------------------------------------------------------------+

| # | Task ID | Title | Reason | Escalated At | Attempts |
|---|---------|-------|--------|--------------|----------|
| 1 | task-abc123 | Implement OAuth | capability_mismatch | 2h ago | 3 |
| 2 | task-def456 | DB migration | explicit_block | 45m ago | 2 |

### Escalation Details

#### 1. task-abc123: Implement OAuth integration

**Reason:** capability_mismatch
**Escalated By:** eng-001
**Escalated At:** 2025-02-09T13:30:00Z
**Original Assignment:** eng-001
**Attempts:** 3

**Context:**
Requires OAuth expertise not available in current team.

**Suggested Action:**
- Review the escalation reason and context
- Provide guidance, approval, or reassignment decision
- Use `/respond task-abc123 "your response"` to resolve

---

#### 2. task-def456: Database migration blocked

**Reason:** explicit_block
**Escalated By:** eng-002
**Escalated At:** 2025-02-09T14:45:00Z
**Original Assignment:** eng-002
**Attempts:** 2

**Context:**
Production database access requires DBA approval.

**Suggested Action:**
- Review the escalation reason and context
- Provide guidance, approval, or reassignment decision
- Use `/respond task-def456 "your response"` to resolve

================================================================================
  BLOCKED TASKS                                               [needs attention]
================================================================================

These tasks are blocked and may require human intervention to unblock.

+------------------------------------------------------------------------------+
|  BLOCKED QUEUE                                                               |
+------------------------------------------------------------------------------+

| # | Task ID | Title | Dependencies | Blocked Since |
|---|---------|-------|--------------|---------------|
| 1 | task-ghi789 | Deploy to staging | 2 deps | 3h ago |

### Blocked Task Details

#### 1. task-ghi789: Deploy to staging

**Priority:** 2 (High)
**Department:** engineering
**Blocked Since:** 2025-02-09T12:30:00Z
**Dependencies:**
- task-abc123: in_progress (escalated)
- task-jkl012: completed

**Suggested Action:**
- Check if blocking dependencies can be prioritized
- Consider manual unblocking if dependencies are external
- Use `/submit` to add work that resolves blockers

================================================================================
  SUGGESTED ACTIONS
================================================================================

### Quick Commands

- `/respond task-abc123 "message"` - Respond to OAuth escalation
- `/respond task-def456 "message"` - Respond to DB migration escalation
- `/dashboard` - Quick operational snapshot

================================================================================
```

### Example 2: Filter to Escalations Only

```
/pending --type escalations

================================================================================
  ITEMS REQUIRING HUMAN ATTENTION                           [pending review]
================================================================================

Generated: 2025-02-09 15:30 UTC
Filter: escalations only

### Summary

| Type | Count | Priority |
|------|-------|----------|
| Escalations (Tier 4) | 2 | Requires immediate response |

================================================================================
  ESCALATIONS AWAITING RESPONSE                              [tier 4: human]
================================================================================

[... escalations content ...]

================================================================================
```

### Example 3: No Pending Items

```
/pending

================================================================================
  NO ITEMS REQUIRING HUMAN ATTENTION                          [all clear]
================================================================================

There are no items currently requiring human attention.

### What This Means

- No escalations have reached Tier 4 (Human)
- No tasks are blocked waiting on dependencies
- All work is progressing normally

### Check Status

/dashboard          - Quick operational snapshot
/company-status     - Full company status
/employee-status    - Check employee workloads

================================================================================
```

---

## Rules

1. **Always query both sources.** Get escalations from input_channel and blocked tasks from work_allocator (unless filtered).

2. **Group by type.** Clearly separate escalations from blocked tasks with distinct headers.

3. **Show suggested actions.** Every item should have actionable next steps.

4. **Include task IDs prominently.** Users need task IDs to respond or reference items.

5. **Show context when available.** Include escalation reasons, dependencies, and history.

6. **Handle partial failures gracefully.** If one source fails, still show the other.

7. **Respect filters.** When `--type` is specified, only show that category.

8. **Include timestamps.** Show when items were escalated or blocked, in relative format when possible.

9. **Prioritize display.** Show most urgent items first (by escalation time or priority).

10. **Clear empty states.** When nothing is pending, confirm this clearly with next steps.
