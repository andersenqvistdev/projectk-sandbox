#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography"]
# ///
"""
forge_issue_license.py — ForgeLabs Internal License Issuance Tool

Generates signed forge-license.json files for distribution to customers.

Usage:
  # Issue a license (reads private key from FORGE_LICENSE_PRIVATE_KEY env var):
  FORGE_LICENSE_PRIVATE_KEY=<base64-pem> python forge_issue_license.py \\
    --org "Acme Corp" \\
    --org-id org_acme123 \\
    --tier teams-pro \\
    --valid-until 2026-12-31 \\
    --out forge-license.json

  # Issue with custom feature set (overrides tier defaults):
  FORGE_LICENSE_PRIVATE_KEY=<base64-pem> python forge_issue_license.py \\
    --org "Trial User" \\
    --org-id org_trial_abc \\
    --tier teams-starter \\
    --valid-until 2026-03-23 \\
    --features audit-export sbom \\
    --out forge-license.json

  # Generate a NEW development keypair and print to stdout:
  python forge_issue_license.py --generate-dev-keypair

Environment:
  FORGE_LICENSE_PRIVATE_KEY  Base64-encoded PEM private key (Ed25519, PKCS8, no passphrase)
  FORGE_LICENSE_PRIVATE_KEY_PATH  Path to PEM private key file (alternative to env var)

Security note:
  The private key MUST never be committed to git. Use the FORGE_LICENSE_PRIVATE_KEY
  env var injected from a secrets manager (AWS Secrets Manager, HashiCorp Vault, etc.).

Reference: .company/business/license-spec.md
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ---------------------------------------------------------------------------
# Tier → default features mapping (per license-spec.md §3)
# ---------------------------------------------------------------------------
TIER_FEATURES: dict[str, list[str]] = {
    "core": [],
    "teams-starter": [
        "audit-export",
        "sbom",
        "extended-secret-scanning",
    ],
    "teams-pro": [
        "audit-export",
        "sbom",
        "extended-secret-scanning",
        "soc2-mapping",
        "compliance-pack",
    ],
    "teams-business": [
        "audit-export",
        "sbom",
        "extended-secret-scanning",
        "soc2-mapping",
        "compliance-pack",
        "priority-support",
        "multi-project",
        "custom-agents",
    ],
    "enterprise": [
        "audit-export",
        "sbom",
        "extended-secret-scanning",
        "soc2-mapping",
        "compliance-pack",
        "priority-support",
        "multi-project",
        "custom-agents",
        "self-hosted",
        "dedicated-support",
        "sso",
        "advanced-rate-limiting",
    ],
}

VALID_TIERS = list(TIER_FEATURES.keys())

VALID_FEATURES = [
    "audit-export",
    "sbom",
    "extended-secret-scanning",
    "soc2-mapping",
    "compliance-pack",
    "priority-support",
    "multi-project",
    "custom-agents",
    "self-hosted",
    "dedicated-support",
    "sso",
    "advanced-rate-limiting",
]

SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


def _load_private_key_from_env() -> "Ed25519PrivateKey":
    """Load Ed25519 private key from FORGE_LICENSE_PRIVATE_KEY env var (base64-encoded PEM)."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    # Try base64-encoded PEM env var first
    raw_b64 = os.environ.get("FORGE_LICENSE_PRIVATE_KEY", "").strip()
    if raw_b64:
        try:
            pem_bytes = base64.b64decode(raw_b64)
            return load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            _die(f"Failed to decode FORGE_LICENSE_PRIVATE_KEY: {e}")

    # Try path env var
    key_path = os.environ.get("FORGE_LICENSE_PRIVATE_KEY_PATH", "").strip()
    if key_path:
        try:
            pem_bytes = Path(key_path).read_bytes()
            return load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            _die(f"Failed to load private key from {key_path}: {e}")

    _die(
        "No private key found. Set FORGE_LICENSE_PRIVATE_KEY (base64-encoded PEM) "
        "or FORGE_LICENSE_PRIVATE_KEY_PATH (path to PEM file)."
    )


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Payload construction and signing
# ---------------------------------------------------------------------------


