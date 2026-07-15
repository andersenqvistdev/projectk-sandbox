"""
Forge License Utilities — Shared license validation module.

Provides feature flag checking for hooks that need to gate behavior based on
license tier. Supports both:
  - forge-license.json (Ed25519, schema_version "1") — primary format
  - FORGE-TIER-EXPIRY-HMAC legacy key — fallback for backward compatibility

Usage in hooks:
    from license_utils import check_feature, get_license_tier

    if not check_feature("extended-secret-scanning"):
        # Use community patterns only
        patterns = COMMUNITY_PATTERNS
    else:
        patterns = ALL_PATTERNS

Load order for forge-license.json:
  1. $FORGE_LICENSE_FILE env var (path to JSON file)
  2. $FORGE_LICENSE_JSON env var (inline JSON string)
  3. ~/.forge/license.json
  4. ./.forge/license.json

Legacy key load order:
  1. $FORGE_LICENSE_KEY env var
  2. ~/.forge/license.key

This module is imported by other hooks — it is NOT a UV script itself.
Ed25519 verification requires the 'cryptography' package. If unavailable,
signature verification is skipped (feature flags in the JSON are still honored,
but forgery protection is reduced). The primary gate hook (forge_license.py)
always performs full Ed25519 verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Public key for Ed25519 signature verification.
# Production: override via FORGE_LICENSE_PUBLIC_KEY_B64 (DER, base64-encoded).
# Default is the development/demo key from license-spec.md §5.
# ---------------------------------------------------------------------------
_DEFAULT_PUBLIC_KEY_B64 = "MCowBQYDK2VwAyEAVCIYPQVPMlv9+zndzPzYJjjlm9VzX8vLSpScBMmZHLo="
_PUBLIC_KEY_B64 = os.environ.get(
    "FORGE_LICENSE_PUBLIC_KEY_B64", _DEFAULT_PUBLIC_KEY_B64
)

# ---------------------------------------------------------------------------
# Tier ordering for both new and legacy formats
# ---------------------------------------------------------------------------
_NEW_TIER_ORDER = [
    "core",
    "teams-starter",
    "teams-pro",
    "teams-business",
    "enterprise",
]

_LEGACY_TIER_ORDER = ["community", "professional", "enterprise"]

# Map legacy tier → canonical new tier
_LEGACY_TO_NEW = {
    "community": "core",
    "professional": "teams-starter",
    "enterprise": "enterprise",
}

# Default features implied by tier (used when no explicit features array is present,
# e.g. legacy HMAC keys that don't carry feature flags).
_TIER_DEFAULT_FEATURES: dict[str, list[str]] = {
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

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_license_json() -> dict | None:
    """Attempt to load forge-license.json from standard locations."""
    # 1. Path from env var
    env_path = os.environ.get("FORGE_LICENSE_FILE", "").strip()
    if env_path:
        try:
            return json.loads(Path(env_path).read_text())
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Inline JSON from env var
    env_json = os.environ.get("FORGE_LICENSE_JSON", "").strip()
    if env_json:
        try:
            return json.loads(env_json)
        except json.JSONDecodeError:
            pass

    # 3. ~/.forge/license.json
    home_path = Path.home() / ".forge" / "license.json"
    if home_path.exists():
        try:
            return json.loads(home_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    # 4. ./.forge/license.json (project-local)
    local_path = Path(".forge") / "license.json"
    if local_path.exists():
        try:
            return json.loads(local_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    return None


def _load_legacy_key() -> str | None:
    """Load legacy HMAC-based license key."""
    env_key = os.environ.get("FORGE_LICENSE_KEY", "").strip()
    if env_key:
        return env_key
    legacy_path = Path.home() / ".forge" / "license.key"
    if legacy_path.exists():
        try:
            return legacy_path.read_text().strip()
        except OSError:
            pass
    return None


def _verify_ed25519(payload_bytes: bytes, signature_b64: str) -> bool:
    """Verify Ed25519 signature. Returns True if valid OR if cryptography not available."""
    try:
        import base64

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        pub_der = base64.b64decode(_PUBLIC_KEY_B64)
        public_key = load_der_public_key(pub_der)
        sig_bytes = base64.urlsafe_b64decode(signature_b64 + "==")
        public_key.verify(sig_bytes, payload_bytes)
        return True
    except ImportError:
        # cryptography not available — skip signature check, trust JSON presence
        return True
    except (InvalidSignature, Exception):
        return False


def _canonical_payload(license_data: dict) -> bytes:
    """Canonical JSON payload for signature verification (all fields except signature)."""
    payload = {k: v for k, v in license_data.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _check_expiry(valid_until: str) -> bool:
    """Return True if not expired."""
    if valid_until.lower() == "perpetual":
        return True
    try:
        expiry_date = datetime.strptime(valid_until, "%Y-%m-%d").date()
        return date.today() <= expiry_date
    except ValueError:
        return False


def _normalize_tier(tier: str) -> str:
    """Map legacy tier names to canonical new tier names."""
    return _LEGACY_TO_NEW.get(tier.lower(), tier.lower())


def _tier_satisfies(tier: str, required: str) -> bool:
    """Check if tier meets or exceeds required tier (using new tier ordering)."""
    tier_norm = _normalize_tier(tier)
    required_norm = _normalize_tier(required)
    try:
        return _NEW_TIER_ORDER.index(tier_norm) >= _NEW_TIER_ORDER.index(required_norm)
    except ValueError:
        return False


def _parse_legacy_key(key: str) -> tuple[str, str, str] | None:
    """Parse FORGE-TIER-EXPIRY-HMAC key. Returns (tier, expiry, hmac_hex) or None."""
    parts = key.strip().split("-")
    if len(parts) < 4 or parts[0] != "FORGE":
        return None
    tier = parts[1].lower()
    hmac_hex = parts[-1]
    expiry = "-".join(parts[2:-1]) if len(parts) > 4 else parts[2]
    return tier, expiry, hmac_hex


def _verify_legacy_hmac(tier: str, expiry: str, provided_hmac: str) -> bool:
    """Verify legacy HMAC-SHA256 signature.

    Fails closed: legacy keys can only be verified when the issuer's secret is
    explicitly provided via FORGE_LICENSE_SECRET. There is no default secret —
    a published default would let anyone mint valid legacy keys.
    """
    secret = os.environ.get("FORGE_LICENSE_SECRET", "")
    if not secret:
        return False
    message = f"{tier}:{expiry}".encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), provided_hmac.lower())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class LicenseInfo:
    """Validated license information."""

    def __init__(
        self,
        valid: bool,
        tier: str,
        features: list[str],
        message: str,
        source: str,
    ):
        self.valid = valid
        self.tier = tier  # Canonical new-format tier name
        self.features = features
        self.message = message
        self.source = source  # "json", "legacy", or "community"

    def has_feature(self, flag: str) -> bool:
        return flag in self.features

    def satisfies_tier(self, required: str) -> bool:
        return _tier_satisfies(self.tier, required)


def load_license() -> LicenseInfo:
    """
    Load and validate the current license.

    Priority:
    1. forge-license.json (Ed25519, new format)
    2. Legacy HMAC key
    3. Community fallback (no license)
    """
    # --- Try forge-license.json first ---
    license_data = _load_license_json()
    if license_data is not None:
        schema = license_data.get("schema_version")
        if schema != "1":
            return LicenseInfo(
                valid=False,
                tier="core",
                features=[],
                message=f"Unknown license schema_version: {schema!r}",
                source="json",
            )

        tier = license_data.get("tier", "core")
        valid_until = license_data.get("valid_until", "")
        features = license_data.get("features", [])
        signature = license_data.get("signature", "")

        if not _check_expiry(valid_until):
            return LicenseInfo(
                valid=False,
                tier="core",
                features=[],
                message=f"License expired on {valid_until}",
                source="json",
            )

        payload_bytes = _canonical_payload(license_data)
        if not _verify_ed25519(payload_bytes, signature):
            return LicenseInfo(
                valid=False,
                tier="core",
                features=[],
                message="License signature is invalid",
                source="json",
            )

        # M5: Check revocation list
        license_id = license_data.get("license_id")
        if license_id and is_license_revoked(license_id):
            return LicenseInfo(
                valid=False,
                tier="core",
                features=[],
                message=f"License {license_id} has been revoked",
                source="json",
            )

        canonical_tier = _normalize_tier(tier)
        return LicenseInfo(
            valid=True,
            tier=canonical_tier,
            features=features,
            message=f"Valid {tier} license (expires: {valid_until})",
            source="json",
        )

    # --- Try legacy HMAC key ---
    legacy_key = _load_legacy_key()
    if legacy_key:
        # Trial keys bypass HMAC check
        if legacy_key.startswith("FORGE-trial-"):
            parts = legacy_key.split("-")
            if len(parts) >= 3:
                expiry = parts[2] if len(parts) == 3 else "-".join(parts[2:-1])
                if _check_expiry(expiry):
                    trial_tier = "teams-pro"
                    return LicenseInfo(
                        valid=True,
                        tier=trial_tier,
                        features=_TIER_DEFAULT_FEATURES.get(trial_tier, []),
                        message=f"Trial license (expires: {expiry})",
                        source="legacy",
                    )
                return LicenseInfo(
                    valid=False,
                    tier="core",
                    features=[],
                    message="Trial license expired",
                    source="legacy",
                )

        parsed = _parse_legacy_key(legacy_key)
        if parsed:
            tier, expiry, hmac_hex = parsed
            if _verify_legacy_hmac(tier, expiry, hmac_hex) and _check_expiry(expiry):
                canonical_tier = _normalize_tier(tier)
                return LicenseInfo(
                    valid=True,
                    tier=canonical_tier,
                    features=_TIER_DEFAULT_FEATURES.get(canonical_tier, []),
                    message=f"Valid {tier} license (expires: {expiry})",
                    source="legacy",
                )

    # --- Community fallback ---
    return LicenseInfo(
        valid=False,
        tier="core",
        features=[],
        message="No valid license found (community tier)",
        source="community",
    )


# Cached result to avoid re-loading on every call within the same process
_cached_license: LicenseInfo | None = None


def get_license(force_reload: bool = False) -> LicenseInfo:
    """Get the current license, with caching."""
    global _cached_license
    if _cached_license is None or force_reload:
        _cached_license = load_license()
    return _cached_license


def check_feature(feature_flag: str) -> bool:
    """Return True if the current license grants the given feature flag."""
    return get_license().has_feature(feature_flag)


def get_license_tier() -> str:
    """Return the canonical tier name of the current license."""
    return get_license().tier


def satisfies_tier(required: str) -> bool:
    """Return True if the current license tier meets or exceeds the required tier."""
    return get_license().satisfies_tier(required)


# ---------------------------------------------------------------------------
# M5: Feature Gating Registry
# ---------------------------------------------------------------------------

# Maps feature flag -> (minimum_required_tier, description)
FEATURE_REGISTRY: dict[str, tuple[str, str]] = {
    # teams-starter features
    "audit-export": ("teams-starter", "Export audit trails for compliance reviews"),
    "sbom": ("teams-starter", "Generate Software Bill of Materials"),
    "extended-secret-scanning": (
        "teams-starter",
        "Advanced secret patterns beyond defaults",
    ),
    # teams-pro features
    "soc2-mapping": ("teams-pro", "SOC 2 control mapping and reports"),
    "compliance-pack": ("teams-pro", "Full Compliance Pack bundle"),
    "priority-support": ("teams-pro", "Priority support queue"),
    # teams-business features
    "multi-project": ("teams-business", "Multi-project company management"),
    "custom-agents": ("teams-business", "Custom agent definitions"),
    # enterprise features
    "sso": ("enterprise", "SAML/OIDC single sign-on"),
    "rbac": ("enterprise", "Role-based access control"),
    "self-hosted": ("enterprise", "Self-hosted deployment support"),
    "dedicated-support": ("enterprise", "Dedicated support engineer"),
    "advanced-rate-limiting": ("enterprise", "Custom rate limiting rules"),
}


class FeatureGateError(Exception):
    """Raised when a feature requires a higher license tier."""

    def __init__(self, feature: str, required_tier: str, current_tier: str):
        self.feature = feature
        self.required_tier = required_tier
        self.current_tier = current_tier
        super().__init__(
            f"Feature '{feature}' requires {required_tier} tier or higher. "
            f"Current tier: {current_tier}. Upgrade at https://forgeframework.dev/upgrade"
        )


def require_feature(feature: str) -> None:
    """
    Raise FeatureGateError if the current license does not grant the feature.

    Checks in order:
    1. Explicit feature in license.features array
    2. Tier-based access via FEATURE_REGISTRY
    """
    license_info = get_license()

    # Check explicit features first
    if license_info.has_feature(feature):
        return

    # Check tier-based access via registry
    if feature in FEATURE_REGISTRY:
        required_tier, _ = FEATURE_REGISTRY[feature]
        if license_info.satisfies_tier(required_tier):
            return
        raise FeatureGateError(feature, required_tier, license_info.tier)

    # Unknown feature — treat as requiring enterprise
    raise FeatureGateError(feature, "unknown", license_info.tier)


def get_available_features() -> list[str]:
    """
    Return list of all features available to the current license.

    Combines explicit features from license.features array with
    tier-implied features from FEATURE_REGISTRY.
    """
    license_info = get_license()
    features = set(license_info.features)

    # Add tier-implied features
    for feature, (required_tier, _) in FEATURE_REGISTRY.items():
        if license_info.satisfies_tier(required_tier):
            features.add(feature)

    return sorted(features)


def get_feature_info(feature: str) -> dict:
    """
    Get information about a feature including availability.

    Returns:
        {
            "feature": str,
            "available": bool,
            "required_tier": str | None,
            "description": str | None,
            "current_tier": str
        }
    """
    license_info = get_license()
    info = {
        "feature": feature,
        "available": False,
        "required_tier": None,
        "description": None,
        "current_tier": license_info.tier,
    }

    if feature in FEATURE_REGISTRY:
        required_tier, description = FEATURE_REGISTRY[feature]
        info["required_tier"] = required_tier
        info["description"] = description
        info["available"] = license_info.has_feature(
            feature
        ) or license_info.satisfies_tier(required_tier)
    else:
        info["available"] = license_info.has_feature(feature)

    return info


# ---------------------------------------------------------------------------
# M5: License Revocation
# ---------------------------------------------------------------------------

_revocation_list: set[str] | None = None
_revocation_list_loaded_at: float = 0
_REVOCATION_CACHE_TTL = 3600  # Reload revocation list every hour


def _load_revocation_list() -> set[str]:
    """Load the revocation list from file, with caching."""
    global _revocation_list, _revocation_list_loaded_at
    import time

    now = time.time()
    if (
        _revocation_list is not None
        and (now - _revocation_list_loaded_at) < _REVOCATION_CACHE_TTL
    ):
        return _revocation_list

    revoked = set()

    # Check file path from env or default locations
    revocation_file = os.environ.get("FORGE_REVOCATION_LIST_FILE", "").strip()
    if not revocation_file:
        # Check default locations
        for path in [
            Path.home() / ".forge" / "revocation.list",
            Path(".forge") / "revocation.list",
        ]:
            if path.exists():
                revocation_file = str(path)
                break

    if revocation_file:
        try:
            content = Path(revocation_file).read_text()
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    revoked.add(line)
        except OSError:
            pass

    _revocation_list = revoked
    _revocation_list_loaded_at = now
    return revoked


def is_license_revoked(license_id: str) -> bool:
    """Check if a license ID has been revoked."""
    if not license_id:
        return False
    return license_id in _load_revocation_list()


def revoke_license(license_id: str, revocation_file: str | None = None) -> bool:
    """
    Add a license ID to the revocation list.

    Args:
        license_id: The license ID to revoke
        revocation_file: Path to revocation file (default: ~/.forge/revocation.list)

    Returns:
        True if successfully added, False otherwise
    """
    if not license_id:
        return False

    if not revocation_file:
        revocation_file = str(Path.home() / ".forge" / "revocation.list")

    try:
        path = Path(revocation_file)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing entries
        existing = set()
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    existing.add(line)

        # Add new entry if not present
        if license_id not in existing:
            with open(path, "a") as f:
                f.write(f"{license_id}\n")

        # Clear cache to pick up new entry
        clear_revocation_cache()
        return True
    except OSError:
        return False


def clear_revocation_cache() -> None:
    """Clear the revocation list cache, forcing reload on next check."""
    global _revocation_list, _revocation_list_loaded_at
    _revocation_list = None
    _revocation_list_loaded_at = 0
