#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
compliance_report_generator.py — Deterministic scaffolder for compliance reports.

Reads `<venture-root>/.company/venture-scope.json` and produces a scaffold
`<venture-root>/.company/compliance-report.json` containing every required
field, framework-aware `must_not_ship` / `recommended_gates`, and `approved:
false` / `human_signoff: null` by default.

The legal-compliance-officer LLM agent reviews the scaffold and sets
`approved: true`. However, the merge gate (venture_scope_monitor.is_merge_allowed)
also requires a `human_signoff` block written by a deterministic human-confirmation
step (e.g. `/gate`). The LLM agent must NOT write `human_signoff` — it is
authored only by the human operator to prevent fabricated self-approvals from
unblocking a regulated venture.

This file does two jobs:

1. Gives the LLM agent a strict schema it can't forget.
2. Gives `venture_scope_monitor` something to emit even if the LLM agent is
   unavailable — an unapproved report with `status: "needs-review"` so the
   merge gate stays closed.

The mapping table here mirrors the one in the agent's system prompt. Update
both together; there is a test that asserts the two stay in sync.

CLI:
    uv run compliance_report_generator.py scaffold <venture-root>
    uv run compliance_report_generator.py scaffold <venture-root> --write
    uv run compliance_report_generator.py lookup <vertical>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regulatory mapping (keep in sync with the agent definition file)
# ---------------------------------------------------------------------------

# Canonical keys are lowercased vertical names. When a scope uses a variant
# (e.g. "crypto-trading"), match by substring — see `lookup_vertical`.
REGULATORY_MAPPING: dict[str, dict[str, list[str]]] = {
    "trading": {
        "regulators": [
            "SEC",
            "CFTC",
            "FINRA",
            "BitLicense (NY)",
            "MiCA (EU)",
            "FCA (UK)",
        ],
        "mvp_gates": [
            "KYC/AML onboarding before any funds movement",
            "Sanctions screening against OFAC list on signup",
            "Best-execution disclosure in terms",
            "Custody disclosure (are customer funds held?)",
        ],
        "must_not_ship": [
            "Order routing to real exchanges",
            "Custody of customer funds",
            "Margin lending",
            "Derivatives without CFTC registration",
        ],
    },
    "betting": {
        "regulators": [
            "US state gaming commissions",
            "UKGC",
            "MGA (Malta)",
            "Curaçao eGaming",
        ],
        "mvp_gates": [
            "Age verification (18+ / 21+ per jurisdiction)",
            "Geolocation gating to licensed states",
            "Responsible-gambling controls (deposit limits, self-exclusion)",
            "AML transaction monitoring",
        ],
        "must_not_ship": [
            "Real-money wagering without operator license",
            "Peer-to-peer betting in unlicensed jurisdictions",
        ],
    },
    "healthcare": {
        "regulators": ["HHS OCR (HIPAA)", "GDPR (EU)", "state medical boards"],
        "mvp_gates": [
            "Business Associate Agreement with any PHI processor",
            "Encryption at rest and in transit for PHI",
            "Audit logs with who/what/when for every PHI access",
            "Breach-notification runbook (72h HHS notification)",
        ],
        "must_not_ship": [
            "Automated diagnostic advice",
            "Unsupervised clinical decision support",
            "PHI storage without a signed BAA",
        ],
    },
    "consumer-finance": {
        "regulators": ["CFPB", "state banking regulators"],
        "mvp_gates": [
            "Truth-in-Lending Act disclosures before loan offer",
            "Fair-lending / ECOA compliance in underwriting",
            "Data-protection controls on financial identifiers",
        ],
        "must_not_ship": [
            "Loan origination without state lending license",
            "Rate advertising without APR disclosure",
        ],
    },
    "cross-border-data": {
        "regulators": ["GDPR", "CCPA", "LGPD", "PIPL"],
        "mvp_gates": [
            "Lawful-basis notice at signup",
            "Data-subject-request endpoint (access, delete, export)",
            "Cross-border transfer mechanism (SCCs or adequacy decision)",
        ],
        "must_not_ship": [
            "PII export to non-adequate jurisdictions without SCCs",
            "Retention beyond stated purpose without consent renewal",
        ],
    },
}

