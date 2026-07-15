# /forge-start — Quick Start Onboarding

Interactive onboarding for new Forge users. Guides through initial setup and suggests first tasks based on project state.

## Input

$ARGUMENTS

Optional arguments:
- `--skip-check` — Skip health checks
- `--minimal` — Show only essential commands

## Step 1: Detect Project State

Check what exists:

```bash
# Check for company structure
ls -la .company/ 2>/dev/null

# Check for planning docs
ls -la .planning/ 2>/dev/null

# Check daemon status
cat .company/daemon.heartbeat 2>/dev/null | head -1
```

Determine state:
- **NEW**: No `.company/` directory
- **INITIALIZED**: `.company/` exists but no employees
- **READY**: Employees exist, daemon not running
- **RUNNING**: Daemon is active

## Step 2: Display Welcome

```
═══════════════════════════════════════════════════════════════════════════════
 FORGE — Structured Autonomy                                    [Quick Start]
═══════════════════════════════════════════════════════════════════════════════

 Welcome to Forge — AI-powered software development with safety controls.

 Project State: [NEW | INITIALIZED | READY | RUNNING]

═══════════════════════════════════════════════════════════════════════════════
```

## Step 3: State-Specific Guidance

### If NEW (no .company/):

```
 NEXT STEPS — Initialize Your Company

 You haven't set up a company yet. Run one of these:

 1. Quick Setup (recommended):
    /company-init

    Creates a basic company with default departments and employees.

 2. Bootstrap from Codebase:
    /company-bootstrap

    Analyzes your codebase and creates tailored departments/employees.

 3. Multi-Project Setup:
    /company-create

    Creates a company root that manages multiple projects.

═══════════════════════════════════════════════════════════════════════════════
 TIP: Most users start with /company-init for a quick setup.
═══════════════════════════════════════════════════════════════════════════════
```

### If INITIALIZED (company exists, no work):

```
 NEXT STEPS — Start Working

 Your company is set up! Here's how to start:

 1. Submit your first task:
    /company-request "Build a REST API for user auth"

 2. Or run the strategic planner:
    /strategy plan

 3. Check company health:
    /dashboard

 ──────────────────────────────────────────────────────────────────────────────
 QUICK REFERENCE

 | Command | Purpose |
 |---------|---------|
 | /company-request | Submit work |
 | /dashboard | Health overview |
 | /employee-status | See workforce |
 | /daemon start | Start autonomous execution |

═══════════════════════════════════════════════════════════════════════════════
```

### If READY (employees exist, daemon not running):

```
 NEXT STEPS — Activate the Daemon

 Your company is ready. Start autonomous execution:

 1. Start the daemon:
    /daemon start

 2. Or run a single cycle manually:
    /run-loop

 3. Check queue status:
    ./bin/forge-queue

 ──────────────────────────────────────────────────────────────────────────────
 PENDING WORK

 | Queue | Count |
 |-------|-------|
 | Pending | [N] tasks |
 | In Progress | [N] tasks |
 | Blocked | [N] tasks |

 The daemon will process these automatically once started.

═══════════════════════════════════════════════════════════════════════════════
```

### If RUNNING (daemon active):

```
 STATUS — Forge is Running

 Your daemon is active and processing work.

 ──────────────────────────────────────────────────────────────────────────────
 DAEMON STATUS

 | Metric | Value |
 |--------|-------|
 | Uptime | [X hours] |
 | Tasks Completed | [N] |
 | Current Task | [task-id or idle] |
 | Workers | [N] active |

 ──────────────────────────────────────────────────────────────────────────────
 QUICK ACTIONS

 | Command | Purpose |
 |---------|---------|
 | /company-request | Submit new work |
 | /dashboard | Full health view |
 | /daemon status | Detailed daemon info |
 | /respond | Handle escalations |

═══════════════════════════════════════════════════════════════════════════════
```

## Step 4: Show Essential Commands

Unless `--minimal` is specified, show categorized command reference:

```
 ──────────────────────────────────────────────────────────────────────────────
 COMMAND REFERENCE (Top 20)

 WORK MANAGEMENT
 | /company-request | Submit work to the company |
 | /pending | View items needing attention |
 | /respond | Handle escalations |
 | /proposals | Review employee proposals |

 VISIBILITY
 | /dashboard | Health snapshot |
 | /company-health | Deep health analysis |
 | /employee-status | Workforce overview |
 | /queue | View work queue |

 DAEMON CONTROL
 | /daemon start | Start background execution |
 | /daemon stop | Stop the daemon |
 | /daemon status | Check daemon health |
 | /run-loop | Manual execution cycle |

 PLANNING
 | /plan | Create implementation plan |
 | /strategy | Strategic planning |
 | /goals | View company goals |

 CODE WORKFLOW
 | /build | Execute plan with commits |
 | /review | Code review |
 | /commit | Create a commit |
 | /sync | Sync with remote |

 Full list: /help

═══════════════════════════════════════════════════════════════════════════════
```

## Step 5: Health Check (unless --skip-check)

Run a quick health check:

```bash
# Check for common issues
uv run .claude/hooks/company/health_check.py --quick 2>/dev/null || echo "Health check not available"
```

If issues found:

```
 ──────────────────────────────────────────────────────────────────────────────
 HEALTH WARNINGS

 [!] Test coverage below 50% — run /test-sprint
 [!] 3 tasks blocked — check /pending
 [!] Daemon heartbeat stale — check /daemon status

═══════════════════════════════════════════════════════════════════════════════
```

## Step 6: Suggest First Action

Based on state, suggest ONE clear next action:

```
 ──────────────────────────────────────────────────────────────────────────────
 SUGGESTED FIRST ACTION

 → [Action based on state]

═══════════════════════════════════════════════════════════════════════════════
```

| State | Suggestion |
|-------|------------|
| NEW | `Run /company-init to set up your company` |
| INITIALIZED | `Run /company-request "your first task"` |
| READY | `Run /daemon start to begin autonomous work` |
| RUNNING | `Run /dashboard to see current status` |

## Rules

- **Be concise** — New users are overwhelmed by 68 commands
- **Show state-appropriate guidance** — Don't suggest daemon commands if no company exists
- **One clear action** — Always end with a single suggested next step
- **Check health** — Warn about obvious issues unless skipped
- **Link to docs** — Reference `docs/` for deeper learning
