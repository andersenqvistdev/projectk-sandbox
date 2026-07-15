# /improve — Self-Improvement Loop Control

Trigger and manage the self-improvement system that detects gaps and proposes enhancements.

## Input

$ARGUMENTS

Supported subcommands:
- `scan` — Run detectors, show findings (no proposals created)
- `propose` — Generate proposals from latest scan
- `run [--dry-run]` — Full improvement cycle (detect -> propose -> submit)
- `status` — Show improvement system status
- `history [--days N]` — Show improvement cycle history

## Overview

The self-improvement loop (P30) enables the system to **improve itself** by:

1. **Detecting Gaps** — Analyzes codebase, patterns, and metrics for improvement opportunities
2. **Proposing Enhancements** — Converts detections into actionable capability proposals
3. **Submitting Proposals** — Routes proposals through initiative engine (respecting approval tiers)
4. **Learning** — Tracks cycle results and patterns over time

## Step 1: Parse Subcommand

Parse `$ARGUMENTS` to extract the subcommand and any flags:

```bash
subcommand=$(echo "$ARGUMENTS" | awk '{print $1}')
flag=$(echo "$ARGUMENTS" | awk '{print $2}')
days_value=$(echo "$ARGUMENTS" | grep -oE '\-\-days [0-9]+' | awk '{print $2}')
```

Store:
- `subcommand` — One of: `scan`, `propose`, `run`, `status`, `history`
- `dry_run` — Boolean (true if `--dry-run` flag present)
- `days` — Number of days for history (default: 30)

If no subcommand provided, default to `status`.

## Step 2: Validate Company Directory

Check that we're in a company-enabled project:

```bash
if [ ! -d ".company" ]; then
  echo "Error: No company structure found"
  exit 1
fi
```

If not found, display:

```
===============================================================================
 SELF-IMPROVEMENT                                                      [ERROR]
===============================================================================

No company structure found.

The self-improvement loop requires company mode to:
  - Track improvement cycles in .company/improvement_cycles.json
  - Submit proposals via the initiative engine
  - Access efficiency and pattern data

To initialize:
  /company-init

===============================================================================
```

Exit without further processing.

## Step 3: Execute Subcommand

### scan — Run Detectors

Run the improvement detector to scan for gaps and opportunities:

```bash
uv run .claude/hooks/company/improvement_detector.py scan
```

Display formatted output:

```
===============================================================================
 IMPROVEMENT SCAN                                            [YYYY-MM-DD HH:MM]
===============================================================================

 Detectors Run: 5

 DETECTIONS FOUND: 3

 | # | Type              | Severity | Description                      |
 |---|-------------------|----------|----------------------------------|
 | 1 | missing_test      | medium   | auth_service.py has 0% coverage  |
 | 2 | deprecated_api    | low      | Using old config.get() pattern   |
 | 3 | performance       | high     | N+1 query in user_list endpoint  |

 High: 1 | Medium: 1 | Low: 1

===============================================================================
 NEXT STEPS
   Generate proposals: /improve propose
   Run full cycle:     /improve run
===============================================================================
```

If no detections:

```
===============================================================================
 IMPROVEMENT SCAN                                            [YYYY-MM-DD HH:MM]
===============================================================================

 Detectors Run: 5

 DETECTIONS FOUND: 0

 No improvement opportunities detected.

 The system appears to be in good health.

===============================================================================
```

### propose — Generate Proposals

Generate enhancement proposals from the latest detections:

```bash
uv run .claude/hooks/company/capability_proposer.py generate
```

Display formatted output:

```
===============================================================================
 ENHANCEMENT PROPOSALS                                       [YYYY-MM-DD HH:MM]
===============================================================================

 Based on 3 detections

 PROPOSALS GENERATED: 2

 [PROP-001] Add Test Coverage for auth_service
   Type: CAPABILITY_ENHANCEMENT
   Size: medium | Approval: auto
   Priority: 70
   Tasks:
     1. Create test file tests/test_auth_service.py
     2. Add unit tests for login/logout
     3. Add integration tests for session handling
   Estimated Hours: 4

 [PROP-002] Fix N+1 Query in user_list
   Type: CAPABILITY_ENHANCEMENT
   Size: small | Approval: auto
   Priority: 85
   Tasks:
     1. Add prefetch_related to user query
     2. Verify query count reduced
   Estimated Hours: 2

===============================================================================
 Auto-Approvable: 2
 Needs Human Approval: 0

 NEXT STEPS
   Run full cycle to submit: /improve run
   Preview without submitting: /improve run --dry-run
===============================================================================
```

