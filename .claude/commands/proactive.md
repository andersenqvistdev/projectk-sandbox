# /proactive — Proactive Initiative Engine

Trigger the Proactive Initiative Engine to scan for improvement opportunities and optionally execute them.

## Input

<args>

Optional arguments:
- `--execute` — Approve and execute eligible proposals
- `--dry-run` — Preview what would happen without executing
- `--type=<type>` — Only scan for specific proposal type

## Overview

The Proactive Initiative Engine (P13) enables the system to **propose work autonomously**. Instead of waiting for human direction, it:

1. **Detects Opportunities** — Scans codebase, metrics, and queue for improvements
2. **Evaluates ROI** — Ranks proposals by value/effort ratio
3. **Routes for Approval** — Auto-approves low-risk, defers high-risk to humans
4. **Executes** — Converts approved proposals to work queue tasks

## Usage Examples

```bash
# Scan and display opportunities
/proactive

# Scan and execute eligible (auto-approve) proposals
/proactive --execute

# Preview execution without making changes
/proactive --execute --dry-run

# Only look for test coverage opportunities
/proactive --type=test_coverage_sprint
```

## Step 1: Run Initiative Engine Scan

```bash
uv run .claude/hooks/company/initiative_engine.py scan
```

If `--execute` flag is present:
```bash
uv run .claude/hooks/company/initiative_engine.py scan --execute
```

If `--dry-run` flag is present:
```bash
uv run .claude/hooks/company/initiative_engine.py scan --execute --dry-run
```

## Step 2: Display Results

Show the output in a formatted table:

```
═══════════════════════════════════════════════════════════════════════════════
 PROACTIVE SCAN                                              [<timestamp>]
═══════════════════════════════════════════════════════════════════════════════

 Opportunities Detected: <count>

 | # | Type | Title | ROI | Effort | Approval |
 |---|------|-------|-----|--------|----------|
 | 1 | test_coverage | Test Sprint: 45% → 50% | 0.75 | 120min | ✓ auto |
 | 2 | dependency | Minor Updates (3 pkgs) | 0.50 | 15min | ✓ auto |
 | 3 | employee | Review Idle (2) | 0.40 | 15min | ⏳ human |

 Auto-Approvable: <count>
 Needs Human Approval: <count>

═══════════════════════════════════════════════════════════════════════════════
```

## Step 3: Execute Results (if --execute)

If `--execute` flag was provided, show execution results:

```
═══════════════════════════════════════════════════════════════════════════════
 EXECUTION RESULTS
═══════════════════════════════════════════════════════════════════════════════

 Processed: <total>
 Approved: <count>
 Pending Human: <count>

 | Proposal | Status | Tasks Created |
 |----------|--------|---------------|
 | Test Sprint | ✓ Executed | task-xxx, task-yyy |
 | Minor Updates | ✓ Executed | task-zzz |
 | Review Idle | ⏳ Pending | — |

 Use /pending to review proposals awaiting approval.

═══════════════════════════════════════════════════════════════════════════════
```

## Proposal Types

| Type | Description | Default Approval |
|------|-------------|------------------|
| `test_coverage_sprint` | Coverage below threshold | Auto |
| `dependency_update_minor` | Minor version updates | Auto |
| `dependency_update_major` | Major version updates | Human |
| `task_investigation` | Repeated task failures | Auto |
| `employee_reassignment` | Idle employees | Config |
| `documentation_update` | Missing/outdated docs | Auto |
| `security_fix` | Security vulnerabilities | Auto (urgent) |
| `performance_optimization` | Performance issues | Config |

## Approval Tiers

| Tier | Behavior |
|------|----------|
| **AUTO_APPROVE** | Execute immediately, no human needed |
| **CONFIG_APPROVE** | Check config flag, execute if enabled |
| **HUMAN_APPROVE** | Always require human sign-off via /respond |

## Configuration

Settings in `.claude/forge-config.json`:

```json
{
  "proactive": {
    "enabled": true,
    "scanIntervalMinutes": 60,
    "autoApprove": {
      "testSprints": true,
      "minorDependencyUpdates": true,
      "majorDependencyUpdates": false,
      "employeeReassignment": false
    },
    "thresholds": {
      "testCoverageMinimum": 0.5,
      "idleEmployeeHours": 24
    },
    "limits": {
      "maxProposalsPerScan": 10,
      "maxAutoApprovePerHour": 5
    }
  }
}
```

## Integration with /pending

Proposals requiring human approval appear in `/pending`:

```bash
/pending  # Shows escalations AND proactive proposals
```

Use `/respond` to approve or reject:

```bash
/respond prop-TEST-xxx approve
/respond prop-TEST-xxx reject "Not needed right now"
```

## Daemon Integration

When the daemon is running, proactive scans happen automatically at the configured interval:

```bash
/daemon start  # Starts daemon with proactive scanning
/daemon status # Shows next scan time
```

## Rules

- **Scan conservatively.** Detectors should minimize false positives.
- **Rank by ROI.** Higher value/effort ratio = higher priority.
- **Respect rate limits.** Max 5 auto-approvals per hour by default.
- **Never auto-approve structural changes.** Major updates, reorgs, etc. need humans.
- **Log everything.** All proposals and decisions are tracked for audit.
