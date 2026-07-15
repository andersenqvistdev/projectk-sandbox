# /autonomy — Toggle Auto-Merge to Main

View and control whether the Forge daemon can autonomously merge PRs to the main branch.

`allowMergeToMain` is the highest autonomy lever in Forge. When enabled, the daemon operates
end-to-end: it creates a branch, writes code, opens a PR, runs CI, and merges — without any
human touch. Turning it off reverts to semi-auto: daemon still does all the work, but the
merge to main requires a human decision.

> **INTERACTIVE USE ONLY.** This command must never run inside a daemon worker. If the
> environment variable `FORGE_WORKER_ID` is set, refuse immediately and print:
> `ERROR: /autonomy cannot be run inside a daemon worker. Run this in your interactive session.`
>
> **humanProtected note:** `forge-config.json` cannot be edited by the daemon.
> This command only works in an interactive Claude Code session (you running it).

## Input

$ARGUMENTS

Supported usage:
- `/autonomy` — Show current autoMerge state with plain-language explanation
- `/autonomy on` — Enable `allowMergeToMain` (full autonomous operation)
- `/autonomy off` — Disable `allowMergeToMain` (semi-auto: daemon creates PRs, humans merge)

## Step 1: Load Config

Extract the two relevant fields from **the root `forge-config.json`** (not `.claude/forge-config.json`,
which is a legacy install-time template the daemon ignores):

```bash
jq '.autonomy.autoMerge | {enabled, allowMergeToMain}' forge-config.json
```

Store the values:
- `autoMerge_enabled` — value of `autonomy.autoMerge.enabled` (boolean)
- `allow_merge` — value of `autonomy.autoMerge.allowMergeToMain` (boolean)

If `forge-config.json` is missing, show:

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                              [ERROR]
═══════════════════════════════════════════════════════════════

forge-config.json not found in the project root.

To initialize: /forge-start
═══════════════════════════════════════════════════════════════
```

Exit without changes.

## Step 2: Parse Arguments

Parse `$ARGUMENTS`:
- If empty or missing → display current state (Step 3)
- If `on` → enable allowMergeToMain (Step 4)
- If `off` → disable allowMergeToMain (Step 5)
- Otherwise → show usage error:

```
Unknown argument: "<arg>"

Usage:
  /autonomy       — Show current state
  /autonomy on    — Enable auto-merge to main
  /autonomy off   — Disable auto-merge to main
```

## Step 3: Display Current State

Determine the current autonomy level from the two flags:

| autoMerge.enabled | allowMergeToMain | Level | Label |
|-------------------|-----------------|-------|-------|
| false | (any) | 0 | Manual |
| true | false | 1 | Semi-auto |
| true | true | 2 | Full auto |

Display:

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                        [forge-config]
═══════════════════════════════════════════════════════════════
 autoMerge.enabled:      <true/false>
 allowMergeToMain:       <true/false>
═══════════════════════════════════════════════════════════════

 Level <N> — <Label>
 ─────────────────────────────────────────────────────────────
 <Plain-language explanation — see below>
═══════════════════════════════════════════════════════════════

To change:
  /autonomy on   — enable allowMergeToMain  (Level 1 → 2)
  /autonomy off  — disable allowMergeToMain (Level 2 → 1)

Note: autoMerge.enabled is not managed by this command.
      Edit forge-config.json directly to toggle it.
═══════════════════════════════════════════════════════════════
```

### Plain-language explanations

**Level 0 — Manual** (`autoMerge.enabled = false`):
```
Auto-merge is OFF. The daemon creates branches and writes code,
but every PR waits for a human to review and merge it.
The /autonomy toggle has no effect while autoMerge.enabled is false.
To enter semi-auto or full-auto mode, first set autoMerge.enabled
to true in forge-config.json, then re-run /autonomy.
```

**Level 1 — Semi-auto** (`autoMerge.enabled = true`, `allowMergeToMain = false`):
```
Auto-merge is ON, but merging to main requires human approval.
The daemon creates branches, writes code, opens PRs, and runs CI.
You review each PR and decide whether to merge.
This is the recommended starting point when climbing the autonomy ladder.
```

**Level 2 — Full auto** (`autoMerge.enabled = true`, `allowMergeToMain = true`):
```
Full autonomous operation. The daemon creates branches, writes code,
opens PRs, runs CI, and merges to main — end to end, no human touch.
High-risk tasks and protected paths (forge-config.json, CLAUDE.md,
.claude/settings.json) are still blocked from auto-merge.
Re-canary after every config change to verify behavior.
```

## Step 4: Enable allowMergeToMain (`/autonomy on`)

### 4.0 CI Liveness Gate

Before enabling `allowMergeToMain`, verify that CI is active and branch protection
requires it. Enabling full autonomy on a repo with no CI gate lets the daemon merge
PRs that break the codebase — this is the D5 failure mode documented in Project D.

Run:

```bash
bin/forge-protect-main --ensure-ci
```