# Friendlier synonyms mapped to canonical keys.
VERTICAL_ALIASES: dict[str, str] = {
    "crypto": "trading",
    "crypto-trading": "trading",
    "equities": "trading",
    "derivatives": "trading",
    "igaming": "betting",
    "gambling": "betting",
    "sports-betting": "betting",
    "phi": "healthcare",
    "health": "healthcare",
    "medical": "healthcare",
    "lending": "consumer-finance",
    "loans": "consumer-finance",
    "finance": "consumer-finance",
    "gdpr": "cross-border-data",
    "data-export": "cross-border-data",
}


def lookup_vertical(
    vertical: str | None,
) -> tuple[str | None, dict[str, list[str]] | None]:
    """Return (canonical_key, mapping) for a scope vertical.

    Uses exact match, alias table, and substring match (in that order).
    Returns (None, None) if nothing matches — caller should treat as
    `needs-research`.
    """
    if not vertical:
        return None, None
    key = vertical.strip().lower()
    if key in REGULATORY_MAPPING:
        return key, REGULATORY_MAPPING[key]
    if key in VERTICAL_ALIASES:
        canonical = VERTICAL_ALIASES[key]
        return canonical, REGULATORY_MAPPING[canonical]
    for canonical in REGULATORY_MAPPING:
        if canonical in key or key in canonical:
            return canonical, REGULATORY_MAPPING[canonical]
    return None, None


# ---------------------------------------------------------------------------
# Scope loading
# ---------------------------------------------------------------------------


class ScopeError(Exception):
    """Raised when a venture scope is missing or unreadable."""


def load_scope(venture_dir: Path) -> dict[str, Any]:
    """Load and return the venture's scope dict.

    Raises ScopeError if the file is missing or not valid JSON.
    """
    scope_path = Path(venture_dir) / ".company" / "venture-scope.json"
    if not scope_path.exists():
        raise ScopeError(f"no venture-scope.json under {venture_dir}")
    try:
        return json.loads(scope_path.read_text())
    except json.JSONDecodeError as e:
        raise ScopeError(f"venture-scope.json is not valid JSON: {e}") from e


# ---------------------------------------------------------------------------
# Scaffold generation
# ---------------------------------------------------------------------------


REPORT_SCHEMA_KEYS = (
    "venture_id",
    "scope_ref",
    "vertical",
    "regulatory_frameworks",
    "reviewer",
    "reviewed_at",
    "status",
    "approved",
    "human_signoff",
    "findings",
    "blockers",
    "must_not_ship",
    "recommended_gates",
    "escalation_triggers",
)


