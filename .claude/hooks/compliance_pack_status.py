# /// script
# requires-python = ">=3.10"
# ///
"""
Forge Compliance Pack Status — License and feature health check.

Reports the current Compliance Pack license status and which features are
active or locked, optionally with hook presence verification.

Usage:
    uv run .claude/hooks/compliance_pack_status.py
    uv run .claude/hooks/compliance_pack_status.py --verify
    uv run .claude/hooks/compliance_pack_status.py --json

Exit codes:
    0 — License valid (all Pack features accessible)
    1 — No valid license (community tier or expired)
    2 — Verification failure (hooks missing or misconfigured)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure license_utils is importable from the same hooks directory
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
# Feature definitions: each Compliance Pack feature with its required tier
# ---------------------------------------------------------------------------

FEATURES = [
    {
        "id": "extended-secret-scanning",
        "name": "Extended Secret Scanning",
        "description": "23 patterns (8 community + 15 professional)",
        "required_tier": "teams-starter",
        "hook_file": ".claude/hooks/secrets_scanner.py",
        "command": None,
    },
    {
        "id": "audit-export",
        "name": "Audit Export",
        "description": "Compliance-ready audit packages with SHA-256 manifest",
        "required_tier": "teams-starter",
        "hook_file": ".claude/hooks/audit_exporter.py",
        "command": ".claude/commands/audit-export.md",
    },
    {
        "id": "sbom",
        "name": "SBOM Generation",
        "description": "CycloneDX/SPDX software bill of materials via Syft",
        "required_tier": "teams-starter",
        "hook_file": ".claude/hooks/sbom_generator.py",
        "command": ".claude/commands/sbom.md",
    },
    {
        "id": "soc2-mapping",
        "name": "SOC 2 Control Mapping",
        "description": "Formal auditor-ready SOC 2 Type 1 control mapping",
        "required_tier": "teams-pro",
        "hook_file": ".planning/G9-SOC2-MAPPING.md",
        "command": None,
    },
    {
        "id": "compliance-pack",
        "name": "Compliance Pack Bundle",
        "description": "Full compliance pack (audit-export + SBOM + extended scanning + SOC 2)",
        "required_tier": "teams-pro",
        "hook_file": None,
        "command": None,
    },
]

# Core hooks that should always be present (community tier)
CORE_HOOKS = [
    ".claude/hooks/secrets_scanner.py",
    ".claude/hooks/block_dangerous.py",
    ".claude/hooks/log_activity.py",
    ".claude/hooks/git_guardian.py",
    ".claude/hooks/forge_license.py",
    ".claude/hooks/license_utils.py",
]

# Tier display names
TIER_DISPLAY = {
    "core": "Core (Free)",
    "teams-starter": "Teams Starter",
    "teams-pro": "Teams Pro",
    "teams-business": "Teams Business",
    "enterprise": "Enterprise",
}

# Tier order for comparison
_TIER_ORDER = ["core", "teams-starter", "teams-pro", "teams-business", "enterprise"]


def _satisfies(current: str, required: str) -> bool:
    try:
        return _TIER_ORDER.index(current) >= _TIER_ORDER.index(required)
    except ValueError:
        return False


def _find_project_root() -> Path:
    """Walk up from hooks dir to find project root (contains .claude/)."""
    candidate = _HOOKS_DIR.parent.parent  # .claude/hooks/ -> .claude/ -> root
    if candidate.is_dir():
        return candidate
    return Path.cwd()


def build_status(project_root: Path, verify: bool) -> dict:
    """Build a full status dict for the Compliance Pack."""
    if not _HAS_LICENSE_UTILS:
        return {
            "error": "license_utils not available — run from project root",
            "licensed": False,
            "tier": "core",
            "features": [],
        }

    info: LicenseInfo = load_license()
    tier = info.tier  # canonical name
    tier_display = TIER_DISPLAY.get(tier, tier)

    features_status = []
    for feat in FEATURES:
        active = info.valid and (
            info.has_feature(feat["id"]) or _satisfies(tier, feat["required_tier"])
        )
        entry: dict = {
            "id": feat["id"],
            "name": feat["name"],
            "description": feat["description"],
            "required_tier": TIER_DISPLAY.get(
                feat["required_tier"], feat["required_tier"]
            ),
            "active": active,
        }
        if verify:
            hook_ok = True
            if feat["hook_file"]:
                hook_path = project_root / feat["hook_file"]
                hook_ok = hook_path.exists()
            cmd_ok = True
            if feat["command"]:
                cmd_path = project_root / feat["command"]
                cmd_ok = cmd_path.exists()
            entry["hook_present"] = hook_ok
            entry["command_present"] = cmd_ok
        features_status.append(entry)

    result: dict = {
        "licensed": info.valid,
        "tier": tier,
        "tier_display": tier_display,
        "license_message": info.message,
        "license_source": info.source,
        "features": features_status,
    }

    if verify:
        core_hook_status = []
        for hook in CORE_HOOKS:
            hook_path = project_root / hook
            core_hook_status.append(
                {
                    "file": hook,
                    "present": hook_path.exists(),
                }
            )
        result["core_hooks"] = core_hook_status
        all_core_ok = all(h["present"] for h in core_hook_status)
        result["verification_passed"] = all_core_ok

    return result


def print_human_readable(status: dict) -> None:
    """Print a formatted human-readable status report."""
    W = 65
    bar = "═" * W

    print(f"\n{bar}")
    print(" FORGE COMPLIANCE PACK STATUS")
    print(f"{bar}")

    if "error" in status:
        print(f" ERROR: {status['error']}")
        print(f"{bar}\n")
        return

    # License section
    tier_display = status.get("tier_display", status.get("tier", "Unknown"))
    licensed = status.get("licensed", False)
    source = status.get("license_source", "community")
    msg = status.get("license_message", "")

    license_label = "VALID" if licensed else "UNLICENSED"
    print(f" License:     {license_label}  ({source})")
    print(f" Tier:        {tier_display}")
    if msg:
        print(f" Status:      {msg}")
    print(f"{bar}")

    # Features section
    print(" FEATURES")
    print(f"{'─' * W}")
    for feat in status.get("features", []):
        active = feat["active"]
        icon = "✓" if active else "✗"
        lock = "" if active else f"  [requires {feat['required_tier']}]"
        name = feat["name"]
        desc = feat["description"]
        print(f" {icon} {name}{lock}")
        print(f"   {desc}")
        if "hook_present" in feat and not feat.get("hook_present", True):
            print("   ⚠ Hook file missing")
        if "command_present" in feat and not feat.get("command_present", True):
            print("   ⚠ Command file missing")
    print(f"{bar}")

    # Verification section
    if "core_hooks" in status:
        print(" CORE INFRASTRUCTURE VERIFICATION")
        print(f"{'─' * W}")
        for hook in status["core_hooks"]:
            icon = "✓" if hook["present"] else "✗"
            print(f" {icon} {hook['file']}")
        passed = status.get("verification_passed", False)
        verdict = "PASSED" if passed else "FAILED — missing files above"
        print(f"{'─' * W}")
        print(f" Verification: {verdict}")
        print(f"{bar}")

    # Upgrade CTA if unlicensed
    if not licensed:
        print(" UPGRADE")
        print(f"{'─' * W}")
        print(" Get Compliance Pack features from $99/month:")
        print("   https://forgelabs.dev/pricing")
        print("")
        print(" 14-day free trial (no credit card):")
        from datetime import date

        today = date.today().isoformat()
        print(f"   export FORGE_LICENSE_KEY='FORGE-trial-{today}-trial'")
        print(f"{bar}")

    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Forge Compliance Pack status")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify hook files and command files are present",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output machine-readable JSON",
    )
    args = parser.parse_args()

    project_root = _find_project_root()
    status = build_status(project_root, verify=args.verify)

    if args.json_output:
        print(json.dumps(status, indent=2))
    else:
        print_human_readable(status)

    # Exit codes
    if "verification_passed" in status and not status["verification_passed"]:
        return 2
    if not status.get("licensed", False):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
