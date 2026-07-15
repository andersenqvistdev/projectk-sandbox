# /compliance-pack — Compliance Pack Status

Show the current Forge Compliance Pack license status, which features are active, and optionally verify that all hook infrastructure is in place.

## Input
$ARGUMENTS

Supported arguments:
- `--verify` — Check that all hook files and command files are present on disk
- `--json` — Output machine-readable JSON instead of formatted report

## Step 1: Run Status Check

Execute the compliance pack status utility:

```bash
uv run .claude/hooks/compliance_pack_status.py $ARGUMENTS
```

The script outputs:
- **License status** — valid/unlicensed, tier name, org, expiry date
- **Feature matrix** — which Compliance Pack features are active or locked
- **Verification** (with `--verify`) — hook and command file presence
- **Upgrade CTA** — trial key and pricing link if unlicensed

## Step 2: Interpret and Present Results

### Licensed (exit code 0)

Display the script output as-is. Summarize the active tier and available features.

### Unlicensed or expired (exit code 1)

Display the script output and highlight the upgrade path:

```
The Compliance Pack requires a Teams Starter license or higher.

Start a free 14-day trial (no credit card):
  export FORGE_LICENSE_KEY='FORGE-trial-<today>-trial'

Or purchase a license at https://forgelabs.dev/pricing
```

### Verification failure (exit code 2)

Display the script output and flag the missing files. The infrastructure files
listed as missing must exist for the feature to function, even with a valid license.

## What the Compliance Pack Includes

| Feature | Tier Required | Command |
|---------|---------------|---------|
| Extended Secret Scanning (23 patterns) | Teams Starter | automatic (PreToolUse hook) |
| Audit Export | Teams Starter | `/audit-export` |
| SBOM Generation | Teams Starter | `/sbom` |
| SOC 2 Control Mapping | Teams Pro | `.planning/G9-SOC2-MAPPING.md` |
| Compliance Pack Bundle | Teams Pro | — |

## License Formats

**New format (Ed25519 signed JSON):**
Place at `~/.forge/license.json` or set `$FORGE_LICENSE_FILE` / `$FORGE_LICENSE_JSON`.

**Legacy HMAC key:**
```bash
export FORGE_LICENSE_KEY='FORGE-<tier>-<expiry>-<hmac>'
```

**14-day trial:**
```bash
export FORGE_LICENSE_KEY='FORGE-trial-<YYYY-MM-DD>-trial'
```

## Examples

```bash
# Check license and feature status
/compliance-pack

# Verify all hook files and infrastructure are in place
/compliance-pack --verify

# Machine-readable output (for scripting or CI)
/compliance-pack --json
```

## Rules

- Always display the full feature matrix so users know what is and is not active
- If the license is expired or missing, always include the trial key CTA
- With `--verify`, highlight any missing files — a valid license alone is not sufficient if the implementation files are absent
- With `--json`, pass the flag through to the script and print raw JSON output without additional decoration
