# /license — License Management

Manage Forge Framework licenses: view current status, issue new licenses, generate trials, and manage keypairs.

## Input
$ARGUMENTS

## Command Syntax

```
/license                    # Show current license status
/license status             # Same as above
/license issue              # Issue a new license (interactive)
/license trial              # Generate a 14-day trial license
/license generate-keypair   # Generate a dev Ed25519 keypair
/license tiers              # Show available tiers and pricing
/license validate           # Deep-validate current license file
```

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the action:

| Pattern | Action | Go to |
|---------|--------|-------|
| (empty) | STATUS | Step 1 |
| `status` | STATUS | Step 1 |
| `issue` | ISSUE | Step 2 |
| `trial` | TRIAL | Step 3 |
| `generate-keypair` | KEYPAIR | Step 4 |
| `tiers` | TIERS | Step 5 |
| `validate` | VALIDATE | Step 6 |

## Step 1: Show License Status

Check for an active license and display its status.

### 1.1: Detect License File

Search for license files in priority order:

1. `$FORGE_LICENSE_FILE` env var (path to file)
2. `$FORGE_LICENSE_JSON` env var (inline JSON)
3. `~/.forge/license.json` (user home)
4. `./.forge/license.json` (project root)

Also check for legacy key format:
5. `$FORGE_LICENSE_KEY` env var
6. `~/.forge/license.key`

### 1.2: Run Validation

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && uv run .claude/hooks/forge_license.py
```

This runs the license gate hook which validates and reports the current license status.

### 1.3: Parse License File (if exists)

If a `forge-license.json` file was found, read and parse it to extract:
- `org` — Organization name
- `org_id` — Organization ID
- `tier` — License tier
- `valid_until` — Expiry date
- `issued_at` — When issued
- `features` — Enabled features
- `signature` — Whether signature is present

### 1.4: Display Status

**If license found:**

```
===============================================================
  FORGE LICENSE STATUS
===============================================================

  Organization:  [org] ([org_id])
  Tier:          [tier]
  Status:        [ACTIVE / EXPIRED / INVALID]
  Valid Until:   [valid_until] ([days remaining] days remaining)
  Issued:        [issued_at]
  Format:        Ed25519 JSON (schema v1)

  ENABLED FEATURES ([count])
  ---------------------------------------------------------------
  [checkmark] audit-export          Compliance audit export
  [checkmark] sbom                  SBOM generation (CycloneDX/SPDX)
  [checkmark] extended-secret-scanning  14+ detection patterns
  [checkmark] soc2-mapping          SOC 2 Type 1 mapping
  [checkmark] compliance-pack       Full Compliance Pack
  [x] priority-support              Requires teams-business
  [x] multi-project                 Requires teams-business
  [x] custom-agents                 Requires teams-business
  [x] self-hosted                   Requires enterprise
  [x] dedicated-support             Requires enterprise
  [x] sso                           Requires enterprise
  [x] advanced-rate-limiting        Requires enterprise

  LICENSE FILE
  ---------------------------------------------------------------
  Location: [path where license was found]
  Signature: [VALID / INVALID / MISSING]

===============================================================

  Upgrade: https://forgelabs.dev/pricing
  Issue:   /license issue
  Trial:   /license trial

===============================================================
```

**If no license found:**

```
===============================================================
  FORGE LICENSE STATUS
===============================================================

  Tier:     core (Free / Open Source)
  Status:   NO LICENSE FILE

  You are running Forge Core — the free open-source tier.
  All core features are available without a license.

  CORE FEATURES (always available)
  ---------------------------------------------------------------
  [checkmark] Hook system           Deterministic security hooks
  [checkmark] GSD workflow           Plan/Build/Review pipeline
  [checkmark] Agent system           Agent creation and management
  [checkmark] Basic secret scanning  8 detection patterns
  [checkmark] Atomic commits         One task = one commit
  [checkmark] Circuit breaker        Autonomous loop protection

  PREMIUM FEATURES (license required)
  ---------------------------------------------------------------
  [lock] audit-export             /license trial to try
  [lock] sbom                     /license trial to try
  [lock] extended-secret-scanning /license trial to try
  [lock] soc2-mapping             teams-pro and above
  [lock] compliance-pack          teams-pro and above
  [lock] priority-support         teams-business and above
  [lock] multi-project            teams-business and above
  [lock] custom-agents            teams-business and above
  [lock] self-hosted              enterprise only
  [lock] dedicated-support        enterprise only
  [lock] sso                      enterprise only
  [lock] advanced-rate-limiting   enterprise only