If no proposals generated:

```
===============================================================================
 ENHANCEMENT PROPOSALS                                       [YYYY-MM-DD HH:MM]
===============================================================================

 No proposals generated.

 Possible reasons:
   - No detections from last scan (run /improve scan first)
   - All detections below threshold for proposal generation
   - Similar proposals already pending

===============================================================================
```

### run — Full Improvement Cycle

Execute the complete cycle: detect -> propose -> submit:

```bash
# Normal run
uv run .claude/hooks/company/self_improvement_loop.py run

# Dry run (preview only)
uv run .claude/hooks/company/self_improvement_loop.py run --dry-run
```

Display formatted output for normal run:

```
===============================================================================
 IMPROVEMENT CYCLE                                           [YYYY-MM-DD HH:MM]
===============================================================================

 Cycle ID: cycle-20260226143052-a7b3f2

 PHASE 1: Detection
   Detectors run: 5
   Detections found: 3

 PHASE 2: Proposal Generation
   Proposals generated: 2

 PHASE 3: Submission
   Proposals submitted: 2
   Auto-approved: 1
   Pending human review: 1

===============================================================================
 RESULTS

   Submitted Proposals:
     [PROP-001] Add Test Coverage (auto-approved)
       -> Tasks created: WQ-P30-001-1, WQ-P30-001-2, WQ-P30-001-3
     [PROP-002] Fix N+1 Query (pending human approval)
       -> Awaiting review in /pending

===============================================================================
 NEXT STEPS
   View pending proposals: /pending
   Check initiative status: /strategy active
   View cycle history: /improve history
===============================================================================
```

Display formatted output for dry run:

```
===============================================================================
 IMPROVEMENT CYCLE (DRY RUN)                                 [YYYY-MM-DD HH:MM]
===============================================================================

 Cycle ID: cycle-20260226143052-a7b3f2 (preview)

 PHASE 1: Detection
   Detectors run: 5
   Detections found: 3

 PHASE 2: Proposal Generation
   Proposals generated: 2

 PHASE 3: Submission (SIMULATED)
   Would submit: 2 proposals
   Would auto-approve: 1
   Would require human review: 1

===============================================================================
 DRY RUN - No changes made

 Preview of what would happen:
   [PROP-001] Add Test Coverage -> Would auto-approve
   [PROP-002] Fix N+1 Query -> Would require human approval

 To execute for real: /improve run
===============================================================================
```

### status — Show System Status

Display the current state of the improvement system:

```bash
uv run .claude/hooks/company/self_improvement_loop.py status
```

Display formatted output:

```
===============================================================================
 SELF-IMPROVEMENT STATUS                                     [YYYY-MM-DD HH:MM]
===============================================================================

 CYCLE STATUS

   Last Cycle:          2026-02-25T14:30:52+00:00
   Cycles (30 days):    4
   Next Cycle Due:      2026-03-04 (7 day interval)

 STATISTICS (Last 30 Days)

   Detections:          12
   Proposals Generated: 8
   Proposals Submitted: 8
   Auto-Approved:       5
   Pending Human:       3

 PENDING PROPOSALS

   Currently Pending:   2
     - PROP-007: Refactor config loading
     - PROP-008: Add retry logic to API calls

 SHOULD RUN NEW CYCLE?

   Status: No
   Reason: Last cycle was 1d 2h ago. Next cycle in ~5d 22h.

===============================================================================
 ACTIONS
   Run manual cycle: /improve run
   View pending:     /pending
   View history:     /improve history
===============================================================================
```

If never run:

