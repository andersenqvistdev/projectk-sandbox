#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Deliverable Judge — single-PR pre-merge "does the diff address the task?" gate.

Phase 2 of the autonomy plan (.planning/autonomy/phase-2-premerge-deliverable-gate.md).

CI proves a diff is *safe*, not that it *does what the task asked*. The Tier-2
semantic judge (`.claude/workflows/calibrate.js`) catches the phantom-merge class
(e.g. #1012 — re-documented an already-documented function) but today only weeks
later as an AUDIT. This module promotes that exact adversarial check to a
PRE-MERGE GATE: given one (task_id, title, description, pr_number) it asks a
skeptical judge whether the PR's diff substantively addresses the task.

Design invariants (see the phase-2 spec):
  * Reuse calibrate.js's adversarial framing + decision rule + schema. The shared
    sentences live in the constants below and a regression test
    (tests/test_deliverable_judge.py) asserts they appear verbatim in calibrate.js,
    so the two stay one source of truth even though the JS workflow sandbox cannot
    import this module.
  * Fail-CLOSED-to-REVIEW. Judge errors / rate-limits / unparseable output =>
    blocked + needs-manual-review. NEVER auto-reject and NEVER auto-close the work
    on a judge failure (we have repeatedly fought auto-close levers — see MEMORY).
  * Additive. This composes with the existing allowMergeToMain / human-approval
    auto-merge gates; it never creates a path that bypasses them.
  * Cost-aware. One agent per PR, gated to daemon/* (and forge/*) branches only,
    and the verdict is cached by the PR head commit sha so re-polls / CI re-runs
    do not re-call the LLM.

The gate bites in two places:
  1. The daemon Python path (pr_output_manager.execute_auto_pr_workflow) runs the
     judge right after creating a daemon PR and, on a block verdict, applies the
     ``needs-manual-review`` label + posts the reason as a PR comment.
  2. ci.yml's auto-merge job refuses to enable auto-merge on any daemon PR that
     carries ``needs-manual-review`` — so the label set here closes the real
     (CI) auto-merge lever too.

Usage:
    python deliverable_judge.py judge --task-id T --pr-number 123 \
        --title "..." --description "..."
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

try:
    from . import company_resolver as _company_resolver
except ImportError:  # direct script execution
    import company_resolver as _company_resolver

try:
    from . import agent_providers as _agent_providers
except ImportError:  # direct script execution
    import agent_providers as _agent_providers

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# =============================================================================
# Shared prompt — single source of truth with calibrate.js
# =============================================================================
# These two strings are duplicated VERBATIM in .claude/workflows/calibrate.js.
# tests/test_deliverable_judge.py asserts they still appear there, so editing the
# adversarial framing in one place forces the other to be updated too. (The JS
# Workflow sandbox has no filesystem access, so it cannot import this module.)

ADVERSARIAL_FRAMING = (
    "You are ADVERSARIALLY verifying whether a merged pull request genuinely "
    "delivers the task it claims to. Be skeptical: a merged PR is NOT proof the "
    "task was done — the diff can be trivial, unrelated, a rubber-stamp, doc-only "
    "when code was required, or address a different problem."
)

DECISION_RULE = (
    "Set addresses_task=true ONLY if the diff substantively does what the task "
    "describes. Default to addresses_task=false if the change is "
    "trivial/unrelated/rubber-stamp, or if you cannot confirm from the diff."
)

# JSON Schema for a single verdict — identical shape to calibrate.js's SCHEMA.
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "addresses_task": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["task_id", "addresses_task", "confidence", "reason"],
}

# Identity strings unique to the Forge framework's own documentation. The
# tripwire fires when any of these appear in added diff lines inside a repo
# that is NOT the framework itself — it means a worker copied the framework
# README verbatim into a product repo.
FRAMEWORK_IDENTITY_MARKERS: tuple[str, ...] = ("Forge — Structured Autonomy",)

# Exact org.json company names that identify the framework's own repo. Any
# product repo (e.g. ledgy-cli, forge-academy) is NOT exempt — use exact
# match, not substring, to avoid false exemptions.
_FRAMEWORK_PRODUCT_NAMES: frozenset[str] = frozenset(
    {"forge", "forge framework", "forge-framework"}
)


# =============================================================================
# Configuration
# =============================================================================


def _gate_defaults() -> dict:
    return {
        "enabled": True,
        # addresses_task=true is only honoured at/above this confidence; below it
        # (or addresses_task=false) the PR is sent to manual review.
        "confidenceThreshold": 0.7,
        # Model alias/id passed to `claude --model`. null => omit the flag and let
        # the CLI use its configured default.
        "model": None,
        # On judge error / unparseable output: "block" (fail-closed-to-review,
        # the spec default) or "allow" (operator escape hatch if claude is not
        # reachable from the daemon host).
        "onJudgeError": "block",
        # Only daemon-authored branches are judged; human PRs are never gated.
        "branchPrefixes": ["daemon/", "forge/"],
        "timeoutSeconds": 240,
        # Diffs larger than this are truncated before being embedded in the prompt.
        "maxDiffChars": 60000,
        "label": "needs-manual-review",
    }


def load_gate_config() -> dict:
    """Load autonomy.deliverableGate config, falling back to defaults.

    forge-config.json (root) is canonical (#1052); the .claude/ copy is legacy.
    Reads root first. Unknown keys in config are ignored; missing keys default.
    """
    defaults = _gate_defaults()
    config_paths = [
        Path.cwd() / "forge-config.json",
        Path.cwd() / ".claude" / "forge-config.json",
    ]
    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                gate = config.get("autonomy", {}).get("deliverableGate", {})
                if isinstance(gate, dict):
                    return {k: gate.get(k, v) for k, v in defaults.items()}
            except (json.JSONDecodeError, OSError):
                pass
    return defaults


def _sanitize_prompt_field(value: str | None, max_len: int = 300) -> str | None:
    """Sanitize a user-sourced string before embedding it in an LLM prompt.

    Strips newlines (prevent prompt structure injection), carriage returns, and
    caps length. Prompt injection via these fields remains theoretically possible
    but this removes the most naive vectors (line-break-terminated injections).
    """
    if not value:
        return None
    sanitized = value.replace("\n", " ").replace("\r", " ").strip()[:max_len]
    return sanitized or None


def load_product_identity() -> dict:
    """Load product name/description from org.json.

    Returns a dict with keys ``product_name`` and ``product_description`` (both
    ``str | None``). Values are sanitized to remove newlines before being used in
    LLM prompts. Fail-open: returns Nones on any I/O or parse error so the judge
    can still run without product context.
    """
    result: dict = {"product_name": None, "product_description": None}
    try:
        org_json = _company_resolver.get_company_dir() / "org.json"
        if org_json.exists():
            with open(org_json, encoding="utf-8") as f:
                org_data = json.load(f)
            company = org_data.get("company", {})
            result["product_name"] = _sanitize_prompt_field(company.get("name"))
            result["product_description"] = _sanitize_prompt_field(
                company.get("description")
            )
    except (json.JSONDecodeError, OSError):
        pass
    return result


def _is_framework_repo() -> bool:
    """True if cwd is the forge-framework repo itself (via git remote URL).

    Uses the git remote URL rather than user-editable org.json so that the
    tripwire's exemption logic is deterministic and not bypassable by modifying
    project files. Falls back to False (tripwire stays active) on any error.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(Path.cwd()),
        )
        if result.returncode == 0:
            return "forge-framework" in result.stdout.strip().lower()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


# =============================================================================
# Verdict
# =============================================================================


@dataclass
class DeliverableVerdict:
    """Outcome of judging one PR.

    ``blocked`` is the single decision the gate acts on: when True the PR must NOT
    be auto-merged and should get the manual-review label + reason comment.
    ``addresses_task`` is None when the judge could not produce a verdict (error /
    unparseable / disabled-skip), which — under the default onJudgeError="block" —
    yields blocked=True (fail-closed-to-review).
    """

    task_id: str
    addresses_task: bool | None
    confidence: float | None
    reason: str
    blocked: bool
    needs_manual_review: bool
    error: str | None = None
    cached: bool = False
    sha: str | None = None
    skipped_reason: str | None = None
    pr_url: str | None = None


def _decide(addresses_task: bool | None, confidence: float | None, threshold: float):
    """Map a judge result to (blocked, needs_manual_review).

    Rule (per spec): allow only when addresses_task is True AND confidence is at
    or above the threshold. Everything else — false, low-confidence, or unknown —
    blocks and routes to manual review.
    """
    if addresses_task is True and (confidence is not None and confidence >= threshold):
        return False, False
    return True, True


# =============================================================================
# git / gh helpers
# =============================================================================


def _clean_child_env() -> dict:
    """Build a subprocess env safe for invoking the Claude CLI.

    Mirrors employee_activator's P71 handling: keep the full environment (the CLI
    needs auth vars we cannot enumerate) but strip the vars/paths that break a
    nested, UV-managed invocation.
    """
    child_env = dict(os.environ)
    problematic_prefixes = ("UV_", "VIRTUAL_ENV", "CLAUDECODE", "CLAUDE_CODE_")
    for key in list(child_env.keys()):
        if key.startswith(problematic_prefixes):
            del child_env[key]
    original_path = child_env.get("PATH", "")
    clean_path_parts = [
        p
        for p in original_path.split(":")
        if ".cache/uv/environments" not in p and "virtualenv" not in p.lower()
    ]
    child_env["PATH"] = (
        ":".join(clean_path_parts) if clean_path_parts else "/usr/bin:/bin"
    )
    child_env["TERM"] = "xterm-256color"
    child_env["LANG"] = "en_US.UTF-8"
    return {k: v for k, v in child_env.items() if v}


def _claude_cmd_prefix() -> list[str]:
    """Return the command prefix to run the Claude CLI.

    Prefer `uv run claude` (the project's canonical invocation) when uv is on PATH;
    otherwise fall back to a bare `claude` if available. Returns [] if neither is
    found so callers can fail-closed-to-review.
    """
    if shutil.which("uv"):
        return ["uv", "run", "claude"]
    if shutil.which("claude"):
        return ["claude"]
    return []


def get_pr_head_sha(pr_number: str | int) -> str | None:
    """Fetch the PR's head commit sha (for verdict caching)."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "-q",
                ".headRefOid",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            return sha or None
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def fetch_pr_context(
    pr_number: str | int, max_diff_chars: int
) -> tuple[str, str] | None:
    """Fetch PR metadata + diff text for embedding in the judge prompt.

    Returns (meta_json_str, diff_text) or None on failure. The diff is truncated
    to max_diff_chars with a marker so an enormous PR cannot blow the prompt.
    """
    try:
        meta = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "title,body,files,additions,deletions",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if meta.returncode != 0:
            return None
        diff = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if diff.returncode != 0:
            return None
        diff_text = diff.stdout
        if len(diff_text) > max_diff_chars:
            diff_text = (
                diff_text[:max_diff_chars]
                + f"\n\n... [diff truncated at {max_diff_chars} chars] ..."
            )
        return meta.stdout.strip(), diff_text
    except (subprocess.TimeoutExpired, OSError):
        return None


# =============================================================================
# Prompt + parsing
# =============================================================================


def build_judge_prompt(
    task_id: str,
    title: str,
    description: str,
    pr_number: str | int,
    meta_json: str,
    diff_text: str,
    *,
    product_name: str | None = None,
    product_description: str | None = None,
    goal_metric: str | None = None,
) -> str:
    """Build the single-PR judge prompt.

    The adversarial framing + decision rule are the SHARED constants (kept in sync
    with calibrate.js). Unlike calibrate.js — which has the agent run gh itself —
    the gate embeds the already-fetched diff so the judgment is deterministic and
    needs no tools.

    Optional keyword args ``product_name``, ``product_description``, and
    ``goal_metric`` anchor the judgment to THIS product's identity and goal. When
    provided the prompt explicitly asks "does the diff satisfy THIS metric for THIS
    product" rather than the generic "is this a plausible artifact".
    """
    # Build optional product-identity + goal-metric context block.
    # Sanitize all user-sourced strings to strip newlines before embedding.
    safe_name = _sanitize_prompt_field(product_name)
    safe_desc = _sanitize_prompt_field(product_description)
    safe_metric = _sanitize_prompt_field(goal_metric, max_len=500)
    context_parts: list[str] = []
    if safe_name or safe_desc:
        context_parts.append(f"PRODUCT: {safe_name or '(unknown)'}")
        if safe_desc:
            context_parts.append(f"PRODUCT DESCRIPTION: {safe_desc}")
    if safe_metric:
        context_parts.append(
            f"GOAL SUCCESS METRIC (the exact bar this diff must clear for this product):\n{safe_metric}"
        )
    context_block = ("\n\n" + "\n\n".join(context_parts)) if context_parts else ""
    metric_addendum = (
        "\nVerify the diff satisfies the above SUCCESS METRIC for the above PRODUCT "
        "exactly as stated — a plausible but wrong artifact (e.g. the framework's "
        "own README instead of this product's README) must be rejected."
        if context_parts
        else ""
    )

    return f"""{ADVERSARIAL_FRAMING}

This is a PRE-MERGE check on a daemon-authored pull request that is about to be
auto-merged. Judge the diff below; do not run any commands.

TASK {task_id}: {title or "(no title)"}
DESCRIPTION: {description or "(none)"}
PR NUMBER: {pr_number}{context_block}

PR METADATA (gh pr view --json title,body,files,additions,deletions):
{meta_json}

PR DIFF (gh pr diff):
```diff
{diff_text}
```

{DECISION_RULE}{metric_addendum}

Set task_id='{task_id}'. Give a one-sentence reason citing what the diff actually
changed. Respond with ONLY a single JSON object and nothing else, exactly:
{{"task_id": "{task_id}", "addresses_task": <true|false>, "confidence": <0..1>, "reason": "<one sentence>"}}"""


def parse_verdict_json(text: str) -> dict | None:
    """Extract the last well-formed verdict JSON object from CLI output.

    The CLI may wrap the answer in prose or markdown fences. Scan for balanced
    JSON objects and return the last one that carries an ``addresses_task`` key.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    found: dict | None = None
    idx = 0
    n = len(text)
    while idx < n:
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(obj, dict) and "addresses_task" in obj:
            found = obj
        idx = end
    return found


# =============================================================================
# Verdict cache (by PR head sha)
# =============================================================================


def _cache_path() -> Path:
    return _company_resolver.get_company_dir() / "state" / "deliverable_verdicts.json"


def _load_cache() -> dict:
    path = _cache_path()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _record_decision(verdict: DeliverableVerdict) -> None:
    """Append the verdict to a jsonl audit trail (best-effort)."""
    log_path = _company_resolver.get_company_dir() / "state" / "deliverable_gate.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **asdict(verdict)}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _record_skip(task_id: str, pr_url: str | None, reason: str) -> None:
    """Write an explicit skip entry to deliverable_gate.jsonl (best-effort).

    Called when the gate cannot run or deliberately does not run, so the audit
    log accounts for every daemon PR without a gap. Skip entries are
    distinguishable from genuine verdicts by ``skipped_reason is not None``
    and ``addresses_task is None``.
    """
    v = DeliverableVerdict(
        task_id=task_id,
        addresses_task=None,
        confidence=None,
        reason=reason,
        blocked=False,
        needs_manual_review=False,
        skipped_reason=reason,
        pr_url=pr_url,
    )
    _record_decision(v)


def maybe_record_antipattern(verdict: DeliverableVerdict, title: str) -> bool:
    """Feed a CONFIRMED phantom into the learning loop (Phase 3).

    Records a learned anti-pattern ONLY when the judge produced a genuine
    ``addresses_task is False`` verdict — i.e. the diff demonstrably does not address
    the task. Error / unparseable verdicts (``addresses_task is None``, which
    fail-closed-to-review) are NOT phantoms and must NEVER be recorded, or a flaky
    judge would poison the generator. Best-effort: never raises. Returns True if an
    anti-pattern entry was written/reinforced.
    """
    if verdict.addresses_task is not False:
        return False
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        entry = la.record_antipattern(
            kind=la.KIND_PHANTOM_MERGE,
            title=title or verdict.task_id,
            reason=verdict.reason or "pre-merge judge: diff did not address the task",
            source="deliverable_gate",
            example_task_id=verdict.task_id,
        )
        return entry is not None
    except Exception:
        return False


def _check_identity_tripwire(
    diff_text: str,
    product_name: str | None,
    *,
    is_framework_repo: bool = False,
) -> str | None:
    """Return an error message if framework identity appears in a non-framework diff.

    Fires when any ``FRAMEWORK_IDENTITY_MARKERS`` string appears in the added
    lines of the diff AND ``is_framework_repo`` is False. The exemption is keyed
    on ``is_framework_repo`` (derived from the git remote URL by callers, not from
    user-editable data) to prevent the tripwire from being bypassed by modifying
    org.json. Returns None if the diff is clean or if this is the framework's own
    repo. Never raises.
    """
    if is_framework_repo:
        return None
    added_lines = "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for marker in FRAMEWORK_IDENTITY_MARKERS:
        if marker in added_lines:
            return (
                f"tripwire: diff introduces framework identity marker '{marker}' "
                f"into product repo '{product_name or 'unknown'}' — "
                "the framework README was likely copied verbatim into a product repo"
            )
    return None


# =============================================================================
# Public API
# =============================================================================


def is_daemon_branch(branch: str | None, prefixes: list[str] | None = None) -> bool:
    """True if ``branch`` is a daemon-authored branch eligible for the gate."""
    if not branch:
        return False
    if prefixes is None:
        prefixes = _gate_defaults()["branchPrefixes"]
    return any(branch.startswith(p) for p in prefixes)


def _run_judge_cli(prompt: str, config: dict) -> tuple[str | None, str | None]:
    """Invoke the configured coding-agent provider as a read-only judge."""
    root_config: dict = {}
    # Same cwd-anchored two-path convention as load_gate_config(): the daemon
    # always runs the judge from the project root; root config is canonical
    # (#1052), the .claude/ copy is legacy.
    for config_path in (
        Path.cwd() / "forge-config.json",
        Path.cwd() / ".claude" / "forge-config.json",
    ):
        try:
            with open(config_path, encoding="utf-8") as handle:
                root_config = json.load(handle)
            break
        except (OSError, json.JSONDecodeError):
            continue

    try:
        provider = _agent_providers.resolve_provider(root_config, "trivial")
    except ValueError as exc:
        return None, f"judge provider configuration error: {exc}"

    if provider.provider_type == "codex":
        health = _agent_providers.check_provider_health(provider)
        if not health.healthy:
            return None, f"judge provider preflight failed: {health.message}"
        cmd = [
            "codex",
        ]
        if provider.reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{provider.reasoning_effort}"'])
        cmd.extend(
            [
                "--ask-for-approval",
                "never",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--cd",
                str(Path.cwd()),
            ]
        )
        if provider.model and provider.model != "default":
            cmd.extend(["--model", provider.model])
        cmd.append("-")
        assert_spawn_allowed("deliverable_judge._run_judge_cli", subprocess.run)
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=str(Path.cwd()),
                env=_agent_providers.prepare_environment(provider),
                timeout=int(config.get("timeoutSeconds", 240)),
            )
        except subprocess.TimeoutExpired:
            return None, "judge CLI timed out"
        except OSError as exc:
            return None, f"judge CLI failed to start: {exc}"
        output = _agent_providers.normalize_output(provider, result.stdout or "")
        if result.returncode != 0:
            detail = (result.stderr or "judge CLI returned non-zero").strip()[:500]
            return output, detail
        return output, None

    prefix = _claude_cmd_prefix()
    if not prefix:
        return None, "claude CLI not found on PATH"
    cmd = list(prefix)
    model = config.get("model")
    if model:
        cmd.extend(["--model", str(model)])
    cmd.extend(
        [
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ]
    )
    # 2026-07-06 fork-bomb guard: judge calls launch a real claude process.
    assert_spawn_allowed("deliverable_judge._run_judge_cli", subprocess.run)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(Path.cwd()),
            env=_clean_child_env(),
            timeout=int(config.get("timeoutSeconds", 240)),
        )
    except subprocess.TimeoutExpired:
        return None, "judge CLI timed out"
    except OSError as e:
        return None, f"judge CLI failed to start: {e}"
    if result.returncode != 0:
        return result.stdout, (result.stderr or "judge CLI returned non-zero").strip()[
            :500
        ]
    return result.stdout, None


def judge_pr_deliverable(
    task_id: str,
    title: str,
    description: str,
    pr_number: str | int,
    *,
    branch: str | None = None,
    config: dict | None = None,
    use_cache: bool = True,
    product_name: str | None = None,
    product_description: str | None = None,
    goal_metric: str | None = None,
) -> DeliverableVerdict:
    """Judge whether the PR's diff substantively addresses the task.

    Returns a DeliverableVerdict. On any failure the verdict is fail-closed-to-
    review (blocked=True, needs_manual_review=True) unless onJudgeError="allow".
    The work is NEVER rejected or closed here — only blocked from auto-merge.

    Optional keyword args ``product_name``, ``product_description``, and
    ``goal_metric`` anchor the judgment to this product's identity. When omitted,
    ``product_name`` and ``product_description`` are auto-loaded from org.json.
    ``goal_metric`` is task-specific; callers who have it (e.g. from vision.md via
    ``parse_goals_from_vision()``) should pass it explicitly.
    """
    if config is None:
        config = load_gate_config()

    # Auto-load product identity from org.json when caller does not provide it.
    if product_name is None and product_description is None:
        identity = load_product_identity()
        product_name = identity["product_name"]
        product_description = identity["product_description"]
    threshold = float(config.get("confidenceThreshold", 0.7))
    on_error = config.get("onJudgeError", "block")

    def _error_verdict(reason: str, *, sha: str | None = None) -> DeliverableVerdict:
        if on_error == "allow":
            blocked, needs_review = False, False
        else:
            blocked, needs_review = True, True
        v = DeliverableVerdict(
            task_id=task_id,
            addresses_task=None,
            confidence=None,
            reason=reason,
            blocked=blocked,
            needs_manual_review=needs_review,
            error=reason,
            sha=sha,
        )
        _record_decision(v)
        return v

    # Disabled gate => no-op allow (does not block the happy path).
    if not config.get("enabled", True):
        return DeliverableVerdict(
            task_id=task_id,
            addresses_task=None,
            confidence=None,
            reason="deliverable gate disabled",
            blocked=False,
            needs_manual_review=False,
            skipped_reason="disabled",
        )

    sha = get_pr_head_sha(pr_number)

    # Cache hit (keyed by head sha).
    if use_cache and sha:
        cache = _load_cache()
        cached = cache.get(sha)
        if isinstance(cached, dict) and cached.get("task_id") == task_id:
            v = DeliverableVerdict(
                task_id=task_id,
                addresses_task=cached.get("addresses_task"),
                confidence=cached.get("confidence"),
                reason=cached.get("reason", ""),
                blocked=bool(cached.get("blocked")),
                needs_manual_review=bool(cached.get("needs_manual_review")),
                error=cached.get("error"),
                cached=True,
                sha=sha,
            )
            _record_decision(v)
            return v

    ctx = fetch_pr_context(pr_number, int(config.get("maxDiffChars", 60000)))
    if ctx is None:
        return _error_verdict(
            "could not fetch PR diff for deliverable judgment", sha=sha
        )
    meta_json, diff_text = ctx
    if not diff_text.strip():
        # An empty diff cannot address any task — fail-closed to review.
        return _error_verdict("PR has an empty diff — nothing delivered", sha=sha)

    # Tripwire: hard-fail before calling the LLM when the diff introduces
    # framework identity content into a non-framework product repo. Exemption
    # is keyed on git remote URL (not org.json) to prevent bypass by file edits.
    tripwire_msg = _check_identity_tripwire(
        diff_text, product_name, is_framework_repo=_is_framework_repo()
    )
    if tripwire_msg:
        tripwire_verdict = DeliverableVerdict(
            task_id=task_id,
            addresses_task=False,
            confidence=1.0,
            reason=tripwire_msg,
            blocked=True,
            needs_manual_review=True,
            error=tripwire_msg,
            sha=sha,
        )
        if sha:
            cache = _load_cache()
            cache[sha] = {
                "task_id": task_id,
                "addresses_task": False,
                "confidence": 1.0,
                "reason": tripwire_msg,
                "blocked": True,
                "needs_manual_review": True,
                "error": tripwire_msg,
            }
            _save_cache(cache)
        _record_decision(tripwire_verdict)
        maybe_record_antipattern(tripwire_verdict, title)
        return tripwire_verdict

    prompt = build_judge_prompt(
        task_id,
        title,
        description,
        pr_number,
        meta_json,
        diff_text,
        product_name=product_name,
        product_description=product_description,
        goal_metric=goal_metric,
    )
    stdout, err = _run_judge_cli(prompt, config)
    if err and not stdout:
        return _error_verdict(f"judge error: {err}", sha=sha)

    parsed = parse_verdict_json(stdout or "")
    if not parsed:
        return _error_verdict("judge output had no parseable verdict JSON", sha=sha)

    addresses = parsed.get("addresses_task")
    if not isinstance(addresses, bool):
        return _error_verdict("judge verdict missing boolean addresses_task", sha=sha)
    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    reason = str(parsed.get("reason", "")) or "(no reason given)"

    blocked, needs_review = _decide(addresses, confidence, threshold)
    verdict = DeliverableVerdict(
        task_id=task_id,
        addresses_task=addresses,
        confidence=confidence,
        reason=reason,
        blocked=blocked,
        needs_manual_review=needs_review,
        sha=sha,
    )

    # Persist to cache (by sha) + audit jsonl.
    if sha:
        cache = _load_cache()
        cache[sha] = {
            "task_id": task_id,
            "addresses_task": addresses,
            "confidence": confidence,
            "reason": reason,
            "blocked": blocked,
            "needs_manual_review": needs_review,
            "error": None,
        }
        _save_cache(cache)
    _record_decision(verdict)
    # Phase 3 learning loop: a confirmed phantom (addresses_task is False) teaches the
    # generator to stop proposing this class. Errors (None) are never recorded.
    maybe_record_antipattern(verdict, title)
    return verdict


# =============================================================================
# CLI
# =============================================================================


def main() -> None:
    args: dict = {}
    argv = sys.argv[2:] if len(sys.argv) > 1 else []
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:].replace("-", "_")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                args[key] = argv[i + 1]
                i += 2
            else:
                args[key] = True
                i += 1
        else:
            i += 1

    command = sys.argv[1].lower() if len(sys.argv) > 1 else "help"

    if command == "judge":
        pr_number = args.get("pr_number")
        task_id = args.get("task_id", "unknown")
        if not pr_number:
            print("Error: --pr-number required")
            sys.exit(1)
        verdict = judge_pr_deliverable(
            task_id=task_id,
            title=args.get("title", ""),
            description=args.get("description", ""),
            pr_number=pr_number,
            branch=args.get("branch"),
            use_cache=not args.get("no_cache", False),
        )
        print(json.dumps(asdict(verdict), indent=2))
        # Exit 0 always — the verdict is data, not a process failure.
        sys.exit(0)
    else:
        print(__doc__)
        sys.exit(0)


if __name__ == "__main__":
    main()
