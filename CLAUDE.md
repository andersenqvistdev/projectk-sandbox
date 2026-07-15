# Forge — Structured Autonomy

> AI-powered software development with deterministic safety controls.

**Philosophy**: Structured autonomy — fast AND safe. Not full automation, not manual-only.

## Quick Reference

| Need | Command |
|------|---------|
| Check status | `/dashboard` |
| Submit work | `/company-request "description"` |
| View queue | `./bin/forge-queue` |
| Start daemon | `/daemon start` |
| See employees | `/employee-status` |
| Review scout proposals | `gh pr list --search "head:scout/"` + `.planning/ROADMAP-SCOUT.md` |
| Create PR | Branch + `gh pr create` |

## Trust Tiers

| Tier | Examples | Permission |
|------|----------|------------|
| **Free** | Read, Glob, Grep, git status | Auto |
| **Guarded** | Write, Edit, git commit | Auto + hooks |
| **Gated** | git push, deploy | Human confirm |
| **Forbidden** | rm -rf, sudo, push --force main | Blocked |

## Git Workflow

**Always branch + PR** — `git_guardian.py` blocks direct push to main.

```bash
git checkout -b feat/my-feature
# ... make changes ...
git add <files> && git commit -m "feat: description"
git push -u origin feat/my-feature
gh pr create
```

After PR merge: `/sync` to update local main.

## Active Hooks

| Hook | Purpose |
|------|---------|
| `block_dangerous.py` | Blocks rm -rf, sudo, chmod 777 |
| `secrets_scanner.py` | Blocks API keys in code |
| `git_guardian.py` | Blocks push to main, secrets in commits |
| `lint_on_edit.py` | Auto-lint, protects org.json |
| `context_monitor.py` | Warns at low context |

Full list: `.claude/settings.json`

## Maintenance Mode

**CRITICAL**: When modifying core daemon files, enable maintenance mode first:

```bash
# Enable (blocks daemon start)
echo '{"enabled": true, "reason": "Fixing X", "protected_files": ["forge_daemon.py"]}' > .company/maintenance_mode.json

# Disable (allows daemon to run)
rm .company/maintenance_mode.json
```

**Protected files** (never edit while daemon is running):
- `forge_daemon.py` — Main daemon loop
- `employee_activator.py` — Task execution
- `failure_recovery.py` — Error handling
- `operation_loop.py` — Work queue processing

**Rule**: If editing these files, stop daemon first. Don't let the engine fix the engine.

## Daemon Operation

The daemon runs autonomously, processing tasks from the work queue.

**States**: CLOSED (normal) → OPEN (tripped, cooling down) → HALF_OPEN (testing)

**Key files**:
- Queue: `.company/state/work_queue.json`
- Config: `forge-config.json`
- Logs: `.company/logs/`

**Monitor**: `./bin/forge-queue watch`

## Autonomy Features (WS-105/106)

**Target**: 85% autonomous operation.

| Feature | What It Does |
|---------|--------------|
| **Failure Recovery** | Auto-retry failed tasks with intelligent strategies |
| **Smart Escalation** | Route to capable employees before humans |
| **Pattern Learning** | Extract patterns from successes, inject into prompts |
| **Auto-Merge** | Confidence-based PR auto-merge (score ≥ 80) |
| **Gap Analysis** | Daily goal assessment, auto-generate fix tasks |

See `docs/autonomy-features.md` for details.

## Commands

### Core Workflow
| Command | Purpose |
|---------|---------|
| `/plan` | Create implementation plan |
| `/build` | Execute plan with atomic commits |
| `/review` | Code review |
| `/verify` | Check work completeness |
| `/pre-merge` | Validate CI locally before push |

### Company Management
| Command | Purpose |
|---------|---------|
| `/dashboard` | Health snapshot |
| `/company-request` | Submit work |
| `/company-health` | Deep insights |
| `/employee-status` | Workforce view |
| `/respond` | Handle escalations |

### Daemon Control
| Command | Purpose |
|---------|---------|
| `/daemon start` | Start background processing |
| `/daemon stop` | Stop daemon |
| `/daemon status` | Check daemon health |
| `/run-loop` | Manual execution cycle |

Full command list: `/help`

## Configuration

All config in `forge-config.json`:
- `modelProfile`: Model selection (max-200, max-100, pro, balanced-5, scale-10)
- `daemon.*`: Polling intervals, timeouts
- `autonomy.*`: Auto-merge, auto-rollback, CI healing
- `adaptiveScheduler.*`: Worker scaling (must match profile's `workers` field)

**Worker count**: `adaptiveScheduler.maxParallelWorkers` must match profile. For balanced-5: set to 5.

## Code Standards

- Run linter before commit
- Tests required for new code
- No secrets in code (hooks enforce)
- One task = one atomic commit
- Branch + PR for all changes

### Test isolation for path-defaulting configs

Some config dataclasses (e.g. `WatchdogConfig`) have path fields. **Never give such fields a cwd-relative default** like `Path(".company/foo.json")` — tests then have to override every path individually, and missing one lets real state from the working tree's `.company/` leak into the fixture (CI passes on clean checkout, fails locally). Use a `base_dir: Path` field that anchors all paths, so test isolation is a single `base_dir=tmp_path` override.

If you're writing tests for a config that already has this antipattern (e.g. `DaemonConfig` in the protected `forge_daemon.py`), override **every** path field to a fixture path. Don't rely on the defaults.

## Key Directories

| Path | Purpose |
|------|---------|
| `.company/` | Org data, queue, agents |
| `.claude/hooks/` | Safety hooks |
| `.claude/agents/` | Agent definitions |
| `.planning/` | Plans, roadmaps |
| `forge-config.json` | All configuration |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Push blocked | Create branch, use PR |
| Daemon not starting | Check `/daemon status` |
| Task stuck | `./bin/forge-queue heal` |
| Import error | Check `sys.path` in script |
| Empty Claude output | Check CLAUDE.md size (<20KB) |
| False lint failures / ruff errors in unrelated files | Stale worktrees in `/tmp/forge-worktrees/` — restart daemon (startup cleanup removes all) or run `git worktree prune` manually |
| Closed-unmerged PRs still counting as shipped | Daemon auto-reconciles every 6h; force now: `autonomy_metrics.py reconcile` (`--dry-run` to preview) |
| Scout task never schedules | Check `.company/state.nosync/task_admission_rejections.jsonl` — backtick-quoted not-yet-existing identifiers trip the admission gate's target check |

## Documentation

Detailed docs in `docs/`:
- `docs/autonomy-features.md` — WS-105/106 autonomy (failure recovery, smart escalation) + scout intake loop
- `docs/daemon-operations.md` — Daemon architecture, configuration, troubleshooting; § External Intake Files (opportunity scout)
- `docs/commands-reference.md` — All 82 commands reference
- `docs/parallel-execution.md` — Worktree isolation
- `SECURITY.md` — Security philosophy

---

*This file is auto-optimized. Full reference: `docs/CLAUDE-full.md`*