```
===============================================================================
 SELF-IMPROVEMENT STATUS                                     [YYYY-MM-DD HH:MM]
===============================================================================

 CYCLE STATUS

   Last Cycle:          Never run
   Cycles (30 days):    0

 STATISTICS (Last 30 Days)

   Detections:          0
   Proposals Generated: 0
   Proposals Submitted: 0
   Auto-Approved:       0
   Pending Human:       0

 SHOULD RUN NEW CYCLE?

   Status: Yes
   Reason: No previous cycles found. First run.

===============================================================================
 ACTIONS
   Run first cycle:  /improve run
   Preview cycle:    /improve run --dry-run
   Scan only:        /improve scan
===============================================================================
```

### history — Show Cycle History

Display past improvement cycles:

```bash
# Default: last 30 days
uv run .claude/hooks/company/self_improvement_loop.py history

# Custom days
uv run .claude/hooks/company/self_improvement_loop.py history --days 7
```

Display formatted output:

```
===============================================================================
 IMPROVEMENT HISTORY                                         [Last 30 Days]
===============================================================================

 Total Cycles: 4

 | Cycle ID                     | Date       | Detect | Propose | Submit | Auto |
 |------------------------------|------------|--------|---------|--------|------|
 | cycle-20260225143052-a7b3f2  | 2026-02-25 | 3      | 2       | 2      | 1    |
 | cycle-20260218091215-c4d2e1  | 2026-02-18 | 5      | 3       | 3      | 2    |
 | cycle-20260211120030-f8a9b0  | 2026-02-11 | 2      | 1       | 1      | 1    |
 | cycle-20260204083045-e2c3d4  | 2026-02-04 | 2      | 2       | 2      | 1    |

 AGGREGATES

   Total Detections:      12
   Total Proposals:       8
   Total Submitted:       8
   Total Auto-Approved:   5
   Improvement Rate:      62.5% (auto-approved / submitted)

===============================================================================
 VIEW DETAILS
   Cycle status: /improve status
   Run new cycle: /improve run
===============================================================================
```

If no history:

```
===============================================================================
 IMPROVEMENT HISTORY                                         [Last 30 Days]
===============================================================================

 No improvement cycles found in this time period.

 The improvement loop has not been run yet, or all cycles
 are older than the requested time window.

 To run your first cycle:
   /improve run

 To preview first:
   /improve run --dry-run

===============================================================================
```

## Error Handling

### Script Not Found

If the Python scripts don't exist:

```
===============================================================================
 SELF-IMPROVEMENT                                                      [ERROR]
===============================================================================

Required script not found: improvement_detector.py

The self-improvement system requires these scripts:
  - .claude/hooks/company/improvement_detector.py
  - .claude/hooks/company/capability_proposer.py
  - .claude/hooks/company/self_improvement_loop.py

These are part of P30 (Self-Improvement Loop).

Check that P30 tasks 30.1-30.4 are complete.

===============================================================================
```

### Module Import Error

If dependencies are missing:

```
===============================================================================
 SELF-IMPROVEMENT                                                      [ERROR]
===============================================================================

Failed to load required module.

Error: [error message from stderr]

Ensure all P30 dependencies are available:
  - improvement_detector
  - capability_proposer
  - initiative_engine

Run /improve status for diagnostic info.

===============================================================================
```

### Cycle Already Running

If another cycle is in progress:

```
===============================================================================
 SELF-IMPROVEMENT                                                  [IN PROGRESS]
===============================================================================

An improvement cycle is already running.

Current cycle: cycle-20260226143052-a7b3f2
Started: 2 minutes ago

Wait for the current cycle to complete, or check status:
  /improve status

===============================================================================
```

## Rules

- **Use `uv run` to execute** — Always invoke Python scripts via `uv run .claude/hooks/company/`
- **Respect approval tiers** — Auto-approve only low-risk improvements; defer others to humans
- **Log all cycles** — Every cycle is recorded in .company/improvement_cycles.json
- **Limit pending proposals** — Don't run new cycles if too many proposals are pending review
- **Dry run for safety** — Always support `--dry-run` to preview before committing
- **Show clear next steps** — Guide users to review pending, approve, or run again
- **Format timestamps consistently** — Use ISO format in logs, human-readable in display
- **Handle missing dependencies gracefully** — If scripts aren't installed, explain what's needed