This command (in order):
1. Looks for workflow files under `.github/workflows/`. If none exist, writes a
   minimal `ci.yml` with `workflow_dispatch:` support and exits with instructions
   to commit and push it before re-running.
2. Checks GitHub for a recent successful Actions run. If none exists, triggers a
   `workflow_dispatch` event and waits up to 10 minutes for it to complete.
   This handles the new-repo no-runs quirk where a ci.yml exists but has never run.
3. Applies branch protection to the main branch with the CI checks required.

**If the command exits 0** — protection is now in place. Proceed to step 4.1.

**If the command exits 1 with "[ACTION REQUIRED]"** — a minimal `ci.yml` was
written but needs to be committed and pushed before CI can run. Show:

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                            [BLOCKED]
═══════════════════════════════════════════════════════════════

CI not yet live on this repository. A minimal workflow was written to:
  .github/workflows/ci.yml

Before enabling full autonomy:
  1. git add .github/workflows/ci.yml
  2. git commit -m "ci: add minimal GitHub Actions workflow"
  3. git push
  4. Re-run: bin/forge-protect-main --ensure-ci
  5. Then re-run: /autonomy on

Reason: allowMergeToMain must not be enabled on a repo with no CI gate.
Without CI, the daemon can merge PRs that fail tests or break the codebase.
═══════════════════════════════════════════════════════════════
```

Exit without changes.

**If the command exits 1 with "[FAIL]"** — CI liveness check failed for another
reason (CI run failed, trigger failed, gh auth issue). Show:

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                            [BLOCKED]
═══════════════════════════════════════════════════════════════

CI liveness gate failed. Run for details:
  bin/forge-protect-main --ensure-ci

Then re-run: /autonomy on
═══════════════════════════════════════════════════════════════
```

Exit without changes.

### 4.1 Check preconditions

If `autoMerge_enabled` is false:
```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                            [BLOCKED]
═══════════════════════════════════════════════════════════════

autoMerge.enabled is currently false.
allowMergeToMain has no effect while auto-merge is disabled.

To enable auto-merge first, edit forge-config.json:
  autonomy.autoMerge.enabled → true

Then run /autonomy on again.
═══════════════════════════════════════════════════════════════
```
Exit without changes.

If `allow_merge` is already true:
```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                        [NO CHANGE]
═══════════════════════════════════════════════════════════════

allowMergeToMain is already true (Level 2 — Full auto).

No change made.
═══════════════════════════════════════════════════════════════
```
Exit without changes.

Check for PRs already labeled `auto-merge-ready` that could merge the moment the flag flips:

```bash
gh pr list --label auto-merge-ready --state open --json number,title,headRefName 2>/dev/null
```

If any are found, include this in the warning panel:

```
 ⚠  Open PRs with auto-merge-ready label: <N>
    These will merge to main as soon as you enable this setting.
    Review them first: gh pr list --label auto-merge-ready
```

### 4.2 Show warning

Display before making any edit:

```
═══════════════════════════════════════════════════════════════
 AUTONOMY — ENABLING FULL AUTO-MERGE                [WARNING]
═══════════════════════════════════════════════════════════════

 Change:  allowMergeToMain  false → true
 File:    forge-config.json  (humanProtected — you own this)

 What this means:
   The daemon will merge PRs to main without human review.
   CI must pass and blockedPaths are still protected, but
   otherwise the daemon operates end-to-end autonomously.

 Before enabling, confirm:
   □ CI is green on main right now
   □ blockedPaths in autoMerge covers all sensitive files
   □ You plan to run a canary immediately after (see below)

 This change takes effect on the daemon's next task cycle.
 No daemon restart is needed.
═══════════════════════════════════════════════════════════════
```

### 4.3 Apply the edit

Use the Edit tool to change `"allowMergeToMain": false,` to `"allowMergeToMain": true,`
in `forge-config.json`. Change ONLY this field — do not touch any other value.

### 4.4 Confirm and show canary checklist

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                           [ENABLED]
═══════════════════════════════════════════════════════════════

 allowMergeToMain:  false → true   ✓
 Level:             Semi-auto (1) → Full auto (2)

═══════════════════════════════════════════════════════════════
 RE-CANARY CHECKLIST                              [required]
═══════════════════════════════════════════════════════════════

Run through this checklist to verify the new setting is working:

  0. Verify the edit landed in the root file (not the legacy copy):
       jq '.autonomy.autoMerge.allowMergeToMain' forge-config.json
       # expected: true

  1. Stop the daemon:
       kill -9 $(cat .company/daemon.pid)

  2. Submit a safe canary task:
       /company-request "canary: add a one-line comment to README — verify auto-merge"

  3. Restart the daemon:
       /daemon start
     Note: No restart is required for the config value itself — the daemon re-reads
     forge-config.json fresh on each auto-merge decision. Restart ensures a clean
     circuit-breaker state for the canary.

  4. Watch the queue:
       ./bin/forge-queue watch

  5. Verify end-to-end behavior:
       □ A branch was created (daemon/wt-*)
       □ A PR was opened
       □ CI passed (or was bypassed for trivial tasks)
       □ PR was merged to main automatically
       □ Branch was deleted after merge

  6. Confirm circuit breaker stayed CLOSED:
       /daemon status

  7. If anything went wrong:
       □ Stop daemon: kill -9 $(cat .company/daemon.pid)
       □ Run /autonomy off to revert
       □ Investigate before re-enabling

