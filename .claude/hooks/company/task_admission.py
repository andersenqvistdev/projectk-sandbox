"""Task-admission gate (Phase 1, autonomy plan).

Pure, unit-testable admission logic for *self/auto-generated* tasks. Rejects
redundant, invalid, or already-done work BEFORE it enters the work queue.

Motivation (see ``.planning/autonomy/phase-1-task-admission.md``): the
proactive/initiative/ideation engine had no admission gate, so it produced the
origins of nearly every phantom — duplicate-title artifacts
(``"Document: Document: Document:"``), tests for already-shipped features,
wrong-target tests, and references to symbols that do not exist
(``build_panel_data`` — the real symbol is ``build_demo_data``).

Design rules:
  * ``admit_task`` is a PURE function (no side effects, no logging) so it is
    unit-testable without the daemon. The wiring layer in ``work_allocator``
    does the logging + shadow-mode handling.
  * ``human``-sourced tasks ALWAYS bypass the gate — never block a human.
  * Fail CLOSED (reject) only when a check is *definitive*; fail OPEN (admit)
    when a check is merely uncertain. A too-aggressive gate that drops real
    work is worse than a few phantoms.

Public API:
    admit_task(task, queue=None, repo_root=None, config=None) -> (bool, reason|None)
    load_admission_config(repo_root) -> dict
    log_rejection(repo_root, task, reason, *, shadow=False) -> None
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Sources that are subject to the gate. Everything else (notably ``human``, but
# also ``escalation``/``feedback_monitor``/``cron`` recovery lanes) bypasses —
# we only police machine-proposed work. Unknown sources fail OPEN (bypass).
GATED_SOURCES = frozenset(
    {
        "self",
        "planning",
        "roadmap",
        "workshop",
        "employee_initiative",
        "employee_ideation",
        "gap_analysis",
    }
)


def _is_gated_source(source: str | None) -> bool:
    """Whether a task source is subject to the gate.

    Matches both bare sources (``"self"``, ``"planning"``) and project-suffixed
    variants seen in the real queue (``"roadmap-P85"``, ``"workshop-ws124"``).
    Human and recovery lanes (``human``/``escalation``/``auto_merge_escalation``/
    ``feedback_monitor``/``cron``) bypass — they fail OPEN.
    """
    s = (source or "self").lower()
    return any(s == p or s.startswith(p + "-") for p in GATED_SOURCES)


DEFAULT_CONFIG = {
    "enabled": True,
    # Shadow mode is enforced by the wiring layer, not here; surfaced in the
    # config so the wiring can read it from the same place.
    "shadowMode": False,
    "checkDuplicateTitle": True,
    "checkTargetsExist": True,
    "checkNotAlreadyDone": True,
    "valueFloor": True,
    # Phase 3: consult learned anti-patterns (confirmed-phantom classes) so the gate
    # stops re-admitting work the system has already shipped-but-didn't-deliver.
    "checkAntiPatterns": True,
    # Concrete-deliverable gate: GATED-source tasks that use abstract improvement
    # verbs ("Improve X", "Enhance X") or are short abstract noun phrases
    # ("Developer Experience", "Test Automation") must name a concrete target
    # (a file path, code symbol, or named binary) to be admitted.
    "checkConcreteDeliverable": True,
    # PB-2 semantic relevance gate: the deterministic checks above catch malformed
    # and duplicate work but are blind to "novel, plausible, and pointless" — the
    # process-navel-gazing an idle company generates about itself. This LLM-judge
    # asks whether a gated task concretely advances an ACTIVE goal from vision.md,
    # and rejects it if not. It runs LAST (after every cheap check passes) so the
    # model is invoked rarely, and DEGRADES to admit on any error / no-goals / no
    # claude CLI — a judge outage must never hard-block the whole company.
    "checkSemanticRelevance": True,
    "semanticTimeoutSeconds": 60,
    # Model alias/id for `claude --model`; null => omit the flag (CLI default).
    "semanticModel": None,
}

# Directories never worth scanning for symbol/file validity checks.
_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".forge-worktrees",
        "htmlcov",
    }
)

# A leading ``"<prefix>: "`` segment repeated two or more times, e.g.
# ``"Document: Document: Document: [WS-124-002]"``.
_DUP_PREFIX_RE = re.compile(r"^\s*(.{2,60}?:\s+)(?:\1)+", re.IGNORECASE)

# Task operates on *existing* code (so its named targets ought to exist).
_OPERATE_ON_EXISTING_RE = re.compile(
    r"\b(test|tests|testing|document|docs|docstring|fix|refactor|cover|coverage)\b",
    re.IGNORECASE,
)

# Test-coverage intent ("add tests for X", "improve test coverage for X").
_TEST_INTENT_RE = re.compile(
    r"\b(add|write|improve|increase|expand)\b.{0,40}\b(test|tests|coverage)\b",
    re.IGNORECASE,
)

# Incremental-coverage intent — legitimate work even when a test file already
# exists ("backfill", "raise coverage from 50%", "missing/remaining tests").
# These must NOT be rejected as "already has tests" (that would over-block).
_INCREMENTAL_INTENT_RE = re.compile(
    r"\b(backfill|increase|raise|additional|more|missing|remaining|uncovered|"
    r"edge[\s-]?cases?)\b|coverage gap|currently at|\b\d{1,3}(\.\d+)?\s*%",
    re.IGNORECASE,
)

# Trivially-scoped work (value floor).
# "line"/"word" are only matched when paired with an explicit size marker
# ("one" or "single") — bare "a" collides with ordinary usage
# ("add a timestamped line", "add a config line").
# "docstring"/"typo"/"comment" remain trivial with any single-item quantifier.
_TRIVIAL_RE = re.compile(
    r"\b(expand|add|fix|update|tweak)\b.{0,30}\b(one|a|single)\b.{0,25}\b(docstring|typo|comment)\b"
    r"|\b(expand|add|fix|update|tweak)\b.{0,30}\b(one|single)\b.{0,25}\b(line|word)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Concrete-deliverable gate (closes the "novel, plausible, pointless" gap)
# --------------------------------------------------------------------------- #

# Abstract improvement verbs — "Improve X", "Enhance X", "Optimize X", etc.
# When a task uses one of these and names no concrete target, it is a broad
# process idea, not an actionable engineering task.
_ABSTRACT_IMPROVEMENT_VERB_RE = re.compile(
    r"^\s*(improve|enhance|optimize|boost|strengthen|advance|refine|"
    r"elevate|augment|uplift|streamline|modernize|overhaul|expand)\b",
    re.IGNORECASE,
)

# Abstract domain nouns — when a short all-TitleCase phrase (2–4 words) ends
# up containing one of these, it is an OKR/initiative concept, not a task.
_ABSTRACT_DOMAIN_NOUNS = frozenset(
    {
        "automation",
        "experience",
        "quality",
        "velocity",
        "efficiency",
        "productivity",
        "excellence",
        "maturity",
        "culture",
        "governance",
        "adoption",
        "cohesion",
    }
)

# A file path that carries a known source/config/doc extension.
_CONCRETE_FILE_RE = re.compile(
    r"\b[\w][\w./-]*\.(?:py|md|json|yaml|yml|toml|sh|txt|js|ts|html|css|sql)\b",
    re.IGNORECASE,
)

# A bin/ command path.
_CONCRETE_BIN_RE = re.compile(r"\bbin/[a-z][a-z0-9_-]+")

# ``identifier()`` call tokens (snake_case / camelCase function references).
_PARENS_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\(\)")
# Backtick-wrapped identifiers (no dots — those are paths, handled separately).
_BACKTICK_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})`")

# --------------------------------------------------------------------------- #
# Negation-context detection for backtick symbols
#
# "no `mcpServers` config found" or "evaluate whether `X` should be added"
# must NOT be treated as a required-to-exist target: the text explicitly notes
# the identifier is absent or under evaluation.  The check runs per-occurrence
# (skipped symbols are NOT added to `seen`), so a symbol that appears once in
# negation context and once without is still validated on the second occurrence.
#
# Intentional omissions:
#   "without" — "implement Y without `X`" is ambiguous (X may exist); omitted
#   to avoid false negatives on real targets.  The fail-open gate posture means
#   a genuine missing-target task that slips through the symbol check is still
#   caught by _extract_py_paths or the semantic-relevance judge.
# --------------------------------------------------------------------------- #

# Characters scanned before/after a backtick match to detect absence context.
_NEGATION_CTX_WINDOW = 80

# Absence language immediately before the opening backtick:
#   "no `X`", "missing `X`", "absent `X`", "zero `X`"
#   "evaluate whether `X`", "assess whether to add `X`"
# Uses DOTALL so the window can contain newlines in multi-line descriptions.
_NEGATION_PRE_RE = re.compile(
    r"\b(?:no|zero|missing|absent)\s*$"
    r"|\b(?:evaluate|assess)\b.{0,60}?\bwhether\b[^`]*$",
    re.IGNORECASE | re.DOTALL,
)

# Absence language immediately after the closing backtick:
#   "`X` not found", "`X` does not exist", "`X` is not yet …"
_NEGATION_POST_RE = re.compile(
    r"^\s*(?:not\s+found|does\s+not\s+exist|is\s+not\b|not\s+yet\b)",
    re.IGNORECASE,
)
# Python file paths.
_PY_PATH_RE = re.compile(r"\b([\w./-]+\.py)\b")
# "for X" / "for: X" target capture (test-coverage targets).
_FOR_TARGET_RE = re.compile(r"\bfor[:\s]+`?([A-Za-z_][\w.]+)`?", re.IGNORECASE)
# A plausible lowercase Python module name (snake_case or single lowercase word).
_MODULE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{3,}")

# Verbs that indicate a .py path is a greenfield DELIVERABLE, not a missing target.
# "create scripts/foo.py", "scaffold new_module.py", "generate parser.py" → deliverable.
# Bare "add" is excluded: "add tests for scripts/foo.py" names an existing target.
_DELIVERABLE_CREATION_VERB_RE = re.compile(
    r"\b(create|scaffold|generate)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Repo searcher (cached, pure-Python; no dependency on ripgrep being present)
# --------------------------------------------------------------------------- #


class RepoSearcher:
    """Scans a repo's ``.py`` files once and answers symbol/file existence.

    Cheap enough for an occasional admission check; the concatenated text and
    basename set are built lazily on first use and reused for the life of the
    instance.
    """

    def __init__(self, repo_root: Path | str):
        self.repo_root = Path(repo_root)
        self._py_files: list[Path] | None = None
        self._all_text: str | None = None
        self._basenames: set[str] | None = None

    @property
    def py_files(self) -> list[Path]:
        if self._py_files is None:
            files: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(self.repo_root):
                dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
                for fn in filenames:
                    if fn.endswith(".py"):
                        files.append(Path(dirpath) / fn)
            self._py_files = files
        return self._py_files

    @property
    def all_py_text(self) -> str:
        if self._all_text is None:
            parts: list[str] = []
            for f in self.py_files:
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
            self._all_text = "\n".join(parts)
        return self._all_text

    @property
    def basenames(self) -> set[str]:
        if self._basenames is None:
            self._basenames = {f.name for f in self.py_files}
        return self._basenames

    def has_symbol(self, symbol: str) -> bool:
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        return bool(pattern.search(self.all_py_text))

    def has_file(self, basename: str) -> bool:
        return basename in self.basenames

    def test_exists_for(self, name: str) -> bool:
        """True if a dedicated test file under ``tests/`` already targets ``name``.

        Strict on purpose: requires a file whose stem is exactly
        ``test_<name>`` (case-insensitive) AND whose content references ``name``
        as a whole word. Earlier substring matching over-blocked — e.g. the
        candidate ``Document`` matched ``test_document_approvals.py`` and
        ``Write`` matched ``test_task_result_writer.py``.
        """
        tests_dir = self.repo_root / "tests"
        if not tests_dir.is_dir():
            return False
        target_stem = f"test_{name.lower()}"
        word = re.compile(r"\b" + re.escape(name) + r"\b")
        for f in tests_dir.rglob("test_*.py"):
            if f.stem.lower() == target_stem:
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if word.search(content):
                    return True
        return False


_SEARCHER_CACHE: dict[str, tuple[RepoSearcher, float]] = {}
_SEARCHER_TTL = 30.0  # seconds — short enough to pick up newly-added files


def _get_searcher(repo_root: Path | str) -> RepoSearcher:
    key = str(repo_root)
    now = time.monotonic()
    cached = _SEARCHER_CACHE.get(key)
    if cached is not None and (now - cached[1]) < _SEARCHER_TTL:
        return cached[0]
    searcher = RepoSearcher(repo_root)
    _SEARCHER_CACHE[key] = (searcher, now)
    return searcher


# --------------------------------------------------------------------------- #
# Individual checks (each returns a reject reason string, or None to admit)
# --------------------------------------------------------------------------- #


def _check_duplicate_title_artifact(title: str) -> str | None:
    match = _DUP_PREFIX_RE.match(title or "")
    if match:
        segment = match.group(1).strip()
        return f"duplicate-title artifact: repeated prefix {segment!r}"
    return None


def _check_value_floor(task: dict) -> str | None:
    text = f"{task.get('title', '')}\n{task.get('description', '')}"
    if _TRIVIAL_RE.search(text):
        return "trivial scope: below value floor"
    return None


def _has_concrete_target(text: str) -> bool:
    r"""True if *text* (title + description) names a concrete deliverable.

    A concrete target is any of: a file with a known extension
    (``scripts/chlog.py``), a code symbol with parentheses
    (``operation_loop()``), a backtick-wrapped identifier (``\`widget\``), or
    a ``bin/`` command path.  The check is intentionally broad so that an
    abstract-framed title like "Enhance performance" is admitted when the
    description says "Profile ``build_demo_data()`` in ``control_panel.py``".
    """
    return bool(
        _CONCRETE_FILE_RE.search(text)
        or _PARENS_SYMBOL_RE.search(text)
        or _BACKTICK_SYMBOL_RE.search(text)
        or _CONCRETE_BIN_RE.search(text)
    )


def _check_concrete_deliverable(title: str, description: str) -> str | None:
    """Reject abstract 'improve X' tasks that name no concrete deliverable.

    Closes the "novel, plausible, and pointless" admission-gate gap: the
    ideation engine produces broad process-improvement ideas that are novel
    (don't match anti-patterns), name no existing target (don't trigger the
    nonexistent-target check), and are not trivially scoped (don't hit the
    value floor).  Two patterns are caught:

    * **Rule A** — abstract improvement verb with no concrete target:
      ``"Improve Developer Experience"``, ``"Optimize Build Performance"``.
    * **Rule B** — short all-TitleCase noun phrase containing an abstract
      domain concept: ``"Developer Experience"``, ``"Test Automation"``.

    A concrete target anywhere in title or description — a file path, a code
    symbol, or a ``bin/`` path — exempts the task from both rules. Human-sourced
    tasks bypass this check entirely via ``admit_task``.
    """
    text = f"{title}\n{description}"
    if _has_concrete_target(text):
        return None

    _REJECT = (
        "abstract task: names no concrete deliverable"
        " (specify a file path, function, or named feature)"
    )

    # Rule A: abstract improvement verb → must name a concrete target.
    if _ABSTRACT_IMPROVEMENT_VERB_RE.match(title.strip()):
        return _REJECT

    # Rule B: short all-TitleCase noun phrase with an abstract domain concept.
    words = title.strip().split()
    if 1 < len(words) <= 4 and all(w and w[0].isupper() for w in words):
        lower_words = {w.lower() for w in words}
        if lower_words & _ABSTRACT_DOMAIN_NOUNS:
            return _REJECT

    return None


def _is_code_identifier(symbol: str) -> bool:
    """Filter out plain English words; keep things that look like code symbols."""
    if "_" in symbol:
        return True
    if re.search(r"[a-z][A-Z]", symbol):  # camelCase
        return True
    return False


def _symbol_in_negation_context(match: re.Match, text: str) -> bool:
    """True when a backtick symbol match is declared absent by surrounding text.

    Checks a short window before and after the match for absence/negation
    language so identifiers like `mcpServers` in "no `mcpServers` config found"
    are not treated as required-to-exist targets.

    Only called for _BACKTICK_SYMBOL_RE matches, not for parenthesised calls.
    """
    pre = text[max(0, match.start() - _NEGATION_CTX_WINDOW) : match.start()]
    post = text[match.end() : match.end() + _NEGATION_CTX_WINDOW]
    return bool(_NEGATION_PRE_RE.search(pre) or _NEGATION_POST_RE.match(post))


def _extract_symbols(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for regex in (_PARENS_SYMBOL_RE, _BACKTICK_SYMBOL_RE):
        for match in regex.finditer(text):
            sym = match.group(1)
            if sym in seen:
                continue
            if _is_code_identifier(sym):
                # Backtick identifiers in negation context (e.g. "no `mcpServers`
                # found") are skipped — but NOT added to `seen`, so a later
                # non-negated occurrence of the same symbol is still validated.
                if regex is _BACKTICK_SYMBOL_RE and _symbol_in_negation_context(
                    match, text
                ):
                    continue
                seen.add(sym)
                found.append(sym)
    return found


def _extract_py_paths(text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in _PY_PATH_RE.finditer(text):
        path = match.group(1)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _has_creation_verb_near_path(path: str, text: str) -> bool:
    """True when a creation verb appears within ±150 chars of *path* in *text*.

    Tries the full path and the basename so both "create scripts/foo.py" and
    "create foo.py" match.  Fails silently (returns False) on any error.
    """
    targets = {path, path.rsplit("/", 1)[-1]}
    for target in targets:
        pat = re.escape(target)
        for m in re.finditer(pat, text, re.IGNORECASE):
            window = text[max(0, m.start() - 150) : m.end() + 150]
            if _DELIVERABLE_CREATION_VERB_RE.search(window):
                return True
    return False


def _is_goal_deliverable(path: str, repo_root: Path | str | None) -> bool:
    """True when *path*'s basename is named in any active goal's description or metric.

    Reuses ``_load_active_goals`` (fail-open on error).  Matches by basename so
    ``scripts/snipstash.py`` matches a goal that mentions ``snipstash.py``.
    """
    try:
        goals = _load_active_goals(repo_root)
        if not goals:
            return False
        basename = path.rsplit("/", 1)[-1]
        for goal in goals:
            combined = (
                f"{getattr(goal, 'description', '')} "
                f"{getattr(goal, 'success_metric', '')}"
            )
            if basename in combined:
                return True
    except Exception:
        pass
    return False


def _check_targets_exist(
    title: str,
    description: str,
    searcher: RepoSearcher,
    repo_root: Path | str | None = None,
) -> str | None:
    text = f"{title}\n{description}"
    # Only validate targets for tasks that operate on existing code. An
    # "implement new X" task legitimately names things that do not exist yet.
    if not _OPERATE_ON_EXISTING_RE.search(text):
        return None

    for symbol in _extract_symbols(text):
        if not searcher.has_symbol(symbol):
            return f"target not found: {symbol}"

    for path in _extract_py_paths(text):
        basename = path.rsplit("/", 1)[-1]
        # Test deliverable files may legitimately not exist yet (they are the
        # thing being created); only validate non-test source paths.
        if path.startswith("tests/") or basename.startswith("test_"):
            continue
        if not searcher.has_file(basename):
            # Greenfield deliverable: a creation verb adjacent to the path, or the
            # path is named as a deliverable in an active goal's description/metric.
            # Both signals treat the missing file as expected output, not a typo.
            if _has_creation_verb_near_path(path, text):
                continue
            if _is_goal_deliverable(path, repo_root):
                continue
            return f"target file not found: {path}"
    return None


def _candidate_test_targets(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for symbol in _extract_symbols(text):
        if symbol not in seen:
            seen.add(symbol)
            names.append(symbol)
    for match in _FOR_TARGET_RE.finditer(text):
        raw = match.group(1)
        name = raw.rsplit(".", 1)[0] if raw.endswith(".py") else raw
        name = name.rsplit("/", 1)[-1]
        # Only plausible lowercase module identifiers — drops Title-case English
        # noise ("Document"/"Write"/"Workers") and tracker tokens ("WS-124")
        # pulled out of malformed artifact titles.
        if name not in seen and _MODULE_NAME_RE.fullmatch(name):
            seen.add(name)
            names.append(name)
    return names


def _check_not_already_done(
    title: str, description: str, searcher: RepoSearcher
) -> str | None:
    text = f"{title}\n{description}"
    if _TEST_INTENT_RE.search(text):
        # Incremental-coverage work (backfill / raise-from-N% / missing tests)
        # is legitimate even when a test file already exists — don't block it.
        if _INCREMENTAL_INTENT_RE.search(text):
            return None
        for name in _candidate_test_targets(text):
            if searcher.test_exists_for(name):
                return f"already has tests: tests/test_{name}.py"
    return None


# --------------------------------------------------------------------------- #
# Config + logging
# --------------------------------------------------------------------------- #


def _default_repo_root() -> Path:
    """Best-effort repo-root resolution (``<repo>/.company`` -> ``<repo>``)."""
    try:
        try:
            from . import company_resolver as cr  # type: ignore[attr-defined]
        except ImportError:
            import company_resolver as cr  # type: ignore[no-redef]
        return cr.get_company_dir().parent
    except Exception:
        return Path.cwd()


def load_admission_config(repo_root: Path | str | None) -> dict:
    """Load ``taskAdmission`` config from the canonical root ``forge-config.json``.

    Root config is canonical since #1052. Unknown keys are ignored; missing
    keys fall back to ``DEFAULT_CONFIG``.
    """
    config = dict(DEFAULT_CONFIG)
    if repo_root is None:
        repo_root = _default_repo_root()
    cfg_path = Path(repo_root) / "forge-config.json"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        section = raw.get("taskAdmission")
        if isinstance(section, dict):
            config.update({k: v for k, v in section.items() if k in DEFAULT_CONFIG})
    except (OSError, json.JSONDecodeError):
        pass
    return config


def log_rejection(
    repo_root: Path | str,
    task: dict,
    reason: str,
    *,
    shadow: bool = False,
) -> None:
    """Append a rejection record so Phase 3 can learn from it. Never raises."""
    try:
        path = (
            Path(repo_root) / ".company" / "state" / "task_admission_rejections.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "source": task.get("source"),
            "reason": reason,
            "shadow": shadow,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _load_antipatterns(repo_root: Path | str | None) -> list[dict]:
    """Best-effort load of the learned anti-pattern store (empty on any error)."""
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        return la.load_antipatterns(repo_root)
    except Exception:
        return []


def _check_anti_patterns(task: dict, antipatterns: list[dict]) -> str | None:
    """Reject if the task matches a learned anti-pattern (confirmed phantom class)."""
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        return la.match_task(task, antipatterns)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# PB-2 — semantic relevance gate (the missing upstream gate)
#
# The deterministic checks above are blind to "novel, plausible, and pointless":
# a gated task like "Developer Experience" or "Streamline the workflow" names no
# concrete target, matches no anti-pattern, and is not a duplicate — so it sails
# through, which is exactly how an idle company's ideation navel-gazes its way
# into phantom work. This gate asks an LLM judge whether the task concretely
# advances an ACTIVE product goal (from .company/vision.md). It runs LAST, only
# after every cheap check has passed, and it DEGRADES to admit on any failure —
# no goals to judge against, no claude CLI, timeout, bad JSON — because a judge
# outage must never hard-block the whole company. It only ever *rejects* on a
# confident, parseable "not relevant" verdict.
# --------------------------------------------------------------------------- #

# Cache verdicts within a process run, keyed by (task, active-goals) signature,
# so re-admission attempts and shadow/real double-calls don't re-invoke the model.
_SEMANTIC_CACHE: dict[str, str | None] = {}


def _claude_prefix() -> list[str]:
    """Canonical Claude CLI invocation prefix, or [] if unavailable.

    Mirrors deliverable_judge._claude_cmd_prefix; kept local so this module has no
    heavy import dependency on the judge.
    """
    if shutil.which("uv"):
        return ["uv", "run", "claude"]
    if shutil.which("claude"):
        return ["claude"]
    return []


def _load_active_goals(repo_root: Path | str | None) -> list:
    """Active goals parsed from .company/vision.md, or [] (fail-open).

    Reuses goal_tracker's vision parser. Returns [] when the file is absent, has
    no parseable goal table, or on any error — the caller treats [] as
    'nothing to judge against' and admits.
    """
    try:
        root = Path(repo_root) if repo_root is not None else _default_repo_root()
        vision = root / ".company" / "vision.md"
        if not vision.exists():
            return []
        try:
            from . import goal_tracker as gt  # type: ignore[attr-defined]
        except ImportError:
            import goal_tracker as gt  # type: ignore[no-redef]
        default_goals = getattr(gt, "DEFAULT_GOALS", object())
        goals = gt.parse_goals_from_vision(vision, period="active")
        if goals is default_goals:
            goals = gt.parse_goals_from_vision(vision, period="all")
        if not goals or goals is default_goals:
            return []
        return list(goals)
    except Exception:
        return []


def _relevance_prompt(title: str, description: str, goals: list) -> str:
    lines = []
    for g in goals:
        gid = getattr(g, "id", "?")
        name = getattr(g, "name", "")
        desc = getattr(g, "description", "")
        metric = getattr(g, "success_metric", "")
        lines.append(f"- {gid}: {name} — {desc} (success metric: {metric})")
    goal_block = "\n".join(lines)
    return (
        "You are the admission gate for an autonomous software company. Decide "
        "whether a PROPOSED TASK does concrete work toward one of the company's "
        "ACTIVE product goals. Building, testing, or documenting the product "
        "counts. Vague process/organisational improvement that does not advance a "
        "listed goal does NOT count.\n\n"
        f"ACTIVE GOALS:\n{goal_block}\n\n"
        f"PROPOSED TASK:\nTitle: {title}\nDescription: {description}\n\n"
        "Reply with ONE JSON object and nothing else: "
        '{"relevant": true|false, "reason": "<one short sentence>"}'
    )


def _check_semantic_relevance(
    title: str, description: str, repo_root: Path | str | None, config: dict
) -> str | None:
    """Reject a gated task the LLM judges irrelevant to active goals; else None.

    Degrades to None (admit) on every failure path.
    """
    goals = _load_active_goals(repo_root)
    if not goals:
        return None  # nothing to judge against — don't block

    sig = hashlib.sha1(
        (
            title
            + "\x1f"
            + description
            + "\x1f"
            + "|".join(str(getattr(g, "id", "")) for g in goals)
        ).encode("utf-8")
    ).hexdigest()
    if sig in _SEMANTIC_CACHE:
        return _SEMANTIC_CACHE[sig]

    prefix = _claude_prefix()
    if not prefix:
        _SEMANTIC_CACHE[sig] = None  # no CLI — degrade to admit
        return None

    cmd = list(prefix)
    model = config.get("semanticModel")
    if model:
        cmd.extend(["--model", str(model)])
    cmd.extend(["-p", _relevance_prompt(title, description, goals)])

    verdict: str | None = None
    # 2026-07-06 fork-bomb guard: the semantic gate launches a real claude process.
    assert_spawn_allowed("task_admission._check_semantic_relevance", subprocess.run)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=int(config.get("semanticTimeoutSeconds", 60)),
        )
        if result.returncode == 0 and result.stdout:
            out = result.stdout.strip()
            m = re.search(r"\{.*\}", out, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                if data.get("relevant") is False:
                    why = str(data.get("reason", "")).strip()
                    verdict = "not relevant to any active product goal" + (
                        f": {why}" if why else ""
                    )
    except (
        subprocess.TimeoutExpired,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ):
        verdict = None  # any failure degrades to admit

    _SEMANTIC_CACHE[sig] = verdict
    return verdict


def admit_task(
    task: dict,
    queue: dict | None = None,
    repo_root: Path | str | None = None,
    config: dict | None = None,
    antipatterns: list[dict] | None = None,
) -> tuple[bool, str | None]:
    """Decide whether a task may enter the queue.

    Returns ``(True, None)`` to admit, or ``(False, reason)`` to reject. Pure with
    respect to side effects: no logging, no queue writes, no shadow-mode handling
    (the wiring layer owns those). ``human``-sourced tasks always admit.

    ``antipatterns`` may be passed for testability; if None and the anti-pattern
    check is enabled it is loaded lazily from ``repo_root`` (fail-open on error).
    """
    if not _is_gated_source(task.get("source")):
        return True, None

    if config is None:
        config = load_admission_config(repo_root)
    if not config.get("enabled", True):
        return True, None

    title = task.get("title") or ""
    description = task.get("description") or ""

    # Cheap, IO-free checks first.
    if config.get("checkDuplicateTitle", True):
        reason = _check_duplicate_title_artifact(title)
        if reason:
            return False, reason

    if config.get("valueFloor", True):
        reason = _check_value_floor(task)
        if reason:
            return False, reason

    if config.get("checkConcreteDeliverable", True):
        reason = _check_concrete_deliverable(title, description)
        if reason:
            return False, reason

    # Learned anti-patterns (Phase 3) — cheap signature compare. Fail OPEN on error.
    if config.get("checkAntiPatterns", True):
        if antipatterns is None:
            antipatterns = _load_antipatterns(repo_root)
        if antipatterns:
            reason = _check_anti_patterns(task, antipatterns)
            if reason:
                return False, reason

    # IO checks — fail OPEN on any error (a flaky scan must never block work).
    if config.get("checkNotAlreadyDone", True) or config.get("checkTargetsExist", True):
        if repo_root is None:
            repo_root = _default_repo_root()
        searcher = _get_searcher(repo_root)

        if config.get("checkNotAlreadyDone", True):
            try:
                reason = _check_not_already_done(title, description, searcher)
                if reason:
                    return False, reason
            except Exception:
                pass

        if config.get("checkTargetsExist", True):
            try:
                reason = _check_targets_exist(title, description, searcher, repo_root)
                if reason:
                    return False, reason
            except Exception:
                pass

    # PB-2 semantic relevance — runs LAST (most expensive; an LLM call). Only
    # reached when every cheap/IO check has passed. Degrades to admit on any
    # failure, so a judge outage never hard-blocks the company.
    if config.get("checkSemanticRelevance", True):
        try:
            reason = _check_semantic_relevance(title, description, repo_root, config)
            if reason:
                return False, reason
        except Exception:
            pass

    return True, None
