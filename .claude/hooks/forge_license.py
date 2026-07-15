# /// script
# requires-python = ">=3.10"
# ///
"""
Forge License Gate — Premium Feature Enforcement (WS-039-005)

Gates Compliance Pack features behind a valid license key.

Premium features gated:
  - audit-export (/audit-export command → audit_exporter.py)
  - SBOM generation (/sbom command → sbom_generator.py)
  - Extended secret scanning (professional patterns in secrets_scanner.py)
  - SOC 2 control mapping

Delegates validation to license_utils.py which supports:
  - forge-license.json (Ed25519 signed, new format) — primary
  - FORGE-TIER-EXPIRY-HMAC (legacy HMAC, backward compat)
  - FORGE-trial-EXPIRY-trial (14-day trial keys, no HMAC required)

License storage (in priority order):
  $FORGE_LICENSE_FILE   → path to forge-license.json
  $FORGE_LICENSE_JSON   → inline JSON string
  ~/.forge/license.json → new JSON format
  ./.forge/license.json → project-local JSON format
  $FORGE_LICENSE_KEY    → legacy HMAC key env var
  ~/.forge/license.key  → legacy HMAC key file

Exit codes (per Forge hook protocol):
  0  — License valid, operation proceeds
  2  — License invalid/missing, operation blocked
"""

import json
import re as _re
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure license_utils (sibling module in .claude/hooks/) is importable
# ---------------------------------------------------------------------------
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

try:
    from license_utils import LicenseInfo, load_license

    _HAS_LICENSE_UTILS = True
except ImportError:
    _HAS_LICENSE_UTILS = False

# ---------------------------------------------------------------------------
# Backward-compatible public API (imported by secrets_scanner.py)
# ---------------------------------------------------------------------------


def validate_license() -> tuple[bool, str, str]:
    """
    Validate the current license.

    Returns:
        (is_valid, tier, message)
        Legacy tier names are preserved for callers that expect them:
          "community", "professional", "enterprise"

    Delegates to license_utils.load_license() when available.
    """
    if not _HAS_LICENSE_UTILS:
        return False, "community", "license_utils not available"

    info = load_license()
    # Map canonical tier names back to legacy names for backward compat
    _canonical_to_legacy = {
        "core": "community",
        "teams-starter": "professional",
        "teams-pro": "professional",
        "teams-business": "enterprise",
        "enterprise": "enterprise",
    }
    legacy_tier = _canonical_to_legacy.get(info.tier, info.tier)
    return info.valid, legacy_tier, info.message


def tier_allows(tier: str, required: str) -> bool:
    """
    Check if a tier satisfies a feature requirement.

    Accepts both legacy names (community/professional/enterprise) and
    canonical names (core/teams-starter/teams-pro/teams-business/enterprise).
    """
    # Normalize legacy → canonical names before comparison
    _LEGACY = {
        "community": "core",
        "professional": "teams-starter",
        "enterprise": "enterprise",
    }
    _ORDER = ["core", "teams-starter", "teams-pro", "teams-business", "enterprise"]
    tier_norm = _LEGACY.get(tier.lower(), tier.lower())
    required_norm = _LEGACY.get(required.lower(), required.lower())
    try:
        return _ORDER.index(tier_norm) >= _ORDER.index(required_norm)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Premium feature detection
# ---------------------------------------------------------------------------

# Regex: match only when a premium hook script is being *executed* (not merely
# mentioned as a test file path or in a string argument).
# Matches: uv run .claude/hooks/audit_exporter.py  OR
#          python .claude/hooks/audit_exporter.py   OR
#          /absolute/path/to/.claude/hooks/audit_exporter.py (as a direct call)
# Does NOT match: tests/test_audit_exporter.py or echo "hooks/audit_exporter.py"
_PREMIUM_INVOCATION_RE = _re.compile(
    r"(?:uv\s+run|python3?)\s+\S*hooks/(audit_exporter|sbom_generator)\.py"
    r"|(?<!\w)\.claude/hooks/(audit_exporter|sbom_generator)\.py(?!\s*['\"])"
)

# Feature flag used for the compliance pack bundle
COMPLIANCE_PACK_FEATURE = "compliance-pack"

# Minimum tier required (legacy name) for professional scripts (kept for callers)
PROFESSIONAL_SCRIPTS = ["hooks/audit_exporter.py", "hooks/sbom_generator.py"]
_REQUIRED_TIER = "professional"


def _is_license_sufficient(info: "LicenseInfo | None") -> bool:
    """Return True if the license grants access to Compliance Pack features."""
    if info is None:
        return False
    # Expired or otherwise invalid licenses are always insufficient
    if not info.valid:
        return False
    # New format: check explicit feature flag
    if info.has_feature(COMPLIANCE_PACK_FEATURE) or info.has_feature("audit-export"):
        return True
    # Legacy format (teams-starter maps to professional features)
    return info.satisfies_tier("teams-starter")


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """PreToolUse hook: gate premium Compliance Pack features."""
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        # Can't read input — fail open for safety
        return 0

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Only intercept Bash tool calls
    if tool_name != "Bash":
        return 0

    command = tool_input.get("command", "")

    # Check if this Bash command *executes* a premium hook script.
    # Regex match prevents false positives from test file paths (e.g.
    # tests/test_audit_exporter.py) or file-list strings.
    m = _PREMIUM_INVOCATION_RE.search(command)
    if not m:
        return 0

    # Determine feature name for error messages
    matched_script = (m.group(1) or m.group(2) or "").lower()
    feature_name = "Compliance Pack"
    if "audit_exporter" in matched_script:
        feature_name = "Compliance Pack — Audit Export (/audit-export)"
    elif "sbom_generator" in matched_script:
        feature_name = "Compliance Pack — SBOM Generation (/sbom)"

    # --- Validate license ---
    if _HAS_LICENSE_UTILS:
        info = load_license()
        license_ok = _is_license_sufficient(info)
        status_message = info.message
    else:
        # Fallback: use legacy validate_license shim
        is_valid, tier, status_message = validate_license()
        license_ok = is_valid and tier_allows(tier, _REQUIRED_TIER)

    if license_ok:
        return 0

    # --- BLOCK: Premium feature without valid license ---
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"{feature_name} requires a Forge Professional (or higher) license.\n\n"
                    f"Current status: {status_message}\n\n"
                    f"To activate:\n"
                    f"  1. Obtain a license key from https://forgelabs.dev/pricing\n"
                    f"  2. Save it: echo 'FORGE-professional-...' > ~/.forge/license.key\n"
                    f"  3. Or set: export FORGE_LICENSE_KEY='FORGE-professional-...'\n\n"
                    f"Free 14-day trial — full Compliance Pack access, no credit card:\n"
                    f"  export FORGE_LICENSE_KEY='FORGE-trial-{date.today()}-trial'"
                ),
            }
        ),
        file=sys.stdout,
    )
    print(
        f"[forge_license] BLOCKED: {feature_name} — {status_message}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