===============================================================

  Get started:
    /license trial              14-day free trial (all features)
    /license tiers              View pricing
    https://forgelabs.dev       Learn more

===============================================================
```

## Step 2: Issue a License

Interactive license issuance for internal use (requires private key).

### 2.1: Check Private Key Availability

Check if `FORGE_LICENSE_PRIVATE_KEY` or `FORGE_LICENSE_PRIVATE_KEY_PATH` is set.

**If not set:**
```
===============================================================
  LICENSE ISSUANCE
===============================================================

  Private key not found.

  To issue licenses, you need the Ed25519 signing private key:

    export FORGE_LICENSE_PRIVATE_KEY=<base64-encoded-pem>
    # or
    export FORGE_LICENSE_PRIVATE_KEY_PATH=/path/to/private.pem

  For development/testing:
    /license generate-keypair

  For production:
    Contact your security administrator for the signing key.

===============================================================
```

### 2.2: Gather Parameters

Use AskUserQuestion to collect:
- Organization name
- Organization ID
- Tier (core, teams-starter, teams-pro, teams-business, enterprise)
- Valid until date (YYYY-MM-DD or "perpetual")
- Custom features (optional, defaults to tier features)
- Output path (default: forge-license.json)

### 2.3: Execute Issuance

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && \
uv run .claude/hooks/company/forge_issue_license.py \
  --org "[org]" \
  --org-id "[org_id]" \
  --tier [tier] \
  --valid-until [date] \
  --out [output_path]
```

### 2.4: Display Result

```
===============================================================
  LICENSE ISSUED
===============================================================

  Organization:  [org] ([org_id])
  Tier:          [tier]
  Valid Until:   [valid_until]
  Features:      [feature_count] enabled
  Output:        [output_path]

  To activate:
    cp [output_path] ~/.forge/license.json

  To verify:
    /license validate

===============================================================
```

## Step 3: Generate Trial License

Quick trial license generation (14-day, teams-pro tier).

### 3.1: Check Private Key

Same as Step 2.1 — private key required.

### 3.2: Execute Trial Issuance

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && \
uv run .claude/hooks/company/forge_issue_license.py \
  --trial \
  --out forge-license-trial.json
```

### 3.3: Display Trial Result

```
===============================================================
  TRIAL LICENSE GENERATED
===============================================================

  Tier:          teams-pro (full access)
  Valid For:     14 days
  Expires:       [date]
  Features:      5 premium features enabled
  Output:        forge-license-trial.json

  ACTIVATION
  ---------------------------------------------------------------
  Option 1 — Copy to home directory:
    cp forge-license-trial.json ~/.forge/license.json

  Option 2 — Set environment variable:
    export FORGE_LICENSE_FILE=forge-license-trial.json

  Option 3 — Project-local:
    mkdir -p .forge && cp forge-license-trial.json .forge/license.json

  After activation, run:
    /license status

  WHAT YOU GET
  ---------------------------------------------------------------
  [checkmark] audit-export          Export compliance audit trails
  [checkmark] sbom                  Generate SBOMs (CycloneDX/SPDX)
  [checkmark] extended-secret-scanning  14+ secret patterns
  [checkmark] soc2-mapping          SOC 2 Type 1 control mapping
  [checkmark] compliance-pack       Full Compliance Pack bundle

  Upgrade to keep access: https://forgelabs.dev/pricing

===============================================================
```

## Step 4: Generate Development Keypair

Generate a new Ed25519 keypair for development/testing.

### 4.1: Execute Keypair Generation

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && \
uv run .claude/hooks/company/forge_issue_license.py --generate-dev-keypair
```

### 4.2: Display with Security Warning

```
===============================================================
  DEVELOPMENT KEYPAIR GENERATED
===============================================================

  [output from generate-dev-keypair]

  WARNING: Development keys only. For production:
  - Generate on air-gapped machine or HSM
  - Store private key in secrets manager
  - Never commit private key to git
  - See: .company/business/license-spec.md §6

===============================================================
```

