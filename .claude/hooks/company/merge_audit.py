"""
Merge audit chain — one JSONL line per daemon merge.

Each line captures the complete provenance of a merge:
    ts, task_id, pr, deliverable_verdict, ci_state, merge_lever

Missing fields are explicit JSON null values (never absent keys).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_AUDIT_REL = "state/merge_audit.jsonl"
_GATE_LOG_REL = "state/deliverable_gate.jsonl"


def _audit_path(company_dir: Path) -> Path:
    return company_dir / _AUDIT_REL


def lookup_deliverable_verdict(company_dir: Path, task_id: str | None) -> str | None:
    """Return the most-recent deliverable verdict for *task_id*.

    Returns 'PASS', 'BLOCK', 'ERROR', 'SKIP', or None when no entry is found.
    """
    if not task_id:
        return None
    gate_log = company_dir / _GATE_LOG_REL
    if not gate_log.exists():
        return None

    last: dict | None = None
    try:
        with open(gate_log, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("task_id") == task_id:
                    last = rec
    except OSError:
        return None

    if last is None:
        return None

    if last.get("skipped_reason"):
        return "SKIP"
    addresses = last.get("addresses_task")
    if addresses is True:
        return "PASS"
    if addresses is False:
        return "BLOCK"
    return "ERROR"  # addresses_task is None → judge error


def ci_state_from_gates(gate_results: dict | None) -> str | None:
    """Extract a CI state string from security-gate results.

    Returns 'pass', 'fail', 'pending', or None when not available.
    """
    if not gate_results:
        return None
    if gate_results.get("verdict") == "PENDING":
        return "pending"
    ci = gate_results.get("gates", {}).get("ci_checks")
    if ci is None:
        return None
    return "pass" if ci.get("passed") else "fail"


def append_merge_chain(
    company_dir: Path,
    *,
    task_id: str | None,
    pr: int | None,
    deliverable_verdict: str | None,
    ci_state: str | None,
    merge_lever: str | None,
) -> None:
    """Append one audit line to merge_audit.jsonl.

    All fields are always present; missing values are explicit null.
    Best-effort — never raises.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "pr": pr,
        "deliverable_verdict": deliverable_verdict,
        "ci_state": ci_state,
        "merge_lever": merge_lever,
    }
    path = _audit_path(company_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def load_audit_for_pr(company_dir: Path, pr_number: int) -> list[dict]:
    """Return all audit entries for *pr_number* in file order (oldest first)."""
    path = _audit_path(company_dir)
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("pr") == pr_number:
                    results.append(rec)
    except OSError:
        pass
    return results


def load_audit_for_task(company_dir: Path, task_id: str) -> list[dict]:
    """Return all audit entries for *task_id* in file order (oldest first)."""
    path = _audit_path(company_dir)
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("task_id") == task_id:
                    results.append(rec)
    except OSError:
        pass
    return results


def load_all_audit(company_dir: Path, *, newest_first: bool = True) -> list[dict]:
    """Return all audit entries, sorted newest-first by default."""
    path = _audit_path(company_dir)
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                results.append(rec)
    except OSError:
        pass
    if newest_first:
        results.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return results