═══════════════════════════════════════════════════════════════
```

## Step 5: Disable allowMergeToMain (`/autonomy off`)

### 5.1 Check current state

If `allow_merge` is already false:
```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                        [NO CHANGE]
═══════════════════════════════════════════════════════════════

allowMergeToMain is already false (Level 1 — Semi-auto).

No change made.
═══════════════════════════════════════════════════════════════
```
Exit without changes.

### 5.2 Show what will change

```
═══════════════════════════════════════════════════════════════
 AUTONOMY — DISABLING AUTO-MERGE TO MAIN          [CONFIRM]
═══════════════════════════════════════════════════════════════

 Change:  allowMergeToMain  true → false
 File:    forge-config.json  (humanProtected — you own this)

 What this means:
   The daemon will continue creating branches and opening PRs,
   but all merges to main will require a human decision.
   In-flight PRs already open will not be auto-merged.
═══════════════════════════════════════════════════════════════
```

### 5.3 Apply the edit

Use the Edit tool to change `"allowMergeToMain": true,` to `"allowMergeToMain": false,`
in `forge-config.json`. Change ONLY this field — do not touch any other value.

### 5.4 Confirm and show canary checklist

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                          [DISABLED]
═══════════════════════════════════════════════════════════════

 allowMergeToMain:  true → false   ✓
 Level:             Full auto (2) → Semi-auto (1)

═══════════════════════════════════════════════════════════════
 RE-CANARY CHECKLIST                              [required]
═══════════════════════════════════════════════════════════════

Run through this checklist to verify the new setting is working:

  0. Verify the edit landed in the root file:
       jq '.autonomy.autoMerge.allowMergeToMain' forge-config.json
       # expected: false

  0b. Check for PRs already labeled auto-merge-ready — the Forge daemon's gate will
      now block them, but any `gh pr merge --auto` armed at the GitHub layer will still
      execute regardless of this config flag. Clear the label or close the auto-merge
      if you want to prevent those PRs from landing:
       gh pr list --label auto-merge-ready --state open

  1. Stop the daemon:
       kill -9 $(cat .company/daemon.pid)

  2. Submit a safe canary task:
       /company-request "canary: add a one-line comment to README — verify PR stays open"

  3. Restart the daemon:
       /daemon start
     Note: No restart is required for the config value itself — the daemon re-reads
     forge-config.json fresh on each auto-merge decision.

  4. Watch the queue:
       ./bin/forge-queue watch

  5. Verify semi-auto behavior:
       □ A branch was created (daemon/wt-*)
       □ A PR was opened
       □ PR remained OPEN — NOT merged automatically
       □ Circuit breaker stayed CLOSED (/daemon status)

  6. If the PR was auto-merged anyway:
       □ Check forge-config.json was saved correctly (step 0)
       □ Check for armed auto-merge on GitHub (step 0b)
       □ Check for other auto-merge levers (ci.yml `gh pr merge --auto`, branch rules)

═══════════════════════════════════════════════════════════════
```

## Rules

- **Refuse if FORGE_WORKER_ID is set.** This command is interactive-only. A daemon worker running `/autonomy` is a misconfiguration or injection — refuse and print the error message from the header.
- **Run the CI liveness gate before every `/autonomy on`.** Step 4.0 (`forge-protect-main --ensure-ci`) is mandatory, not optional. If it exits non-zero, do not flip `allowMergeToMain`. A repo without a passing CI check in its branch protection is an unsafe target for full autonomy (Project D finding D5).
- **Always target root `forge-config.json`.** The legacy `.claude/forge-config.json` is an install-time template; editing it is a silent no-op. Use `forge-config.json` at the project root.
- **Edit only `allowMergeToMain`.** Never change `autoMerge.enabled` or any other field. Use the exact string `"allowMergeToMain": false,` / `"allowMergeToMain": true,` as the match target.
- **Extract fields via jq, not raw file display.** Never paste or display raw JSON from forge-config.json. Use jq to pull only the two needed fields.
- **Show warning before editing.** The user must see the impact before the change is applied.
- **Show canary checklist after every change.** Autonomy changes require verification.
- **Detect no-op early.** If the value already matches the requested state, exit without editing.
- **Respect humanProtected.** This command runs in interactive session — that's exactly when editing forge-config.json is safe and intentional. The daemon cannot and must not run this command.
- **Display current state first on `on`/`off` if precondition fails.** E.g., if autoMerge.enabled is false, show the full state panel (Step 3) and explain why the toggle is blocked.