def build_payload(
    org: str,
    org_id: str,
    tier: str,
    valid_until: str,
    features: list[str] | None = None,
) -> dict:
    """Construct the canonical license payload (without signature)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "org": org,
        "org_id": org_id,
        "tier": tier,
        "valid_until": valid_until,
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "features": features if features is not None else TIER_FEATURES.get(tier, []),
    }


def sign_payload(payload: dict, private_key: "Ed25519PrivateKey") -> str:
    """
    Sign the canonical payload per license-spec.md §4.

    Canonical form: all fields except 'signature', sorted keys, no whitespace, UTF-8.
    Returns base64url-encoded signature (no padding).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    signature_bytes = private_key.sign(canonical)
    return base64.urlsafe_b64encode(signature_bytes).rstrip(b"=").decode("ascii")


def issue_license(
    org: str,
    org_id: str,
    tier: str,
    valid_until: str,
    features: list[str] | None,
    out_path: Path,
    private_key: "Ed25519PrivateKey",
) -> dict:
    """Build, sign, and write a forge-license.json file. Returns the license dict."""
    payload = build_payload(org, org_id, tier, valid_until, features)
    payload["signature"] = sign_payload(payload, private_key)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# Dev keypair generation
# ---------------------------------------------------------------------------


def generate_dev_keypair() -> None:
    """
    Generate a new Ed25519 development keypair and print to stdout.

    Public key (DER, base64) → paste into license_utils.py _DEFAULT_PUBLIC_KEY_B64
    Private key (PEM, base64) → set as FORGE_LICENSE_PRIVATE_KEY env var
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    pub_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode("ascii")

    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    priv_b64 = base64.b64encode(priv_pem).decode("ascii")

    print("=" * 70)
    print("DEVELOPMENT KEYPAIR — DO NOT USE IN PRODUCTION")
    print("=" * 70)
    print()
    print("Public key (DER, base64) — paste into license_utils.py:")
    print(f"  _DEFAULT_PUBLIC_KEY_B64 = {pub_b64!r}")
    print()
    print("Private key (base64-encoded PEM) — set as env var:")
    print(f"  export FORGE_LICENSE_PRIVATE_KEY={priv_b64!r}")
    print()
    print("WARNING: Keep the private key secret. Never commit it to git.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_date(value: str) -> str:
    if value.lower() == "perpetual":
        return "perpetual"
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}. Use YYYY-MM-DD or 'perpetual'."
        )


def _generate_trial_org_id() -> str:
    """Generate unique trial org_id: org_trial_{timestamp}_{random}."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    return f"org_trial_{ts}_{rand}"