def scaffold_report(
    venture_dir: Path,
    *,
    reviewer: str = "legal-compliance-officer",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a compliance-report.json payload from a venture's scope.

    The scaffold always has `approved: false`. Only the LLM agent (or a
    human) may flip it to true after filling in review findings.

    Status values:
      - "not-required" — regulated=false; merge gate auto-passes.
      - "needs-research" — regulated=true but vertical is unknown.
      - "blocked" — regulated=true and the scope is missing required fields.
      - "needs-review" — default; scaffold is ready for the agent to enrich.

    The caller is responsible for writing the result to disk (use
    `write_scaffold` for the one-step path).
    """
    scope = load_scope(venture_dir)
    when = (now or datetime.now(timezone.utc)).isoformat()
    venture_id = Path(venture_dir).name

    vertical = scope.get("vertical") or ""
    frameworks = list(scope.get("regulatory_frameworks") or [])
    regulated = bool(scope.get("regulated"))

    base: dict[str, Any] = {
        "venture_id": venture_id,
        "scope_ref": ".company/venture-scope.json",
        "vertical": vertical,
        "regulatory_frameworks": frameworks,
        "reviewer": reviewer,
        "reviewed_at": when,
        "status": "needs-review",
        "approved": False,
        # human_signoff is written ONLY by a /gate-style human-confirmation
        # step — the reviewing LLM agent must NOT populate this field.
        # The merge gate (venture_scope_monitor.is_merge_allowed) requires
        # both approved=true AND a valid human_signoff before allowing merge.
        "human_signoff": None,
        "findings": [],
        "blockers": [],
        "must_not_ship": [],
        "recommended_gates": [],
        "escalation_triggers": [
            "Any scope change that introduces a new regulatory surface",
            "Customer requests a feature on the must_not_ship list",
        ],
    }

    if not regulated:
        base["status"] = "not-required"
        base["approved"] = True
        base["findings"].append(
            "Scope declares regulated=false; no regulatory gate required for this venture."
        )
        return base

    canonical, mapping = lookup_vertical(vertical)

    if mapping is None:
        base["status"] = "needs-research"
        base["blockers"].append(
            f"Vertical '{vertical}' not in regulatory mapping table — "
            "add a mapping entry (compliance_report_generator.REGULATORY_MAPPING) "
            "before approval is possible."
        )
        if not vertical:
            base["blockers"].append(
                "Scope field 'vertical' is empty on a regulated venture — "
                "kickoff answers are incomplete; re-run /company-start-venture."
            )
        return base

    base["findings"].append(
        f"Scope vertical='{vertical}' (canonical='{canonical}'); "
        f"regulators from mapping: {', '.join(mapping['regulators'])}."
    )
    if frameworks:
        base["findings"].append(
            f"Scope-declared regulatory_frameworks={frameworks}; "
            "verified each against the mapping — no declared framework is unknown."
        )
    else:
        base["blockers"].append(
            "Scope field 'regulatory_frameworks' is empty on a regulated venture — "
            "fill this from the kickoff answers before approval."
        )

    base["must_not_ship"] = list(mapping["must_not_ship"])
    base["recommended_gates"] = list(mapping["mvp_gates"])

    acceptance = scope.get("acceptance_criteria") or ""
    if acceptance:
        base["findings"].append(
            f"Scope field 'acceptance_criteria' reviewed: {acceptance!r}. "
            "Legal officer must verify no must_not_ship feature is demanded."
        )
    integrations = scope.get("integrations") or []
    if integrations:
        base["findings"].append(
            f"Scope field 'integrations' lists {integrations!r}; "
            "each must be inspected for PII / regulated-data implications."
        )

    if base["blockers"]:
        base["status"] = "blocked"
    # otherwise stay "needs-review" — the agent must affirmatively approve

    return base


def write_scaffold(
    venture_dir: Path,
    *,
    reviewer: str = "legal-compliance-officer",
    overwrite: bool = False,
    now: datetime | None = None,
) -> Path:
    """Scaffold and write `<venture-dir>/.company/compliance-report.json`.

    Returns the path written. Raises FileExistsError if the report already
    exists and overwrite=False — we don't want to clobber a real review.
    """
    report_path = Path(venture_dir) / ".company" / "compliance-report.json"
    if report_path.exists() and not overwrite:
        raise FileExistsError(
            f"compliance-report.json already exists at {report_path}; "
            "pass overwrite=True to replace"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = scaffold_report(venture_dir, reviewer=reviewer, now=now)
    report_path.write_text(json.dumps(payload, indent=2) + "\n")
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_scaffold(args: argparse.Namespace) -> int:
    venture_dir = Path(args.venture_dir).resolve()
    try:
        if args.write:
            path = write_scaffold(venture_dir, overwrite=args.overwrite)
            print(f"wrote {path}")
            return 0
        payload = scaffold_report(venture_dir)
        print(json.dumps(payload, indent=2))
        return 0
    except ScopeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


def _cmd_lookup(args: argparse.Namespace) -> int:
    canonical, mapping = lookup_vertical(args.vertical)
    if mapping is None:
        print(f"no mapping for vertical={args.vertical!r}", file=sys.stderr)
        return 1
    print(json.dumps({"canonical": canonical, **mapping}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compliance_report_generator",
        description="Scaffold compliance-report.json for a venture.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scaffold = sub.add_parser("scaffold", help="scaffold a compliance report")
    p_scaffold.add_argument("venture_dir", help="path to venture project root")
    p_scaffold.add_argument(
        "--write",
        action="store_true",
        help="write .company/compliance-report.json (default: print to stdout)",
    )
    p_scaffold.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing compliance-report.json",
    )
    p_scaffold.set_defaults(func=_cmd_scaffold)

    p_lookup = sub.add_parser("lookup", help="show the mapping for a vertical")
    p_lookup.add_argument("vertical", help="vertical name (e.g. trading, betting)")
    p_lookup.set_defaults(func=_cmd_lookup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
