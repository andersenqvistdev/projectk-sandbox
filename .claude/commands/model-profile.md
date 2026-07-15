# /model-profile — Switch Model Profile

Quickly switch the active model profile used by the daemon and employee activation system.

## Input

$ARGUMENTS

Supported usage:
- `/model-profile` — Show current profile and available profiles
- `/model-profile <name>` — Switch to named profile (warns if worker count diverges)
- `/model-profile <name> --sync-workers` — Switch and sync `adaptiveScheduler.maxParallelWorkers`
- `/model-profile --list` — List all profiles with details

## Step 1: Load Config

Read `forge-config.json` from the project root.

Extract:
- `currentProfile` — value of `modelProfile` key
- `profiles` — all entries under `modelProfiles.profiles`

## Step 2: Parse Arguments

Parse `$ARGUMENTS`:
- If empty or `--list`: Show all profiles (go to Step 3)
- If a profile name (with optional `--sync-workers`): Switch to it (go to Step 4)

## Step 3: Display Profiles

Show the current active profile and all available profiles in a table:

```
═══════════════════════════════════════════════════════════════
 MODEL PROFILES                                    [forge-config]
═══════════════════════════════════════════════════════════════
 Active: <current-profile>
═══════════════════════════════════════════════════════════════

| Profile | Trivial | Standard | Complex | Epic | Executive | Subscription |
|---------|---------|----------|---------|------|-----------|--------------|
| > name  | model   | model    | model   | model| model     | plan         |

> = active profile

To switch: /model-profile <name>
═══════════════════════════════════════════════════════════════
```

For each model, use short names for readability:
- `claude-opus-4-8` → `Opus`
- `claude-sonnet-5` → `Sonnet`
- `claude-haiku-4-5-20251001` → `Haiku`

Exit after displaying.

## Step 4: Switch Profile

### 4.1: Validate Profile Exists

Check if the requested profile name exists in `profiles`.

**If not found:**
```
Profile "<name>" not found.

Available profiles: <comma-separated list>
```
Exit without changes.

### 4.2: Worker-Count Mismatch Check

Before writing anything, check whether the target profile has a `workers` field and whether it differs from `adaptiveScheduler.maxParallelWorkers` in `forge-config.json`.

**If mismatch detected AND `--sync-workers` was NOT given**, print a warning to stderr:
```
Warning: profile '<name>' specifies workers=<N> but adaptiveScheduler.maxParallelWorkers=<M>. Use --sync-workers to align them.
```
Continue with the switch regardless.

**If `--sync-workers` was given**, also update `adaptiveScheduler.maxParallelWorkers` to the profile's `workers` value. Do nothing if the profile has no `workers` field.

### 4.3: Update Config

Use the Edit tool to change the `modelProfile` value in `forge-config.json` from the current value to the requested profile name. If `--sync-workers` was given and the profile has a `workers` field, also update `adaptiveScheduler.maxParallelWorkers` in the same edit.

### 4.4: Confirm Switch

```
═══════════════════════════════════════════════════════════════
 MODEL PROFILE SWITCHED                              [success]
═══════════════════════════════════════════════════════════════
 Previous: <old-profile>
 Active:   <new-profile>
 Description: <profile description>
═══════════════════════════════════════════════════════════════

| Complexity | Model |
|-----------|-------|
| trivial   | <model> |
| standard  | <model> |
| complex   | <model> |
| epic      | <model> |
| executive | <model> |

Takes effect on next daemon task (no restart needed).
═══════════════════════════════════════════════════════════════
```

If `--sync-workers` was applied, include an extra line:
```
 adaptiveScheduler.maxParallelWorkers: <N> (synced)
```

## Rules

- **No restart needed.** `employee_activator.py` reads forge-config.json per task.
- **Validate before switching.** Never write an invalid profile name.
- **Warn on worker mismatch by default.** If the profile has a `workers` field and it differs from `adaptiveScheduler.maxParallelWorkers`, always warn — even without `--sync-workers`.
- **`--sync-workers` is the fix.** It updates `adaptiveScheduler.maxParallelWorkers` to match the profile's `workers` field. No-op for profiles without a `workers` field.
- **Show before/after.** Always confirm what changed.
- **Use short model names.** Opus/Sonnet/Haiku for readability.
- **Preserve file formatting.** Only change the `modelProfile` value (and `maxParallelWorkers` if `--sync-workers`), nothing else.
- **CLI alternative.** `bin/forge-model-profile` exposes the same logic as a shell command backed by `.claude/hooks/company/model_profile_switcher.py`.