def _compute_trial_expiry() -> str:
    """Compute trial expiry date (14 days from now)."""
    expiry = datetime.now(timezone.utc) + timedelta(days=14)
    return expiry.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ForgeLabs license issuance tool. Generates signed forge-license.json files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    sub = parser.add_subparsers(dest="command")

    # --- issue (default action when no subcommand given) ---
    # Main args at top level for ergonomic use
    parser.add_argument(
        "--org", help="Human-readable organization name (e.g. 'Acme Corp')"
    )
    parser.add_argument(
        "--org-id",
        dest="org_id",
        help="Stable unique org identifier (e.g. org_acme123)",
    )
    parser.add_argument(
        "--tier",
        choices=VALID_TIERS,
        help="License tier",
    )
    parser.add_argument(
        "--valid-until",
        dest="valid_until",
        type=_validate_date,
        help="Expiry date (YYYY-MM-DD) or 'perpetual'",
    )
    parser.add_argument(
        "--features",
        nargs="+",
        metavar="FEATURE",
        help=(
            "Explicit feature flags (overrides tier defaults). "
            f"Valid: {', '.join(VALID_FEATURES)}"
        ),
    )
    parser.add_argument(
        "--out",
        default="forge-license.json",
        help="Output path for the license file (default: forge-license.json)",
    )

    # --- generate-dev-keypair subcommand ---
    sub.add_parser(
        "generate-dev-keypair",
        help="Generate a new Ed25519 development keypair and print to stdout",
    )

    # Also accept --generate-dev-keypair as a flag for convenience
    parser.add_argument(
        "--generate-dev-keypair",
        action="store_true",
        help="Generate a new Ed25519 development keypair and print to stdout",
    )

    # --- trial license shortcut ---
    parser.add_argument(
        "--trial",
        action="store_true",
        help=(
            "Issue a 14-day trial license (teams-pro tier, all features). "
            "Auto-fills org, org-id, tier, features, and valid-until. "
            "Accepts --org and --valid-until overrides."
        ),
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Handle keypair generation
    if args.command == "generate-dev-keypair" or getattr(
        args, "generate_dev_keypair", False
    ):
        generate_dev_keypair()
        return

    # Handle --trial mode: auto-fill fields, validate conflicts
    is_trial = getattr(args, "trial", False)
    if is_trial:
        if args.tier:
            _die(
                "--trial cannot be combined with --tier. "
                "Use manual issuance for custom-tier trials."
            )
        if args.features:
            _die(
                "--trial cannot be combined with --features. "
                "Use manual issuance for custom-feature trials."
            )
        if args.org_id:
            _die("--trial auto-generates org-id for uniqueness. Omit --org-id.")
        args.org = args.org or "Trial User"
        args.org_id = _generate_trial_org_id()
        args.tier = "teams-pro"
        args.features = list(TIER_FEATURES["teams-pro"])
        args.valid_until = args.valid_until or _compute_trial_expiry()

    # Validate required fields for license issuance
    missing = [
        name
        for name, val in [
            ("--org", args.org),
            ("--org-id", args.org_id),
            ("--tier", args.tier),
            ("--valid-until", args.valid_until),
        ]
        if not val
    ]
    if missing:
        parser.error(
            f"The following arguments are required for license issuance: {', '.join(missing)}"
        )

    # Validate custom features if provided
    if args.features:
        unknown = [f for f in args.features if f not in VALID_FEATURES]
        if unknown:
            _die(
                f"Unknown feature flag(s): {', '.join(unknown)}\n"
                f"Valid flags: {', '.join(VALID_FEATURES)}"
            )

    # Load private key
    try:
        import cryptography  # noqa: F401
    except ImportError:
        _die(
            "The 'cryptography' package is required. Install with: pip install cryptography"
        )

    private_key = _load_private_key_from_env()

    out_path = Path(args.out)
    license_data = issue_license(
        org=args.org,
        org_id=args.org_id,
        tier=args.tier,
        valid_until=args.valid_until,
        features=args.features,
        out_path=out_path,
        private_key=private_key,
    )

    features = license_data["features"]
    print(f"License issued: {out_path}")
    print(f"  Org:      {args.org} ({args.org_id})")
    print(f"  Tier:     {args.tier}")
    print(f"  Expiry:   {args.valid_until}")
    print(f"  Features: {', '.join(features) if features else '(none)'}")
    print(f"  Issued:   {license_data['issued_at']}")

    if is_trial:
        print()
        print("=" * 70)
        print("TRIAL LICENSE ACTIVATION")
        print("=" * 70)
        print()
        print("  1. Copy the license file to ~/.forge/license.json:")
        print(f"     cp {out_path} ~/.forge/license.json")
        print()
        print("  2. Or set the env var for CI/CD:")
        print(f"     export FORGE_LICENSE_FILE={out_path}")
        print()
        print(f"  Trial expires: {args.valid_until}")
        print("  Upgrade:       https://forgelabs.dev/pricing")
        print()
        print("=" * 70)


if __name__ == "__main__":
    main()