## Step 5: Show Tiers and Pricing

Display the tier comparison table.

```
===============================================================
  FORGE FRAMEWORK — PRICING TIERS
===============================================================

  TIER COMPARISON

  | Tier | Price/mo | Devs | Key Features |
  |------|----------|------|--------------|
  | Core | Free | Unlimited | Hooks, GSD workflow, agents |
  | Teams Starter | $99 | 5-10 | + Audit export, SBOM, scanning |
  | Teams Pro | $249 | 11-25 | + SOC 2, Compliance Pack |
  | Teams Business | $499 | 26-50 | + Priority support, multi-project |
  | Enterprise | Custom | Unlimited | + Self-hosted, SSO, SLA |

  FEATURE MATRIX

  | Feature | Core | Starter | Pro | Business | Enterprise |
  |---------|------|---------|-----|----------|------------|
  | Hook system | Y | Y | Y | Y | Y |
  | GSD workflow | Y | Y | Y | Y | Y |
  | Agent system | Y | Y | Y | Y | Y |
  | Basic scanning (8) | Y | Y | Y | Y | Y |
  | Audit export | - | Y | Y | Y | Y |
  | SBOM generation | - | Y | Y | Y | Y |
  | Extended scanning (14+) | - | Y | Y | Y | Y |
  | SOC 2 mapping | - | - | Y | Y | Y |
  | Compliance Pack | - | - | Y | Y | Y |
  | Priority support | - | - | - | Y | Y |
  | Multi-project | - | - | - | Y | Y |
  | Custom agents | - | - | - | Y | Y |
  | Self-hosted | - | - | - | - | Y |
  | SSO (SAML/OIDC) | - | - | - | - | Y |
  | Dedicated support | - | - | - | - | Y |

  Try free for 14 days: /license trial
  Purchase: https://forgelabs.dev/pricing
  Questions: sales@forgelabs.dev

===============================================================
```

## Step 6: Deep Validate License

Run comprehensive validation on the current license file.

### 6.1: Find License File

Same search as Step 1.1.

### 6.2: Run Full Validation

If license file found, validate:
1. Schema version (must be "1")
2. All required fields present
3. Date format (valid_until is YYYY-MM-DD or "perpetual")
4. Expiry check (not expired)
5. Features are valid known flags
6. Ed25519 signature verification
7. Tier is recognized

### 6.3: Display Validation Report

```
===============================================================
  FORGE LICENSE VALIDATION
===============================================================

  File: [path]

  CHECKS
  ---------------------------------------------------------------
  [checkmark] Schema version     v1 (supported)
  [checkmark] Required fields    All 7 fields present
  [checkmark] Date format        valid_until is valid ISO date
  [checkmark] Expiry             Valid for [N] more days
  [checkmark] Features           [N] valid flags, 0 unknown
  [checkmark] Signature          Ed25519 signature VALID
  [checkmark] Tier               [tier] (recognized)

  RESULT: LICENSE VALID

===============================================================
```

Or with errors:

```
===============================================================
  FORGE LICENSE VALIDATION
===============================================================

  File: [path]

  CHECKS
  ---------------------------------------------------------------
  [checkmark] Schema version     v1 (supported)
  [checkmark] Required fields    All 7 fields present
  [x] Expiry                     EXPIRED on [date] ([N] days ago)
  [checkmark] Features           [N] valid flags
  [x] Signature                  INVALID (tampered or wrong key)

  RESULT: LICENSE INVALID

  Actions:
  - Request a new license from your administrator
  - Generate a trial: /license trial
  - Contact: sales@forgelabs.dev

===============================================================
```

## Rules

1. **Never display private keys.** Only reference their environment variables.
2. **Never commit license files to git.** Warn if attempting to add forge-license.json to staging.
3. **Signature verification requires cryptography package.** Handle ImportError gracefully.
4. **Legacy format backward compat.** Support both forge-license.json and FORGE-TIER-EXPIRY-HMAC formats.
5. **Offline-only validation.** Never make network calls during validation.
6. **Show upgrade paths.** Always suggest the next tier when showing locked features.
7. **Trial is non-destructive.** Trial generation creates a new file, never overwrites existing licenses.
