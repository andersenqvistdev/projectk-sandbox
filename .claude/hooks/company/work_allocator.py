#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Pull-Based Work Allocator — queue-based task assignment for company agents.

Implements a pull-based work allocation system where agents request work
rather than being assigned work. Uses file-based locking for safe
concurrent access.

Work Queue Schema (work_queue.json):
- pending: Tasks ready for assignment
- in_progress: Tasks currently being worked on
- blocked: Tasks waiting on dependencies
- completed: Finished tasks (for audit trail)

Task Schema:
{
    "task_id": "unique-task-id",
    "priority": 1-4 (1=Critical, 2=High, 3=Normal, 4=Low),
    "required_capabilities": ["capability1", "capability2"],
    "department": "department_id",
    "project_id": "project-identifier or null",
    "created_at": "ISO timestamp",
    "deadline": "ISO timestamp or null",
    "estimated_complexity": "trivial|standard|complex|epic",
    "dependencies_satisfied": true/false,
    "dependencies": ["task-id-1", "task-id-2"],
    "title": "Task title",
    "description": "Task description",
    "source": "human|self|escalation|planning",
    "assigned_to": "agent_id or null",
    "assigned_at": "ISO timestamp or null",
    "created_by": "employee_id or null"
}

Source Types:
    human = Human-directed work (explicit user request)
    self = Self-generated work (agent autonomy)
    escalation = Escalated from another agent
    planning = Generated during planning phase

Priority Levels:
    1 = Critical (system down, security issues)
    2 = High (urgent features, major bugs)
    3 = Normal (standard development work)
    4 = Low (nice-to-have, refactoring)

Usage:
    # Add a new task to the queue
    python work_allocator.py add --title "Task" --priority 2 --department eng

    # Agent pulls next available task
    python work_allocator.py pull --agent-id "agent-001" --capabilities "code,review"

    # Update task status
    python work_allocator.py update --task-id "task-123" --status completed

    # List queue status
    python work_allocator.py list [--status pending|in_progress|blocked]

    # Check agent workload
    python work_allocator.py workload --agent-id "agent-001"
"""

import errno
import fcntl
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Import company_resolver from the same directory
# Lazy imports to handle both package and direct execution
company_resolver = None
efficiency_tracker = None
project_orchestrator = None
snipstash = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global company_resolver, efficiency_tracker, project_orchestrator
    if company_resolver is not None:
        return  # Already imported

    try:
        from . import company_resolver as cr
        from . import efficiency_tracker as et
        from . import project_orchestrator as po

        company_resolver = cr
        efficiency_tracker = et
        project_orchestrator = po
    except ImportError:
        # Direct script execution - import from same directory
        import company_resolver as cr  # type: ignore[no-redef]
        import efficiency_tracker as et  # type: ignore[no-redef]
        import project_orchestrator as po  # type: ignore[no-redef]

        company_resolver = cr
        efficiency_tracker = et
        project_orchestrator = po


def _import_snipstash():
    """Lazily import snipstash artifact detection module from scripts/."""
    global snipstash
    if snipstash is not None:
        return snipstash
    try:
        # Try to import snipstash from scripts directory
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        scripts_path = repo_root / "scripts"
        if scripts_path.exists() and str(scripts_path) not in sys.path:
            sys.path.insert(0, str(scripts_path))
        import snipstash as ss  # type: ignore[import]

        snipstash = ss
    except (ImportError, ModuleNotFoundError):
        # snipstash not available; collision detection disabled
        snipstash = None
    return snipstash


# Configuration
QUEUE_FILE = "state/work_queue.json"
LOCK_FILE = "runtime/queue.lock"
MAX_CONCURRENT_TASKS = 2  # Max tasks per agent
LOCK_TIMEOUT = 10  # Seconds to wait for lock

# WS-119 1.6: Engine self-protection.
# Tasks that touch any of these files modify the daemon itself and must NEVER
# be auto-claimed by the daemon ("don't let the engine fix the engine").
# A task is considered engine-modifying if any of these substrings appears in
# its title, description, or required_capabilities. The daemon will skip these
# tasks during pull_next_task() and require human handling instead.
ENGINE_PROTECTED_FILES = frozenset(
    [
        "forge_daemon.py",
        "employee_activator.py",
        "failure_recovery.py",
        "operation_loop.py",
        "work_allocator.py",
        "team_executor.py",
        "approval_learner.py",
        "strategic_planner.py",
        ".claude/hooks/company/launchd/com.forgelabs.daemon.plist",
        "forge-config.json",
    ]
)


def is_engine_modifying_task(task: dict) -> bool:
    """WS-119 1.6: Detect tasks that would modify the running daemon's code.

    Returns True if the task title, description, file paths, or any explicit
    `engine_modifying: true` flag indicates the task touches an engine file.
    Such tasks must be handled by a human (not auto-claimed by the daemon)
    because the daemon cannot safely modify its own running code.
    """
    if task.get("engine_modifying") is True:
        return True
    title = (task.get("title") or "").lower()
    description = (task.get("description") or "").lower()
    files_field = task.get("files") or task.get("file") or ""
    if isinstance(files_field, list):
        files_str = " ".join(files_field).lower()
    else:
        files_str = str(files_field).lower()
    haystack = f"{title}\n{description}\n{files_str}"
    for protected in ENGINE_PROTECTED_FILES:
        if protected.lower() in haystack:
            return True
    return False


# Phase 1 (autonomy): task-admission gate. Lazily imported so a gate bug can
# never break module import, and so both package and direct-script modes work.
task_admission = None


def _import_task_admission():
    """Lazily import the task_admission module (None on any failure)."""
    global task_admission
    if task_admission is not None:
        return task_admission
    try:
        try:
            from . import task_admission as ta
        except ImportError:
            import task_admission as ta  # type: ignore[no-redef]
        task_admission = ta
    except Exception:
        return None
    return task_admission


def _run_admission_gate(task: dict) -> tuple[bool, str | None]:
    """Phase 1 admission gate wrapper (logging + shadow-mode).

    Returns (admitted, reason). ALWAYS admits on any internal error (fail-open)
    so a gate bug can never block the queue. Human-sourced tasks bypass inside
    ``admit_task``. In shadow mode the would-reject is logged but the task is
    still admitted.
    """
    try:
        ta = _import_task_admission()
        if ta is None:
            return True, None
        repo_root = get_company_dir().parent
        config = ta.load_admission_config(repo_root)
        admitted, reason = ta.admit_task(task, repo_root=repo_root, config=config)
        if admitted:
            return True, None
        shadow = bool(config.get("shadowMode", False))
        ta.log_rejection(repo_root, task, reason, shadow=shadow)
        if shadow:
            return True, None
        return False, reason
    except Exception:
        return True, None


# WS-105: Auto-decomposition thresholds
MAX_FILES_PER_TASK = 15  # Tasks mentioning more files get split
MAX_ITEMS_PER_TASK = 10  # Tasks with more list items get split
AUTO_DECOMPOSE_ENABLED = True  # Enable/disable auto-decomposition

# WS-018: PR-aware duplicate detection
MERGED_PR_LOOKBACK_DAYS = 14
MERGED_PR_SIMILARITY_THRESHOLD = 0.5


def is_shipped_in_merged_pr(
    title: str,
    lookback_days: int = MERGED_PR_LOOKBACK_DAYS,
) -> dict | None:
    """
    Check if a task has already been shipped via a merged PR.

    WS-018-001: Prevents duplicate task generation for already-completed features.

    Args:
        title: Task title to check against merged PRs
        lookback_days: How many days back to check merged PRs

    Returns:
        Dict with PR info if shipped, None otherwise
    """
    try:
        # Query recent merged PRs using gh CLI
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "50",
                "--json",
                "number,title,mergedAt",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent.parent.parent),
        )

        if result.returncode != 0:
            return None

        prs = json.loads(result.stdout) if result.stdout.strip() else []

        # Normalize title for comparison
        title_lower = title.lower()
        title_tokens = set(re.findall(r"\w+", title_lower))

        for pr in prs:
            pr_title = pr.get("title", "")
            pr_title_lower = pr_title.lower()
            pr_tokens = set(re.findall(r"\w+", pr_title_lower))

            # Calculate token overlap (Jaccard-like)
            if not title_tokens or not pr_tokens:
                continue

            intersection = len(title_tokens & pr_tokens)
            union = len(title_tokens | pr_tokens)
            similarity = intersection / union if union > 0 else 0

            # Also check containment (one contains most of the other)
            containment = max(
                intersection / len(title_tokens) if title_tokens else 0,
                intersection / len(pr_tokens) if pr_tokens else 0,
            )

            # Use higher of Jaccard or containment
            final_similarity = max(similarity, containment)

            if final_similarity >= MERGED_PR_SIMILARITY_THRESHOLD:
                return {
                    "pr_number": pr.get("number"),
                    "pr_title": pr_title,
                    "merged_at": pr.get("mergedAt"),
                    "similarity": round(final_similarity, 2),
                    "match_type": "merged_pr",
                }

        return None

    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        # gh CLI not available or failed - skip PR check
        return None
    except Exception:
        # Any other error - fail open (don't block task creation)
        return None


def get_company_dir() -> Path:
    """Get the company directory path using company_resolver."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_current_project_id() -> str | None:
    """Get the current project ID if in multi-project mode."""
    _ensure_imports()
    project_info = company_resolver.get_current_project()
    if project_info:
        return project_info.get("project_id")
    return None


def get_queue_path() -> Path:
    """Get the work queue file path."""
    return get_company_dir() / QUEUE_FILE


def get_lock_path() -> Path:
    """Get the lock file path."""
    return get_company_dir() / LOCK_FILE


def ensure_company_dir():
    """Ensure .company directory and required subdirectories exist."""
    company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / "state").mkdir(exist_ok=True)
    (company_dir / "runtime").mkdir(exist_ok=True)


def get_empty_queue() -> dict:
    """Return empty queue structure."""
    return {
        "proposed": [],  # P26: Employee-proposed tasks awaiting review
        "pending": [],  # Approved tasks ready for assignment
        "in_progress": [],  # Tasks currently being worked on
        "blocked": [],  # Tasks waiting on dependencies
        "review": [],  # P26: Completed tasks awaiting manager review
        "pr_open": [],  # PR created, awaiting merge confirmation
        "completed": [],  # Merged/closed tasks
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_modified": datetime.now(timezone.utc).isoformat(),
            "version": "1.2",  # Added pr_open lane
        },
    }


# Process-wide serializer for QueueLock acquirers in this Python process.
# macOS BSD fcntl.flock returns EDEADLK (errno 11) when two threads in the
# same process try to flock the same file concurrently — even though our
# polling loop would handle that contention correctly. Acquiring this
# threading lock first means at most one thread per process is ever inside
# the fcntl.flock call, so the kernel never sees the same-process race.
# Cross-process contention is still handled by fcntl.flock as before.
_PROCESS_QUEUE_LOCK = threading.Lock()


class QueueLock:
    """
    Context manager for file-based queue locking.

    Two-tier lock:
      1. A process-wide threading.Lock serializes in-process callers.
         Required because macOS BSD flock raises EDEADLK on concurrent
         same-process acquisition attempts.
      2. fcntl.flock provides cross-process serialization (e.g. between
         the daemon and external CLI tools that touch the queue).
    """

    def __init__(self, lock_path: Path, timeout: int = LOCK_TIMEOUT):
        self.lock_path = lock_path
        self.timeout = timeout
        self.lock_file = None
        self._thread_lock_held = False

    def __enter__(self):
        ensure_company_dir()

        # Tier 1: in-process serialization.
        if not _PROCESS_QUEUE_LOCK.acquire(timeout=self.timeout):
            raise TimeoutError(
                f"Could not acquire in-process queue lock within {self.timeout}s"
            )
        self._thread_lock_held = True

        try:
            # Tier 2: cross-process file lock.
            # Mode "a" (not "w"): macOS APFS returns EDEADLK on open(...,"w")
            # when the file has outstanding fcntl locks, because "w" implies
            # O_TRUNC which conflicts with the kernel's lock-tracking metadata.
            # "a" opens for append without truncating — same fd usability, no
            # truncation. The lock file's contents are irrelevant; we only
            # need a stable fd to hold the fcntl.flock on.
            self.lock_file = open(self.lock_path, "a")
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as e:
                    # EAGAIN/EWOULDBLOCK = "would block, retry".
                    # EDEADLK is a defensive catch — Tier 1 should make this
                    # unreachable, but if a future code path bypasses it we
                    # still want to back off rather than crash.
                    if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK):
                        raise
                    if time.time() - start_time > self.timeout:
                        raise TimeoutError(
                            f"Could not acquire queue lock within {self.timeout}s"
                        )
                    time.sleep(0.1)
        except BaseException:
            # Failed acquisition: release tier 1 + close any opened fd.
            self._cleanup_after_failed_enter()
            raise

    def _cleanup_after_failed_enter(self) -> None:
        """Release partial state when __enter__ raises after acquiring some
        but not all tiers. Without this, a TimeoutError or unexpected OSError
        leaks the open file descriptor and the threading lock."""
        if self.lock_file is not None:
            try:
                self.lock_file.close()
            except Exception:
                pass
            self.lock_file = None
        if self._thread_lock_held:
            try:
                _PROCESS_QUEUE_LOCK.release()
            except RuntimeError:
                pass  # already released
            self._thread_lock_held = False

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.lock_file is not None:
                try:
                    fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass  # best-effort unlock
                try:
                    self.lock_file.close()
                except Exception:
                    pass
                self.lock_file = None
        finally:
            if self._thread_lock_held:
                try:
                    _PROCESS_QUEUE_LOCK.release()
                except RuntimeError:
                    pass
                self._thread_lock_held = False
        return False


def load_queue() -> dict:
    """Load work queue from file."""
    import os as _os

    queue_path = get_queue_path()

    # P80: Prevent pytest from reading production queue.
    _real_company = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    _is_pytest = "PYTEST_CURRENT_TEST" in _os.environ or any(
        "pytest" in str(v) for v in [_os.environ.get("_", "")]
    )
    if _is_pytest:
        try:
            if queue_path.resolve().is_relative_to(_real_company.resolve()):
                return get_empty_queue()
        except (ValueError, OSError):
            pass

    if not queue_path.exists():
        return get_empty_queue()

    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return get_empty_queue()


def save_queue(queue: dict):
    """Save work queue to file with atomic write.

    Uses tempfile + os.replace to prevent race conditions where
    concurrent readers see truncated/empty files.
    """
    import os as _os
    import tempfile

    ensure_company_dir()
    queue_path = get_queue_path()

    # P80: Guard against pytest overwriting production queue.
    # Tests that call add_task() without proper isolation were writing
    # test data to the real .company/state/work_queue.json, wiping
    # all production tasks every daemon cycle that runs pytest.
    _real_company = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    _is_pytest = (
        "pytest" in _os.environ.get("PYTEST_CURRENT_TEST", "")
        or "PYTEST_CURRENT_TEST" in _os.environ
        or any("pytest" in str(v) for v in [_os.environ.get("_", "")])
    )
    if _is_pytest:
        try:
            if queue_path.resolve().is_relative_to(_real_company.resolve()):
                # Silently skip — test should be using a fixture directory
                return
        except (ValueError, OSError):
            pass

    queue["metadata"]["last_modified"] = datetime.now(timezone.utc).isoformat()

    # Atomic write: write to temp file, then rename
    # CI-safety: use a default encoder that converts non-serializable objects
    # (e.g. MagicMock from test environments) to strings instead of crashing.
    class _SafeEncoder(json.JSONEncoder):
        def default(self, o):
            try:
                return super().default(o)
            except TypeError:
                return str(o)

    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix="work_queue_",
        dir=str(queue_path.parent),
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2, cls=_SafeEncoder)
        _os.replace(tmp_path, queue_path)
    except Exception:
        # Clean up temp file on error
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise


def generate_task_id() -> str:
    """Generate unique task ID."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"task-{timestamp}-{short_uuid}"


# ── Capability Validation Guards ────────────────────────────────────────────

# Maximum length for a valid capability string
MAX_CAPABILITY_LENGTH = 40

# Pattern for valid capability names (lowercase, alphanumeric, hyphens)
VALID_CAPABILITY_PATTERN = None  # Lazy compiled below


def _get_capability_pattern():
    """Lazily compile the capability validation regex."""
    global VALID_CAPABILITY_PATTERN
    if VALID_CAPABILITY_PATTERN is None:
        import re

        VALID_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
    return VALID_CAPABILITY_PATTERN


def validate_capability(cap: str) -> tuple[bool, str | None]:
    """
    Validate a single capability string.

    Valid capabilities:
    - Are strings (not other types)
    - Are <= 40 characters
    - Contain no newlines or tabs
    - Match pattern: lowercase letters, numbers, hyphens
    - Start with a letter

    Returns:
        Tuple of (is_valid, cleaned_value_or_error)
    """
    if not isinstance(cap, str):
        return False, f"capability must be string, got {type(cap).__name__}"

    # Reject control characters
    if "\n" in cap or "\t" in cap or "\r" in cap:
        return False, "capability contains control characters"

    # Reject obvious prose (embedded sentences)
    if ". " in cap or " is " in cap or " the " in cap.lower():
        return False, "capability appears to be prose, not a capability name"

    # Reject overly long values
    if len(cap) > MAX_CAPABILITY_LENGTH:
        return False, f"capability too long ({len(cap)} > {MAX_CAPABILITY_LENGTH})"

    # Normalize: strip whitespace and lowercase
    cleaned = cap.strip().lower()

    if not cleaned:
        return False, "capability is empty"

    # Validate pattern
    pattern = _get_capability_pattern()
    if not pattern.match(cleaned):
        return False, f"capability '{cleaned}' contains invalid characters"

    return True, cleaned


def sanitize_capabilities(
    capabilities: list[str] | None,
) -> list[str]:
    """
    Sanitize and validate a list of capabilities.

    - Removes invalid entries
    - Normalizes valid entries (lowercase, strip)
    - Removes duplicates while preserving order

    Args:
        capabilities: List of capability strings (or None)

    Returns:
        List of valid, normalized capability strings
    """
    if not capabilities:
        return []

    seen = set()
    result = []

    for cap in capabilities:
        is_valid, value_or_error = validate_capability(cap)
        if is_valid and value_or_error not in seen:
            seen.add(value_or_error)
            result.append(value_or_error)
        # Silently drop invalid capabilities with a log if needed
        # (could add logging here for debugging)

    return result


# ── P26 Fix: Fuzzy Duplicate Detection ──────────────────────────────────────

# Configuration for duplicate detection
DUPLICATE_SIMILARITY_THRESHOLD = 0.70  # 70% token overlap = duplicate
RECENT_COMPLETION_HOURS = 4.0  # Check completed tasks from last N hours

# ── Semantic Duplicate Detection Configuration ──────────────────────────────
# Default thresholds (can be overridden by forge-config.json)
SEMANTIC_CHECK_THRESHOLD_LOW = 0.50  # Below this: not a duplicate
SEMANTIC_CHECK_THRESHOLD_HIGH = 0.70  # Above this: use token similarity only
SEMANTIC_SIMILARITY_THRESHOLD = 0.80  # Semantic similarity >= this = duplicate
MAX_SEMANTIC_CHECKS_PER_MINUTE = 10  # Rate limit for Claude API calls

# Module-level state for rate limiting
_semantic_check_timestamps: list[float] = []


def load_duplicate_detection_config() -> dict:
    """
    Load duplicate detection configuration from forge-config.json.

    Returns:
        dict with configuration values:
        - semanticCheckEnabled: bool
        - semanticCheckThreshold: float (low end for semantic check)
        - semanticSimilarityThreshold: float (semantic similarity to block)
        - maxSemanticChecksPerMinute: int
    """
    config_paths = [
        Path.cwd() / ".claude" / "forge-config.json",
        Path.cwd() / "forge-config.json",
    ]

    defaults = {
        "semanticCheckEnabled": True,
        "semanticCheckThreshold": SEMANTIC_CHECK_THRESHOLD_LOW,
        "semanticSimilarityThreshold": SEMANTIC_SIMILARITY_THRESHOLD,
        "maxSemanticChecksPerMinute": MAX_SEMANTIC_CHECKS_PER_MINUTE,
        # WS-119: "fuzzy" uses difflib.SequenceMatcher (fast, deterministic).
        # "llm" retains the legacy claude haiku subprocess check (slow, blocking).
        "dedupStrategy": "fuzzy",
    }

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                dedup_config = config.get("duplicateDetection", {})
                return {
                    "semanticCheckEnabled": dedup_config.get(
                        "semanticCheckEnabled", defaults["semanticCheckEnabled"]
                    ),
                    "semanticCheckThreshold": dedup_config.get(
                        "semanticCheckThreshold", defaults["semanticCheckThreshold"]
                    ),
                    "semanticSimilarityThreshold": dedup_config.get(
                        "semanticSimilarityThreshold",
                        defaults["semanticSimilarityThreshold"],
                    ),
                    "maxSemanticChecksPerMinute": dedup_config.get(
                        "maxSemanticChecksPerMinute",
                        defaults["maxSemanticChecksPerMinute"],
                    ),
                    "dedupStrategy": dedup_config.get(
                        "dedupStrategy", defaults["dedupStrategy"]
                    ),
                }
            except (json.JSONDecodeError, OSError):
                pass

    return defaults


def _check_semantic_rate_limit(max_per_minute: int) -> bool:
    """
    Check if we can make another semantic check within rate limits.

    Args:
        max_per_minute: Maximum allowed checks per minute.

    Returns:
        True if within rate limit, False if rate limited.
    """
    global _semantic_check_timestamps

    now = time.time()
    minute_ago = now - 60

    # Remove timestamps older than 1 minute
    _semantic_check_timestamps = [
        ts for ts in _semantic_check_timestamps if ts > minute_ago
    ]

    if len(_semantic_check_timestamps) >= max_per_minute:
        return False

    _semantic_check_timestamps.append(now)
    return True


def check_semantic_similarity(title1: str, title2: str) -> float | None:
    """
    Check semantic similarity between two task titles.

    WS-119: Replaced LLM-based (claude haiku) similarity check with deterministic
    difflib.SequenceMatcher. The previous implementation made a subprocess call
    for every borderline task pair (token_sim 0.50-0.70), blocking the daemon's
    Discover phase for 10+ minutes per cycle when the pending queue had many
    similar-looking titles.

    The replacement uses SequenceMatcher on normalized titles (lowercased,
    punctuation stripped). For a typical pair of task titles this completes in
    microseconds instead of seconds, and the scores correlate well with the
    LLM's judgments for duplicate detection.

    Config flag dedupStrategy (in .forge/duplicate_detection.json) can be set
    to "llm" to restore the previous behavior; default is "fuzzy".

    Args:
        title1: First task title
        title2: Second task title

    Returns:
        Similarity score (0.0-1.0), or None if check disabled.
    """
    import difflib
    import re

    config = load_duplicate_detection_config()

    if not config["semanticCheckEnabled"]:
        return None

    strategy = config.get("dedupStrategy", "fuzzy")

    if strategy == "llm":
        # Opt-in legacy path: keep LLM-based check available for users who
        # explicitly set dedupStrategy=llm. Most callers should use fuzzy.
        return _check_semantic_similarity_llm(title1, title2)

    # Default: fuzzy matching via SequenceMatcher on normalized titles.
    def _normalize(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()

    a = _normalize(title1)
    b = _normalize(title2)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return max(0.0, min(1.0, ratio))


def _check_semantic_similarity_llm(title1: str, title2: str) -> float | None:
    """Legacy LLM-based similarity check via claude haiku subprocess.

    Retained for opt-in use via dedupStrategy=llm config. Not called by default
    after WS-119 — see check_semantic_similarity() which defaults to fuzzy.
    """
    import os

    config = load_duplicate_detection_config()

    if not _check_semantic_rate_limit(config["maxSemanticChecksPerMinute"]):
        return None

    prompt = f'''Compare these two task titles for semantic similarity.
Are they asking for essentially the same work, even if worded differently?

Title 1: "{title1}"
Title 2: "{title2}"

Respond with ONLY a JSON object:
{{"similarity": 0.X, "reasoning": "brief explanation"}}

Where similarity is:
- 0.9-1.0: Same task, different wording
- 0.7-0.9: Very similar intent, minor differences
- 0.5-0.7: Related but distinct tasks
- 0.0-0.5: Different tasks'''

    try:
        child_env = dict(os.environ)
        problematic_prefixes = ("UV_", "VIRTUAL_ENV", "CLAUDECODE", "CLAUDE_CODE_")
        for key in list(child_env.keys()):
            if key.startswith(problematic_prefixes):
                del child_env[key]

        original_path = child_env.get("PATH", "")
        clean_path_parts = [
            p
            for p in original_path.split(":")
            if not p.startswith(os.path.expanduser("~/.cache/uv/environments"))
            and "virtualenv" not in p.lower()
        ]
        child_env["PATH"] = (
            ":".join(clean_path_parts) if clean_path_parts else "/usr/bin:/bin"
        )

        child_env["TERM"] = "xterm-256color"
        child_env["LANG"] = "en_US.UTF-8"
        child_env = {k: v for k, v in child_env.items() if v}

        # 2026-07-06 fork-bomb guard: similarity check launches a real claude.
        assert_spawn_allowed(
            "work_allocator._check_semantic_similarity_llm", subprocess.run
        )

        result = subprocess.run(
            [
                "claude",
                "--model",
                "haiku",
                "--print",
                "--no-input",
                "--setting-sources",
                "user",
                "-p",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=child_env,
        )

        if result.returncode != 0:
            return None

        output = result.stdout.strip()

        if "```json" in output:
            output = output.split("```json")[1].split("```")[0].strip()
        elif "```" in output:
            output = output.split("```")[1].split("```")[0].strip()

        response = json.loads(output)
        similarity = float(response.get("similarity", 0))

        return max(0.0, min(1.0, similarity))

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError):
        return None


def tokenize_title(title: str) -> set[str]:
    """
    Tokenize a title for similarity comparison.

    Normalizes by lowercasing, removing punctuation, and splitting into words.
    Keeps alphanumeric tokens of any length (e.g., "Q1", "V2").
    """
    import re

    # Lowercase and remove non-alphanumeric characters (except spaces)
    normalized = re.sub(r"[^\w\s]", "", title.lower())
    # Split into tokens, keep all meaningful tokens
    # Filter out single-char tokens and common stop words
    stop_words = {"a", "an", "the", "to", "in", "on", "at", "for", "of", "and", "or"}
    tokens = {t for t in normalized.split() if len(t) > 1 and t not in stop_words}
    return tokens


def calculate_token_similarity(title1: str, title2: str) -> float:
    """
    Calculate similarity between two titles using containment + Jaccard hybrid.

    Uses a weighted combination:
    - Containment: How much of the smaller title is in the larger (catches subsets)
    - Jaccard: Overall overlap (catches near-identical)

    This catches duplicates like:
    - "Q1 Retrospective" vs "Conduct Q1 Retrospective" (containment = 1.0)
    - "Fix bug" vs "Fix bug in module" (containment = 1.0)

    Returns a value between 0.0 (no overlap) and 1.0 (identical or subset).
    """
    tokens1 = tokenize_title(title1)
    tokens2 = tokenize_title(title2)

    if not tokens1 or not tokens2:
        # Fall back to exact match if tokenization fails
        return 1.0 if title1.strip().lower() == title2.strip().lower() else 0.0

    intersection = tokens1 & tokens2
    union = tokens1 | tokens2

    # Jaccard similarity (overall overlap)
    jaccard = len(intersection) / len(union) if union else 0.0

    # Containment similarity (how much of smaller is in larger)
    # This catches cases like "X" vs "Do X" or "X Y" vs "X Y Z"
    min_size = min(len(tokens1), len(tokens2))
    containment = len(intersection) / min_size if min_size > 0 else 0.0

    # Use the higher of the two metrics
    # This means if one title contains most of another, it's a duplicate
    return max(jaccard, containment)


def find_duplicate_task(
    queue: dict,
    title: str,
    check_completed: bool = True,
    max_completed_age_hours: float = RECENT_COMPLETION_HOURS,
    use_semantic_check: bool = True,
) -> dict | None:
    """
    Find a duplicate task using fuzzy matching with optional semantic checking.

    Duplicate Detection Logic:
    1. Token similarity >= 0.70: Block as duplicate (fast path)
    2. Token similarity 0.50-0.70: Run semantic check if enabled
       - Semantic similarity >= 0.80: Block as semantic duplicate
    3. Token similarity < 0.50: Not a duplicate

    Checks:
    1. Pending, blocked, in_progress, and pr_open queues (always)
    2. Recently completed tasks (if check_completed=True)

    Args:
        queue: The work queue dictionary
        title: The title to check for duplicates
        check_completed: Whether to check recently completed tasks
        max_completed_age_hours: Only check completed tasks within this many hours
        use_semantic_check: Whether to use Claude for semantic similarity (default True)

    Returns:
        Dict with duplicate info if found, None otherwise
    """
    now = datetime.now(timezone.utc)
    config = load_duplicate_detection_config() if use_semantic_check else None

    def _check_similarity(
        existing_title: str, existing_task: dict, status_key: str
    ) -> dict | None:
        """Inner function to check similarity with semantic fallback."""
        # Defensive: ensure existing_title is a string (may be dict in edge cases)
        existing_title_str = (
            existing_title
            if isinstance(existing_title, str)
            else existing_title.get("title", "")
        )
        token_sim = calculate_token_similarity(title, existing_title_str)

        # Fast path: High token similarity = definite duplicate
        if token_sim >= DUPLICATE_SIMILARITY_THRESHOLD:
            return {
                "task_id": existing_task.get("task_id"),
                "title": existing_title_str,
                "status": status_key,
                "similarity": round(token_sim, 2),
                "match_type": "exact" if token_sim == 1.0 else "fuzzy",
            }

        # Semantic check for borderline cases (0.50-0.70)
        if (
            use_semantic_check
            and config
            and config["semanticCheckEnabled"]
            and token_sim >= config["semanticCheckThreshold"]
            and token_sim < DUPLICATE_SIMILARITY_THRESHOLD
        ):
            semantic_sim = check_semantic_similarity(title, existing_title_str)

            if (
                semantic_sim is not None
                and semantic_sim >= config["semanticSimilarityThreshold"]
            ):
                return {
                    "task_id": existing_task.get("task_id"),
                    "title": existing_title_str,
                    "status": status_key,
                    "similarity": round(semantic_sim, 2),
                    "token_similarity": round(token_sim, 2),
                    "match_type": "semantic",
                }

        return None

    # Check active queues (always) — pr_open included to prevent re-minting while PRs exist
    for status_key in ["pending", "blocked", "in_progress", "pr_open"]:
        for existing_task in queue.get(status_key, []):
            existing_title = existing_task.get("title", "")
            result = _check_similarity(existing_title, existing_task, status_key)
            if result:
                return result

    # Check recently completed tasks
    if check_completed:
        max_age_seconds = max_completed_age_hours * 3600

        for existing_task in queue.get("completed", []):
            existing_title = existing_task.get("title", "")
            token_sim = calculate_token_similarity(title, existing_title)

            # Only check completion time if there's any potential match
            if (
                token_sim >= config["semanticCheckThreshold"]
                if config
                else SEMANTIC_CHECK_THRESHOLD_LOW
            ):
                completed_at_str = existing_task.get("completed_at")
                if completed_at_str:
                    try:
                        completed_at = datetime.fromisoformat(
                            completed_at_str.replace("Z", "+00:00")
                        )
                        age_seconds = (now - completed_at).total_seconds()

                        if age_seconds <= max_age_seconds:
                            # Now do the full similarity check
                            result = _check_similarity(
                                existing_title, existing_task, "completed"
                            )
                            if result:
                                result["match_type"] = "recent_completion"
                                result["completed_ago_hours"] = round(
                                    age_seconds / 3600, 1
                                )
                                return result
                    except (ValueError, TypeError):
                        pass  # Can't parse date, skip this task

    return None


def deduplicate_pending_tasks(
    dry_run: bool = False,
    similarity_threshold: float = 0.85,
) -> dict:
    """
    Find and remove duplicate tasks from the pending queue.

    Groups pending tasks by normalized title similarity. Within each group,
    keeps the oldest task (first queued) and marks the rest as deduplicated.

    Args:
        dry_run: If True, report duplicates without removing them.
        similarity_threshold: Minimum token similarity to consider duplicate.

    Returns:
        Dict with removed count, kept count, and duplicate details.
    """
    with QueueLock(get_lock_path()):
        queue = load_queue()
        pending = queue.get("pending", [])

        if len(pending) < 2:
            return {
                "success": True,
                "removed": 0,
                "kept": len(pending),
                "duplicates": [],
            }

        # Build groups of similar tasks
        groups: list[list[int]] = []  # each group is a list of indices
        assigned: set[int] = set()

        for i in range(len(pending)):
            if i in assigned:
                continue
            group = [i]
            assigned.add(i)
            title_i = pending[i].get("title", "")

            for j in range(i + 1, len(pending)):
                if j in assigned:
                    continue
                title_j = pending[j].get("title", "")
                sim = calculate_token_similarity(title_i, title_j)
                if sim >= similarity_threshold:
                    group.append(j)
                    assigned.add(j)

            if len(group) > 1:
                groups.append(group)

        if not groups:
            return {
                "success": True,
                "removed": 0,
                "kept": len(pending),
                "duplicates": [],
            }

        # For each group, keep the oldest (by created_at), remove the rest
        remove_indices: set[int] = set()
        duplicate_details = []

        for group in groups:
            # Sort by created_at ascending; keep the first
            sorted_group = sorted(
                group,
                key=lambda idx: pending[idx].get("created_at", ""),
            )
            keeper_idx = sorted_group[0]
            keeper = pending[keeper_idx]

            for dup_idx in sorted_group[1:]:
                dup = pending[dup_idx]
                duplicate_details.append(
                    {
                        "removed_task_id": dup.get("task_id"),
                        "removed_title": dup.get("title"),
                        "kept_task_id": keeper.get("task_id"),
                        "kept_title": keeper.get("title"),
                        "similarity": round(
                            calculate_token_similarity(
                                keeper.get("title", ""), dup.get("title", "")
                            ),
                            2,
                        ),
                    }
                )
                remove_indices.add(dup_idx)

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "would_remove": len(remove_indices),
                "kept": len(pending) - len(remove_indices),
                "duplicates": duplicate_details,
            }

        # Move duplicates to completed with status=deduplicated
        now = datetime.now(timezone.utc).isoformat()
        for idx in sorted(remove_indices, reverse=True):
            task = pending.pop(idx)
            task["status"] = "deduplicated"
            task["completed_at"] = now
            task["dedup_note"] = "Removed as duplicate by auto-dedup"
            queue["completed"].append(task)

        queue["pending"] = pending
        save_queue(queue)

    return {
        "success": True,
        "removed": len(remove_indices),
        "kept": len(pending),
        "duplicates": duplicate_details,
    }


# Valid source types for task origin tracking
VALID_SOURCES = {
    "human",
    "self",
    "escalation",
    "planning",
    "employee_initiative",
    "feedback_monitor",
    "cron",
    "gap_analysis",
}


def infer_capabilities(title: str, description: str = "") -> list[str]:
    """Infer required capabilities from task title and description keywords.

    Scans the combined text for known keywords and maps them to capability
    strings that match employee capability definitions in org.json.

    Args:
        title: Task title
        description: Task description

    Returns:
        De-duplicated list of inferred capability strings (order-preserving).
    """
    KEYWORD_TO_CAPABILITY = {
        # Testing
        "test": "testing",
        "pytest": "testing",
        "coverage": "testing",
        "spec": "testing",
        "unittest": "testing",
        "assert": "testing",
        # Security
        "security": "security",
        "audit": "security",
        "vulnerability": "security",
        "owasp": "security",
        "secrets": "security",
        # Documentation
        "document": "technical-documentation",
        "docs": "technical-documentation",
        "readme": "technical-documentation",
        "guide": "technical-documentation",
        # Code review
        "refactor": "code-review",
        "review": "code-review",
        "lint": "code-review",
        "ruff": "code-review",
        # DevOps
        "deploy": "devops",
        "ci": "devops",
        "pipeline": "devops",
        # Backend / Python
        "api": "backend",
        "endpoint": "backend",
        "server": "backend",
        "python": "python",
        "script": "python",
        "cli": "python",
        "daemon": "python",
        "fix": "python",
        "bug": "python",
        "implement": "python",
        "create": "python",
        "add": "python",
        "build": "python",
        "write": "python",
        "module": "python",
        "function": "python",
        "class": "python",
        "hook": "python",
        "worker": "python",
        "subprocess": "python",
        "validate": "testing",
        "verify": "testing",
        # Frontend
        "css": "frontend",
        "html": "frontend",
        "dashboard": "frontend",
        "ui": "frontend",
        # Architecture
        "architect": "architecture",
        "design": "design-decisions",
        # Installation
        "install": "installation",
        "distribution": "installation",
        "bash": "installation",
        "shell": "installation",
        "bin": "installation",
        # Web
        "website": "web-development",
        "page": "web-development",
        # Marketing
        "social": "marketing",
        "brand": "marketing",
        # Roadmap
        "roadmap": "roadmap",
        "plan": "roadmap",
    }

    text = f"{title} {description}".lower()
    # Tokenize on non-alphanumeric boundaries for whole-word matching
    words = set(re.split(r"[^a-z0-9]+", text))

    seen: set[str] = set()
    result: list[str] = []
    for keyword, capability in KEYWORD_TO_CAPABILITY.items():
        if keyword in words and capability not in seen:
            seen.add(capability)
            result.append(capability)

    return result


VALID_TEAM_PREFERENCES = {"persistent", "fresh", "mixed"}

# Pattern for valid employee IDs: lowercase alphanumeric + hyphens, max 64 chars
_EMPLOYEE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")


def create_task(
    title: str,
    priority: int = 3,
    department: str | None = None,
    required_capabilities: list[str] | None = None,
    deadline: str | None = None,
    estimated_complexity: str = "standard",
    dependencies: list[str] | None = None,
    description: str = "",
    project_id: str | None = None,
    source: str = "self",
    source_project_id: str | None = None,
    allowed_projects: list[str] | None = None,
    team_preference: str = "mixed",
    created_by: str | None = None,
    requires_deliverable: bool | None = None,
) -> dict:
    """
    Create a new task object.

    Args:
        title: Task title
        priority: 1-4 (1=Critical, 2=High, 3=Normal, 4=Low)
        department: Department ID
        required_capabilities: List of required agent capabilities
        deadline: ISO timestamp or None
        estimated_complexity: trivial|standard|complex|epic
        dependencies: List of task IDs this task depends on
        description: Detailed task description
        project_id: Project identifier (auto-detected if None)
        source: Task origin - "human" | "self" | "escalation" | "planning"
        source_project_id: Original project where task was created (for cross-project routing)
        allowed_projects: List of project IDs that can access this task (for access control)
        team_preference: Agent team member type preference - "persistent" (existing employees only),
            "fresh" (no team / single-agent), or "mixed" (default, allow both).
        created_by: Employee ID indicating who initiated this task (None for system-generated tasks).
        requires_deliverable: If True, task must produce PR or file changes to be considered complete.
            If None, auto-inferred from capabilities and title (code tasks require deliverables).

    Returns:
        Task dictionary
    """
    now = datetime.now(timezone.utc).isoformat()

    # Auto-detect project_id if not provided
    if project_id is None:
        project_id = get_current_project_id()

    # If source_project_id not provided, use project_id as source
    if source_project_id is None:
        source_project_id = project_id

    # Validate source
    validated_source = source if source in VALID_SOURCES else "self"

    # Validate and sanitize capabilities (prevents malformed entries)
    validated_capabilities = sanitize_capabilities(required_capabilities)

    # Auto-infer capabilities when none are provided (P50.7)
    if not validated_capabilities:
        validated_capabilities = infer_capabilities(title, description)

    # Determine if this is a cross-project task
    cross_project = (allowed_projects is not None and len(allowed_projects) > 1) or (
        source_project_id is not None and source_project_id != project_id
    )

    try:
        safe_priority = max(1, min(4, int(priority)))
    except (ValueError, TypeError):
        safe_priority = 3

    # Validate team_preference; fall back to "mixed" for unknown values
    validated_team_preference = (
        team_preference if team_preference in VALID_TEAM_PREFERENCES else "mixed"
    )

    # Validate created_by: must match employee ID format or be None
    validated_created_by = None
    if (
        created_by
        and isinstance(created_by, str)
        and _EMPLOYEE_ID_PATTERN.match(created_by)
    ):
        validated_created_by = created_by

    # P95: Auto-infer requires_deliverable if not explicitly set
    # WS-119 1.7: Expanded capability and verb lists so strategic "ship",
    # "enable", "integrate", "wire" tasks correctly require deliverables.
    inferred_requires_deliverable = requires_deliverable
    if inferred_requires_deliverable is None:
        # Code-related capabilities require deliverables
        code_capabilities = {
            "python",
            "javascript",
            "typescript",
            "frontend",
            "backend",
            "web-development",
            "web-design",
            "css",
            "html",
            "api",
            "database",
            "testing",
            "refactoring",
            "bug-fix",
            "feature",
            "implementation",
            "architecture",
            "code",
            "devops",
            "infrastructure",
            "packaging",
            "installation",
        }
        if any(cap.lower() in code_capabilities for cap in validated_capabilities):
            inferred_requires_deliverable = True

        # Code-related keywords in title require deliverables
        title_lower = title.lower()
        code_keywords = [
            "implement",
            "fix",
            "update",
            "add",
            "create",
            "modify",
            "refactor",
            "write",
            "build",
            "develop",
            "ship",
            "deliver",
            "deploy",
            "enable",
            "integrate",
            "wire",
            "remove",
            "delete",
            "rename",
            "migrate",
            "upgrade",
            "patch",
            "release",
        ]
        if any(kw in title_lower for kw in code_keywords):
            inferred_requires_deliverable = True

        # Default to True if still None — tasks should produce code changes
        # unless explicitly marked otherwise. This prevents doc/report busywork
        # from passing without a deliverable.
        if inferred_requires_deliverable is None:
            inferred_requires_deliverable = True

    return {
        "task_id": generate_task_id(),
        "title": title,
        "description": description,
        "priority": safe_priority,
        "department": department,
        "project_id": project_id,
        "required_capabilities": validated_capabilities,
        "created_at": now,
        "deadline": deadline,
        "estimated_complexity": estimated_complexity,
        "dependencies": dependencies or [],
        "dependencies_satisfied": len(dependencies or []) == 0,
        "source": validated_source,
        "assigned_to": None,
        "assigned_at": None,
        "started_at": None,
        "completed_at": None,
        # Cross-project routing fields (v1.1)
        "source_project_id": source_project_id,
        "target_project_id": None,
        "routing_reason": None,
        "routing_history": [],
        "cross_project": cross_project,
        "allowed_projects": allowed_projects or [],
        # Team composition preference (v1.2)
        "team_preference": validated_team_preference,
        # Attribution tracking (v1.3)
        "created_by": validated_created_by,
        # P95: Deliverable enforcement
        "requires_deliverable": inferred_requires_deliverable,
    }


# -----------------------------------------------------------------------------
# WS-105: Auto-Decomposition for Oversized Tasks
# -----------------------------------------------------------------------------


def _extract_file_list(description: str) -> list[str]:
    """Extract file names from task description."""
    files = []
    # Match patterns like "- file.py" or "- Missing docstring: file.py"
    for line in description.split("\n"):
        line = line.strip()
        if line.startswith("-"):
            # Extract .py, .ts, .js, .md files
            matches = re.findall(r"[\w_/]+\.\w{2,4}", line)
            files.extend(matches)
    return files


def _extract_list_items(description: str) -> list[str]:
    """Extract list items from task description."""
    items = []
    for line in description.split("\n"):
        line = line.strip()
        if line.startswith("-") or re.match(r"^\d+\.", line):
            items.append(line)
    return items


def _detect_file_count(title: str, description: str) -> int:
    """Detect how many files are mentioned in a task."""
    # Check title for patterns like "(91 files)" or "91 files"
    match = re.search(r"\(?\s*(\d+)\s*files?\s*\)?", title, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # Count files in description
    files = _extract_file_list(description)
    return len(files)


def _should_decompose(
    title: str, description: str, complexity: str
) -> tuple[bool, str]:
    """
    Check if a task should be auto-decomposed.

    Returns:
        (should_decompose, reason)
    """
    if not AUTO_DECOMPOSE_ENABLED:
        return False, ""

    # WS-112/WS-111: Prevent infinite recursion - never decompose already-decomposed tasks
    # Subtask titles contain "(batch ", "(part ", or "— category (N files)" markers
    if "(batch " in title or "(part " in title:
        return False, ""

    # WS-111: Also detect category-based decomposition pattern "— category (N files)"
    # These are created by _decompose_task when grouping by category
    if re.search(r" — \w+ \(\d+ files?\)$", title):
        return False, ""

    # Check file count
    file_count = _detect_file_count(title, description)
    if file_count > MAX_FILES_PER_TASK:
        return True, f"file_count:{file_count}"

    # Check list item count
    items = _extract_list_items(description)
    if len(items) > MAX_ITEMS_PER_TASK:
        return True, f"item_count:{len(items)}"

    # Epic tasks with explicit file lists should decompose
    if complexity == "epic":
        files = _extract_file_list(description)
        if len(files) > 5:
            return True, f"epic_with_files:{len(files)}"

    return False, ""


def _group_files_by_category(files: list[str]) -> dict[str, list[str]]:
    """Group files by functional category based on naming patterns."""
    categories: dict[str, list[str]] = {
        "daemon": [],
        "company": [],
        "strategy": [],
        "infrastructure": [],
        "external": [],
        "other": [],
    }

    for f in files:
        fname = f.lower()
        if any(k in fname for k in ["daemon", "loop", "worker", "executor", "signal"]):
            categories["daemon"].append(f)
        elif any(
            k in fname for k in ["employee", "manager", "allocator", "pool", "batch"]
        ):
            categories["company"].append(f)
        elif any(
            k in fname for k in ["strategy", "goal", "plan", "initiative", "roadmap"]
        ):
            categories["strategy"].append(f)
        elif any(
            k in fname for k in ["budget", "metric", "track", "monitor", "alert", "ci_"]
        ):
            categories["infrastructure"].append(f)
        elif any(
            k in fname for k in ["github", "webhook", "dashboard", "external", "api"]
        ):
            categories["external"].append(f)
        else:
            categories["other"].append(f)

    # Remove empty categories
    return {k: v for k, v in categories.items() if v}


def _decompose_task(
    title: str,
    description: str,
    priority: int,
    source: str,
    **kwargs,
) -> list[dict]:
    """
    Decompose an oversized task into smaller subtasks.

    Returns:
        List of subtask dicts ready for add_task.
    """
    subtasks = []
    files = _extract_file_list(description)

    if files:
        # Group files by category
        grouped = _group_files_by_category(files)

        for category, cat_files in grouped.items():
            # Further split if still too large
            for i in range(0, len(cat_files), MAX_FILES_PER_TASK):
                batch = cat_files[i : i + MAX_FILES_PER_TASK]
                batch_num = (i // MAX_FILES_PER_TASK) + 1

                # Build subtask
                if len(grouped) == 1:
                    subtask_title = f"{title} (batch {batch_num}/{(len(cat_files) + MAX_FILES_PER_TASK - 1) // MAX_FILES_PER_TASK})"
                else:
                    subtask_title = f"{title} — {category} ({len(batch)} files)"

                subtask_desc = f"Part of: {title}\n\nFiles:\n"
                subtask_desc += "\n".join(f"- {f}" for f in batch)

                subtasks.append(
                    {
                        "title": subtask_title,
                        "description": subtask_desc,
                        "priority": priority,
                        "source": source,
                        "estimated_complexity": "standard",  # Smaller = simpler
                        **{k: v for k, v in kwargs.items() if v is not None},
                    }
                )
    else:
        # No files detected — split by list items
        items = _extract_list_items(description)
        for i in range(0, len(items), MAX_ITEMS_PER_TASK):
            batch = items[i : i + MAX_ITEMS_PER_TASK]
            batch_num = (i // MAX_ITEMS_PER_TASK) + 1
            total_batches = (len(items) + MAX_ITEMS_PER_TASK - 1) // MAX_ITEMS_PER_TASK

            subtask_title = f"{title} (part {batch_num}/{total_batches})"
            subtask_desc = f"Part of: {title}\n\nItems:\n" + "\n".join(batch)

            subtasks.append(
                {
                    "title": subtask_title,
                    "description": subtask_desc,
                    "priority": priority,
                    "source": source,
                    "estimated_complexity": "standard",
                    **{k: v for k, v in kwargs.items() if v is not None},
                }
            )

    return subtasks


# --------------------------------------------------------------------------- #
# D8 Fix: Artifact Conflict Detection (prevent concurrent work on same files)
# --------------------------------------------------------------------------- #


def _extract_file_paths_from_text(text: str) -> set[str]:
    """Extract file paths from task title or description.

    Matches:
    - Python files: scripts/foo.py, tests/test_bar.py
    - Config files: forge-config.json, pyproject.toml
    - Markdown: docs/readme.md
    - Paths with slashes: .claude/hooks/company/foo.py

    Returns normalized, lowercase paths.
    """
    paths: set[str] = set()

    # Match file paths like "path/to/file.ext" or just "filename.ext"
    # Include common extensions
    pattern = r"[\w/./-]+\.(?:py|json|toml|md|ts|js|yml|yaml|sh|txt)"

    for match in re.finditer(pattern, text, re.IGNORECASE):
        path = match.group(0).lower()
        paths.add(path)

    return paths


def _extract_artifacts_from_task(task: dict) -> set[str]:
    """Extract artifact file paths from a task.

    Sources:
    1. Explicit "files" field (list or string)
    2. File paths in title
    3. File paths in description

    Returns normalized, lowercase artifact paths.
    """
    artifacts: set[str] = set()

    # Source 1: Explicit files field
    files_field = task.get("files") or task.get("file")
    if files_field:
        if isinstance(files_field, list):
            for f in files_field:
                if isinstance(f, str):
                    artifacts.add(f.lower())
        elif isinstance(files_field, str):
            for f in files_field.split(","):
                artifacts.add(f.strip().lower())

    # Source 2 & 3: Extract from title and description
    title = task.get("title") or ""
    description = task.get("description") or ""
    text = f"{title}\n{description}"
    artifacts.update(_extract_file_paths_from_text(text))

    return artifacts


def _find_artifact_conflicts(
    new_task_artifacts: set[str],
    queue: dict,
) -> list[dict]:
    """Find tasks with overlapping artifacts.

    Checks in_progress and pr_open tasks for any artifact overlap with the new task.
    Returns a list of blocking tasks.
    """
    blocking_tasks: list[dict] = []

    # Check in_progress and pr_open tasks (active work)
    for status in ["in_progress", "pr_open"]:
        for task in queue.get(status, []):
            task_artifacts = _extract_artifacts_from_task(task)

            # If any artifacts overlap, this task blocks the new task
            if new_task_artifacts & task_artifacts:
                blocking_tasks.append(
                    {
                        "task_id": task.get("task_id"),
                        "title": task.get("title"),
                        "status": status,
                        "overlapping_artifacts": sorted(
                            new_task_artifacts & task_artifacts
                        ),
                    }
                )

    return blocking_tasks


def add_artifact_blocking_note(task: dict, blocking_task_ids: list[str]) -> None:
    """Add blocking note to a task indicating which tasks are blocking it.

    Modifies the task in-place by adding/updating the `blocked_by_tasks` field.
    """
    if blocking_task_ids:
        task["blocked_by_tasks"] = blocking_task_ids


def guarded_requeue_to_pending(queue: dict, task: dict) -> bool:
    """Move *task* to ``pending[0]`` unless its ``task_id`` is already there.

    Caller **must** hold ``QueueLock``.  When the task_id is already present
    in ``pending`` the existing entry's description is updated to whichever of
    the two is longer (more informative), and this function returns *False* so
    the caller can log the skip.  When it is not present, the task is removed
    from ``in_progress``, ``failed``, and ``blocked`` first, then inserted at
    position 0.

    Returns:
        True  — task was inserted; it was not previously in pending.
        False — task_id was already in pending; description may be merged.
    """
    task_id = task.get("task_id")
    if not task_id:
        return False

    pending = queue.setdefault("pending", [])
    for existing in pending:
        if existing.get("task_id") == task_id:
            new_desc = task.get("description") or ""
            old_desc = existing.get("description") or ""
            if len(new_desc) > len(old_desc):
                existing["description"] = new_desc
            return False

    for section in ("in_progress", "failed", "blocked"):
        queue[section] = [
            t for t in queue.get(section, []) if t.get("task_id") != task_id
        ]

    pending.insert(0, task)
    return True


def add_task(
    title: str,
    priority: int = 3,
    department: str | None = None,
    required_capabilities: list[str] | None = None,
    deadline: str | None = None,
    estimated_complexity: str = "standard",
    dependencies: list[str] | None = None,
    description: str = "",
    project_id: str | None = None,
    source: str = "self",
    source_project_id: str | None = None,
    allowed_projects: list[str] | None = None,
    team_preference: str = "mixed",
    created_by: str | None = None,
    requires_deliverable: bool | None = None,
) -> dict:
    """
    Add a new task to the queue.

    Args:
        title: Task title
        priority: 1-4 (1=Critical, 2=High, 3=Normal, 4=Low)
        department: Department ID
        required_capabilities: List of required agent capabilities
        deadline: ISO timestamp or None
        estimated_complexity: trivial|standard|complex|epic
        dependencies: List of task IDs this task depends on
        description: Detailed task description
        project_id: Project identifier (auto-detected if None)
        source: Task origin - "human" | "self" | "escalation" | "planning"
        source_project_id: Original project where task was created (for cross-project routing)
        allowed_projects: List of project IDs that can access this task (for access control)
        team_preference: Agent team member type preference - "persistent" (existing employees only),
            "fresh" (no team / single-agent), or "mixed" (default, allow both).
        created_by: Employee ID indicating who initiated this task (None for system-generated tasks).
        requires_deliverable: If True, task must produce PR or file changes to be considered complete.
            If None, auto-inferred from capabilities and title.

    Returns:
        Dict with task_id and status.
    """
    # WS-105: Check for auto-decomposition BEFORE acquiring lock
    should_split, reason = _should_decompose(title, description, estimated_complexity)
    if should_split:
        subtasks = _decompose_task(
            title=title,
            description=description,
            priority=priority,
            source=source,
            department=department,
            required_capabilities=required_capabilities,
            deadline=deadline,
            project_id=project_id,
            source_project_id=source_project_id,
            allowed_projects=allowed_projects,
            team_preference=team_preference,
            created_by=created_by,
        )

        # Recursively add subtasks (won't decompose further due to WS-112 guard)
        subtask_ids = []
        for subtask in subtasks:
            result = add_task(**subtask)
            if result.get("success"):
                # WS-112: Handle both single task (task_id) and decomposed (subtask_ids) results
                if "task_id" in result:
                    subtask_ids.append(result["task_id"])
                elif "subtask_ids" in result:
                    subtask_ids.extend(result["subtask_ids"])

        return {
            "success": True,
            "decomposed": True,
            "reason": reason,
            "original_title": title,
            "subtask_count": len(subtask_ids),
            "subtask_ids": subtask_ids,
        }

    # Normal path: single task
    with QueueLock(get_lock_path()):
        queue = load_queue()

        # P26 Fix: Enhanced duplicate detection with fuzzy matching + recent completion check
        duplicate = find_duplicate_task(
            queue=queue,
            title=title,
            check_completed=True,
            max_completed_age_hours=RECENT_COMPLETION_HOURS,
        )

        if duplicate:
            match_type = duplicate.get("match_type", "exact")
            similarity = duplicate.get("similarity", 1.0)

            if match_type == "recent_completion":
                hours_ago = duplicate.get("completed_ago_hours", 0)
                message = (
                    f"Similar task '{duplicate['title']}' was completed {hours_ago}h ago "
                    f"({int(similarity * 100)}% similar)"
                )
            elif match_type == "semantic":
                token_sim = duplicate.get("token_similarity", 0)
                message = (
                    f"Semantically similar task '{duplicate['title']}' already exists in {duplicate['status']} "
                    f"({int(similarity * 100)}% semantic similarity, {int(token_sim * 100)}% token overlap)"
                )
            elif match_type == "fuzzy":
                message = (
                    f"Similar task '{duplicate['title']}' already exists in {duplicate['status']} "
                    f"({int(similarity * 100)}% similar)"
                )
            else:
                message = (
                    f"Task with title '{title}' already exists in {duplicate['status']}"
                )

            return {
                "success": False,
                "error": "duplicate_task",
                "message": message,
                "existing_task_id": duplicate.get("task_id"),
                "existing_status": duplicate.get("status"),
                "similarity": similarity,
                "match_type": match_type,
            }

        # WS-018-001: Check if feature was already shipped via merged PR
        shipped_pr = is_shipped_in_merged_pr(title)
        if shipped_pr:
            pr_num = shipped_pr.get("pr_number")
            pr_title = shipped_pr.get("pr_title", "")
            similarity = shipped_pr.get("similarity", 0)
            return {
                "success": False,
                "error": "already_shipped",
                "message": (
                    f"Feature already shipped in PR #{pr_num}: '{pr_title}' "
                    f"({int(similarity * 100)}% similar)"
                ),
                "pr_number": pr_num,
                "pr_title": pr_title,
                "merged_at": shipped_pr.get("merged_at"),
                "similarity": similarity,
                "match_type": "merged_pr",
            }

        task = create_task(
            title=title,
            priority=priority,
            department=department,
            required_capabilities=required_capabilities,
            deadline=deadline,
            estimated_complexity=estimated_complexity,
            dependencies=dependencies,
            description=description,
            project_id=project_id,
            source=source,
            source_project_id=source_project_id,
            allowed_projects=allowed_projects,
            team_preference=team_preference,
            created_by=created_by,
            requires_deliverable=requires_deliverable,
        )

        # Phase 1 (autonomy): task-admission gate. Reject redundant / invalid /
        # already-done self-generated tasks before they enter the queue. Human
        # tasks bypass inside the gate; gate failures fail open.
        admitted, admit_reason = _run_admission_gate(task)
        if not admitted:
            return {
                "success": False,
                "error": "admission_rejected",
                "reason": admit_reason,
                "title": title,
                "source": source,
            }

        # D8 Fix: Check for artifact conflicts with in_progress/pr_open tasks.
        # If artifacts overlap, hold the task in pending with a blocking note.
        task_artifacts = _extract_artifacts_from_task(task)
        blocking_tasks = _find_artifact_conflicts(task_artifacts, queue)

        if blocking_tasks:
            blocking_task_ids = [t["task_id"] for t in blocking_tasks]
            add_artifact_blocking_note(task, blocking_task_ids)

        # Add to pending or blocked based on dependencies and artifact conflicts.
        # Artifact conflicts don't prevent admission; they just add a blocking note
        # so the scheduler can see why a task isn't running yet.
        if task["dependencies_satisfied"]:
            queue["pending"].append(task)
        else:
            queue["blocked"].append(task)

        save_queue(queue)

    artifact_conflict_msg = None
    if blocking_tasks:
        artifact_conflict_msg = (
            f"Waiting on {len(blocking_tasks)} task(s) working on same artifacts"
        )

    return {
        "success": True,
        "task_id": task["task_id"],
        "status": "pending" if task["dependencies_satisfied"] else "blocked",
        "task": task,
        "blocked_by_tasks": [t["task_id"] for t in blocking_tasks]
        if blocking_tasks
        else None,
        "artifact_conflict_note": artifact_conflict_msg,
    }


def get_agent_workload(queue: dict, agent_id: str) -> int:
    """Count current tasks assigned to an agent."""
    count = 0
    for task in queue.get("in_progress", []):
        if task.get("assigned_to") == agent_id:
            count += 1
    return count


def capabilities_match(required: list[str], available: list[str]) -> bool:
    """Check if agent capabilities satisfy task requirements."""
    if not required:
        return True
    return all(cap in available for cap in required)


def get_efficiency_score_for_task(agent_id: str, task: dict) -> float | None:
    """
    Get efficiency score for an agent handling a specific task type.

    Uses efficiency_tracker to find if there's a learned pattern for
    tasks matching this task's tags/type that would make this agent
    more or less efficient.

    Args:
        agent_id: The agent/employee ID
        task: Task dictionary with potential tags

    Returns:
        Efficiency score (0.0-1.0+) if found, None if no data
    """
    _ensure_imports()
    if not efficiency_tracker:
        return None

    try:
        # Get task tags for pattern matching
        tags = task.get("tags", [])
        complexity = task.get("estimated_complexity", "standard")

        # Try to get a suggestion based on task patterns
        suggestion = efficiency_tracker.suggest_optimal_employee(
            tags=tags, complexity=complexity
        )

        if suggestion and suggestion.get("employee_id") == agent_id:
            # This agent is the optimal choice for this task type
            return suggestion.get("efficiency_score", 1.0)

        # Get the agent's general efficiency score
        report = efficiency_tracker.get_efficiency_report()
        if report:
            employee_breakdown = report.get("employee_breakdown", [])
            for emp in employee_breakdown:
                if emp.get("employee_id") == agent_id:
                    return emp.get("efficiency_score", 0.8)

        return None
    except Exception:
        return None


def suggest_optimal_agent_for_task(
    task: dict, candidate_agents: list[str]
) -> str | None:
    """
    Suggest the optimal agent for a task based on efficiency patterns.

    Uses learned patterns from efficiency_tracker to determine which
    agent would be most efficient for a given task type.

    Args:
        task: Task dictionary with tags and complexity
        candidate_agents: List of agent IDs that could handle the task

    Returns:
        Optimal agent ID if a pattern exists, None otherwise
    """
    _ensure_imports()
    if not efficiency_tracker or not candidate_agents:
        return None

    try:
        tags = task.get("tags", [])
        complexity = task.get("estimated_complexity", "standard")

        suggestion = efficiency_tracker.suggest_optimal_employee(
            tags=tags, complexity=complexity
        )

        if suggestion:
            suggested_id = suggestion.get("employee_id")
            if suggested_id in candidate_agents:
                return suggested_id

        return None
    except Exception:
        return None


def update_dependencies(queue: dict) -> bool:
    """
    Check blocked tasks and move to pending if dependencies satisfied.
    Also re-check pending tasks that have dependencies_satisfied=false.

    Dependencies can reference either task_id or source_goal of completed tasks.
    This allows goal-based dependencies (e.g., "W2-MAR-EMPLOYEE") to resolve
    when the task with that source_goal completes.

    Returns True if any tasks were updated.
    """
    completed = queue.get("completed", [])
    # Collect task_ids, source_goals, AND roadmap_task_ids from completed tasks.
    # roadmap_task_id is stored in the notes JSON blob by roadmap_scheduler so
    # that tasks blocked on a raw roadmap ID (e.g. "P14-0.1") self-heal once
    # the corresponding queue task completes.
    completed_ids = set()
    for task in completed:
        tid = task.get("task_id")
        if tid:
            completed_ids.add(tid)
        if task.get("source_goal"):
            completed_ids.add(task["source_goal"])
        # notes is stored by update_task as a list of {"timestamp", "content"}
        # dicts; handle both list-format (normal path) and bare-string (legacy).
        notes_raw = task.get("notes")
        if notes_raw:
            try:
                if isinstance(notes_raw, list):
                    for entry in notes_raw:
                        if not isinstance(entry, dict):
                            continue
                        content = entry.get("content", "")
                        if not isinstance(content, str):
                            continue
                        inner = json.loads(content)
                        rtid = inner.get("roadmap_task_id")
                        if rtid:
                            completed_ids.add(rtid)
                elif isinstance(notes_raw, str):
                    # Bare-string notes accrete free text over a task's
                    # lifetime ('{...} | reset ...'), so strict json.loads
                    # would silently skip them — extract the id tolerantly.
                    m = re.search(r'"roadmap_task_id"\s*:\s*"([^"]+)"', notes_raw)
                    if m:
                        completed_ids.add(m.group(1))
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                pass

    updated = False

    # Process blocked tasks -> move to pending if satisfied
    blocked = queue.get("blocked", [])
    still_blocked = []

    for task in blocked:
        dependencies = task.get("dependencies", [])
        if all(dep_id in completed_ids for dep_id in dependencies):
            task["dependencies_satisfied"] = True
            queue["pending"].append(task)
            updated = True
        else:
            still_blocked.append(task)

    queue["blocked"] = still_blocked

    # Also re-check pending tasks with dependencies_satisfied=false
    # (handles cases where task was created with unsatisfied deps but deps now satisfied)
    for task in queue.get("pending", []):
        if not task.get("dependencies_satisfied", True):
            dependencies = task.get("dependencies", [])
            if all(dep_id in completed_ids for dep_id in dependencies):
                task["dependencies_satisfied"] = True
                updated = True

    return updated


def pull_next_task(
    agent_id: str,
    capabilities: list[str],
    department: str | None = None,
    project_id: str | None = None,
    all_projects: bool = False,
    use_efficiency_routing: bool = True,
) -> dict:
    """
    Pull the next available task for an agent.

    Selection criteria (in order):
    1. Filter by satisfied dependencies
    2. Filter by matching capabilities
    3. Filter by department (if specified)
    4. Filter by project_id (if specified or auto-detected, unless all_projects=True)
    5. Sort by: priority ASC, deadline ASC (nulls last), created_at ASC
    6. If efficiency routing enabled: boost tasks where this agent has high efficiency
    7. Check agent workload (max 2 concurrent tasks)
    8. Atomically claim task via file-based locking

    Args:
        agent_id: The agent requesting work
        capabilities: List of agent capabilities
        department: Optional department filter
        project_id: Optional project filter (auto-detected if None and not all_projects)
        all_projects: If True, pull tasks from all projects (ignore project filter)
        use_efficiency_routing: If True, consider efficiency patterns when selecting tasks
            (G6 Economics feature - routes tasks to agents with proven efficiency)

    Returns:
        Dict with success status and task (if claimed)
    """
    # Input validation
    if not agent_id or not str(agent_id).strip():
        return {"success": False, "reason": "invalid_agent_id", "task": None}
    # Normalize None capabilities to empty list
    if capabilities is None:
        capabilities = []

    # Auto-detect project_id if not specified and not pulling from all projects
    if project_id is None and not all_projects:
        project_id = get_current_project_id()

    with QueueLock(get_lock_path()):
        queue = load_queue()

        # First, update dependencies to move any unblocked tasks
        update_dependencies(queue)

        # Check agent workload
        current_workload = get_agent_workload(queue, agent_id)
        if current_workload >= MAX_CONCURRENT_TASKS:
            save_queue(queue)
            return {
                "success": False,
                "reason": "max_workload",
                "message": f"Agent has {current_workload} tasks (max {MAX_CONCURRENT_TASKS})",
                "agent_id": agent_id,
                "current_tasks": current_workload,
            }

        # Filter pending tasks
        pending = queue.get("pending", [])
        candidates = []

        for task in pending:
            # Check capabilities
            if not capabilities_match(
                task.get("required_capabilities", []), capabilities
            ):
                continue

            # Check department (if specified)
            if (
                department
                and task.get("department")
                and task["department"] != department
            ):
                continue

            # Check project_id (if specified and not all_projects)
            if project_id and not all_projects:
                task_project = task.get("project_id")
                target_project = task.get("target_project_id")

                # Use target_project_id if set (task was routed), otherwise project_id
                effective_project = target_project or task_project

                # Allow tasks with no project_id (legacy) or matching project_id
                if effective_project is not None and effective_project != project_id:
                    continue

            # Check allowed_projects access control
            allowed_projects = task.get("allowed_projects", [])
            if allowed_projects:
                # Task has access restrictions - validate agent has project access
                _ensure_imports()
                has_access = False
                for allowed_project in allowed_projects:
                    if project_orchestrator.validate_cross_project_access(
                        agent_id, allowed_project
                    ):
                        has_access = True
                        break
                if not has_access:
                    continue

            # When all_projects=True, include cross-project tasks
            if all_projects:
                # Include tasks where this agent has access via allowed_projects
                # or tasks that are explicitly cross-project
                pass  # Already included via normal flow

            # Dependencies must be satisfied
            if not task.get("dependencies_satisfied", True):
                continue

            # WS-119 1.6: Skip engine-modifying tasks. The daemon cannot
            # safely modify its own running code. These require human
            # handling (stop daemon, edit, restart) per CLAUDE.md.
            if is_engine_modifying_task(task):
                continue

            candidates.append(task)

        if not candidates:
            save_queue(queue)
            return {
                "success": False,
                "reason": "no_tasks",
                "message": "No matching tasks available",
                "agent_id": agent_id,
                "pending_count": len(pending),
                "filtered_out": len(pending) - len(candidates),
            }

        # Sort candidates: priority ASC, deadline ASC (nulls last), created_at ASC
        # With efficiency routing: boost tasks where this agent has high efficiency
        def sort_key(task):
            priority = task.get("priority", 3)
            deadline = task.get("deadline")
            created = task.get("created_at", "")

            # For deadline sorting: None/null goes last
            if deadline:
                deadline_sort = deadline
            else:
                deadline_sort = "9999-99-99"  # Far future

            # Efficiency boost: reduce effective priority for tasks where
            # this agent has demonstrated high efficiency (G6 Economics)
            efficiency_boost = 0.0
            if use_efficiency_routing:
                efficiency_score = get_efficiency_score_for_task(agent_id, task)
                if efficiency_score is not None and efficiency_score > 0.9:
                    # High efficiency agents get priority boost (lower = higher priority)
                    # This moves efficient matches higher in the sort order
                    efficiency_boost = -0.5

            return (priority + efficiency_boost, deadline_sort, created)

        candidates.sort(key=sort_key)

        # Project D finding D8: Detect artifact collisions before claiming.
        # If a candidate task's files intersect with any in_progress or pr_open
        # task, skip it and note the blocking task. This serializes concurrent
        # modifications to the same files.
        active_tasks = queue.get("in_progress", []) + queue.get("pr_open", [])
        claimed_task = None
        efficiency_matched = False
        ss = _import_snipstash()

        for candidate in candidates:
            if ss:
                # Check for artifact collisions with active tasks
                collision = ss.detect_artifact_collision(candidate, active_tasks)
                if collision:
                    # Mark candidate with collision note for visibility
                    if "notes" not in candidate:
                        candidate["notes"] = ""
                    collision_note = ss.format_collision_note(collision)
                    if collision_note not in candidate.get("notes", ""):
                        candidate["notes"] = (
                            candidate.get("notes", "") + "\n" + collision_note
                        ).strip()
                    continue  # Skip this candidate, try next

            # With efficiency routing, prefer optimal matches
            if claimed_task is None:
                # This is the first non-colliding candidate
                if use_efficiency_routing and len(candidates) > 1:
                    # Check if any future candidate is an optimal match
                    optimal_task = None
                    for task in candidates[candidates.index(candidate) :]:
                        if ss:
                            collision = ss.detect_artifact_collision(task, active_tasks)
                            if collision:
                                continue
                        optimal_agent = suggest_optimal_agent_for_task(task, [agent_id])
                        if optimal_agent == agent_id:
                            optimal_task = task
                            efficiency_matched = True
                            break
                    claimed_task = optimal_task or candidate
                else:
                    claimed_task = candidate

            if claimed_task is not None:
                break

        if claimed_task is None:
            save_queue(queue)
            return {
                "success": False,
                "reason": "artifact_collision",
                "message": "All available tasks have artifact collisions with active work",
                "agent_id": agent_id,
                "pending_count": len(pending),
                "blocked_by_artifacts": len(
                    [
                        c
                        for c in candidates
                        if ss and ss.detect_artifact_collision(c, active_tasks)
                    ]
                )
                if ss
                else 0,
            }

        now = datetime.now(timezone.utc).isoformat()

        # Update task
        claimed_task["assigned_to"] = agent_id
        claimed_task["assigned_at"] = now
        claimed_task["started_at"] = now

        # Move from pending to in_progress
        queue["pending"] = [
            t for t in pending if t["task_id"] != claimed_task["task_id"]
        ]
        queue["in_progress"].append(claimed_task)

        save_queue(queue)

    # Build success message with efficiency info
    message = f"Claimed task: {claimed_task['title']}"
    if use_efficiency_routing and efficiency_matched:
        message += " (efficiency-optimized match)"

    return {
        "success": True,
        "agent_id": agent_id,
        "task": claimed_task,
        "message": message,
        "efficiency_routing": use_efficiency_routing,
        "efficiency_matched": efficiency_matched if use_efficiency_routing else False,
    }


def get_task(task_id: str) -> dict:
    """Get a specific task by ID."""
    queue = load_queue()

    # P26: Search all queue states including proposed/review
    all_statuses = [
        "proposed",
        "pending",
        "in_progress",
        "blocked",
        "review",
        "pr_open",
        "completed",
    ]
    for status in all_statuses:
        for task in queue.get(status, []):
            if task["task_id"] == task_id:
                return {
                    "success": True,
                    "task": task,
                    "status": status,
                }

    return {
        "success": False,
        "reason": "not_found",
        "message": f"Task {task_id} not found",
    }


def update_task(
    task_id: str,
    status: str | None = None,
    progress: str | None = None,
    notes: str | None = None,
    pr_url: str | None = None,
) -> dict:
    """
    Update a task's status or metadata.

    Args:
        task_id: Task to update
        status: New status (proposed, pending, in_progress, blocked, review, completed)
        progress: Progress notes
        notes: Additional notes

    Returns:
        Dict with success status and updated task
    """
    # Input validation
    if not task_id or not str(task_id).strip():
        return {
            "success": False,
            "reason": "invalid_task_id",
            "message": "task_id is required",
        }

    # P26: Added "proposed" and "review" states; pr_open added for post-PR state
    valid_statuses = {
        "proposed",
        "pending",
        "in_progress",
        "blocked",
        "review",
        "pr_open",
        "completed",
    }

    if status and status not in valid_statuses:
        return {
            "success": False,
            "reason": "invalid_status",
            "message": f"Status must be one of: {valid_statuses}",
        }

    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Find the task - P26: Search all queue states including proposed/review
        found_task = None
        found_status = None

        all_statuses = [
            "proposed",
            "pending",
            "in_progress",
            "blocked",
            "failed",
            "review",
            "pr_open",
            "completed",
        ]
        for current_status in all_statuses:
            for i, task in enumerate(queue.get(current_status, [])):
                if task["task_id"] == task_id:
                    found_task = task
                    found_status = current_status
                    break
            if found_task:
                break

        if not found_task:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Task {task_id} not found",
            }

        # Update task metadata
        if progress:
            found_task["progress"] = progress

        if pr_url:
            found_task["pr_url"] = pr_url

        if notes:
            if "notes" not in found_task:
                found_task["notes"] = []
            found_task["notes"].append({"timestamp": now, "content": notes})

        # Handle status change
        if status and status != found_status:
            # Remove from current status list
            queue[found_status] = [
                t for t in queue[found_status] if t["task_id"] != task_id
            ]

            # Update timestamps based on new status
            if status == "completed":
                found_task["completed_at"] = now
            elif status == "pr_open":
                found_task["pr_open_at"] = now
            elif status == "review":
                # P26: Task moving to review (awaiting manager review)
                found_task["review_requested_at"] = now
            elif status == "pending" and found_status == "proposed":
                # P26: Proposal approved, track approval time
                found_task["approved_at"] = now
            elif status == "in_progress" and not found_task.get("started_at"):
                found_task["started_at"] = now

            # Add to new status list
            queue[status].append(found_task)

            # If task completed, check if it unblocks other tasks
            if status == "completed":
                update_dependencies(queue)

        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "previous_status": found_status,
        "new_status": status or found_status,
        "task": found_task,
    }


def list_tasks(
    status: str | None = None,
    project_id: str | None = None,
    all_projects: bool = False,
) -> dict:
    """
    List tasks, optionally filtered by status and/or project.

    Args:
        status: Filter by status (proposed, pending, in_progress, blocked, review, completed)
        project_id: Filter by project (auto-detected if None and not all_projects)
        all_projects: If True, list tasks from all projects

    Returns:
        Dict with tasks grouped by status
    """
    # Auto-detect project_id if not specified and not listing all projects
    if project_id is None and not all_projects:
        project_id = get_current_project_id()

    queue = load_queue()

    def filter_by_project(tasks: list) -> list:
        """Filter tasks by project_id."""
        if all_projects or project_id is None:
            return tasks
        return [
            t
            for t in tasks
            if t.get("project_id") is None or t.get("project_id") == project_id
        ]

    if status:
        filtered = filter_by_project(queue.get(status, []))
        return {
            "success": True,
            status: filtered,
            "count": len(filtered),
            "project_id": project_id,
            "all_projects": all_projects,
        }

    # P26: Include proposed and review queues
    proposed = filter_by_project(queue.get("proposed", []))
    pending = filter_by_project(queue.get("pending", []))
    in_progress = filter_by_project(queue.get("in_progress", []))
    blocked = filter_by_project(queue.get("blocked", []))
    review = filter_by_project(queue.get("review", []))
    completed = filter_by_project(queue.get("completed", []))

    return {
        "success": True,
        "proposed": proposed,
        "pending": pending,
        "in_progress": in_progress,
        "blocked": blocked,
        "review": review,
        "completed": completed,
        "counts": {
            "proposed": len(proposed),
            "pending": len(pending),
            "in_progress": len(in_progress),
            "blocked": len(blocked),
            "review": len(review),
            "completed": len(completed),
        },
        "project_id": project_id,
        "all_projects": all_projects,
    }


def get_workload(agent_id: str) -> dict:
    """Get workload information for an agent."""
    queue = load_queue()

    assigned_tasks = []
    for task in queue.get("in_progress", []):
        if task.get("assigned_to") == agent_id:
            assigned_tasks.append(task)

    return {
        "success": True,
        "agent_id": agent_id,
        "current_tasks": len(assigned_tasks),
        "max_tasks": MAX_CONCURRENT_TASKS,
        "available_slots": MAX_CONCURRENT_TASKS - len(assigned_tasks),
        "tasks": assigned_tasks,
    }


def release_task(task_id: str, agent_id: str) -> dict:
    """
    Release a task back to the pending queue.

    Useful when an agent cannot complete a task.
    """
    # Input validation
    if not task_id or not str(task_id).strip():
        return {
            "success": False,
            "reason": "invalid_task_id",
            "message": "task_id is required",
        }
    if not agent_id or not str(agent_id).strip():
        return {
            "success": False,
            "reason": "invalid_agent_id",
            "message": "agent_id is required",
        }

    with QueueLock(get_lock_path()):
        queue = load_queue()

        # Find task in in_progress
        found_task = None
        for i, task in enumerate(queue.get("in_progress", [])):
            if task["task_id"] == task_id:
                if task.get("assigned_to") != agent_id:
                    return {
                        "success": False,
                        "reason": "not_assigned",
                        "message": f"Task is not assigned to {agent_id}",
                    }
                found_task = task
                break

        if not found_task:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Task {task_id} not found in in_progress",
            }

        # Release the task
        found_task["assigned_to"] = None
        found_task["assigned_at"] = None
        now = datetime.now(timezone.utc).isoformat()
        if "release_history" not in found_task:
            found_task["release_history"] = []
        found_task["release_history"].append(
            {
                "agent_id": agent_id,
                "released_at": now,
            }
        )

        # Move back to pending
        queue["in_progress"] = [
            t for t in queue["in_progress"] if t["task_id"] != task_id
        ]
        queue["pending"].append(found_task)

        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "agent_id": agent_id,
        "message": "Task released back to pending queue",
    }


def route_task(
    task_id: str,
    target_project_id: str,
    employee_id: str,
    reason: str,
) -> dict:
    """
    Route a task to a different project.

    Validates employee has access to target project before routing.
    Maintains routing_history for audit trail.

    Args:
        task_id: The task ID to route
        target_project_id: Target project ID
        employee_id: Employee requesting the route (for access validation)
        reason: Reason for routing

    Returns:
        Dict with:
            - success: bool
            - task_id: The routed task ID
            - source_project_id: Original project ID
            - target_project_id: Target project ID
            - reason: Routing reason
            - error: Error message if failed
    """
    _ensure_imports()

    # Input validation
    if not task_id or not str(task_id).strip():
        return {
            "success": False,
            "reason": "invalid_task_id",
            "task_id": task_id,
            "error": "task_id is required",
        }
    if not target_project_id or not str(target_project_id).strip():
        return {
            "success": False,
            "reason": "invalid_target_project_id",
            "task_id": task_id,
            "error": "target_project_id is required",
        }
    if not employee_id or not str(employee_id).strip():
        return {
            "success": False,
            "reason": "invalid_employee_id",
            "task_id": task_id,
            "error": "employee_id is required",
        }
    if not reason or not str(reason).strip():
        return {
            "success": False,
            "reason": "invalid_reason",
            "task_id": task_id,
            "error": "reason is required",
        }

    # Validate employee has access to target project
    if not project_orchestrator.validate_cross_project_access(
        employee_id, target_project_id
    ):
        return {
            "success": False,
            "task_id": task_id,
            "source_project_id": None,
            "target_project_id": target_project_id,
            "reason": reason,
            "error": f"Access denied: Employee '{employee_id}' does not have access to project '{target_project_id}'",
        }

    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Find the task in any status
        found_task = None

        for status in ["pending", "in_progress", "blocked", "completed"]:
            for task in queue.get(status, []):
                if task["task_id"] == task_id:
                    found_task = task
                    break
            if found_task:
                break

        if not found_task:
            return {
                "success": False,
                "task_id": task_id,
                "source_project_id": None,
                "target_project_id": target_project_id,
                "reason": reason,
                "error": f"Task '{task_id}' not found in work queue",
            }

        # Get current project info
        source_project_id = found_task.get("target_project_id") or found_task.get(
            "project_id"
        )

        # Create routing event for history
        routing_event = {
            "from_project": source_project_id,
            "to_project": target_project_id,
            "routed_at": now,
            "routed_by": employee_id,
            "reason": reason,
        }

        # Update task with routing metadata
        found_task["target_project_id"] = target_project_id
        found_task["routing_reason"] = reason
        found_task["cross_project"] = True

        # Initialize or update routing_history
        if "routing_history" not in found_task:
            found_task["routing_history"] = []
        found_task["routing_history"].append(routing_event)

        # Preserve source_project_id if not set
        if not found_task.get("source_project_id"):
            found_task["source_project_id"] = source_project_id

        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "reason": reason,
        "routed_at": now,
        "routing_history": found_task.get("routing_history", []),
    }


def get_cross_project_tasks() -> list[dict]:
    """
    Get all cross-project tasks from the queue.

    Returns tasks where:
    - cross_project flag is True, OR
    - routing_history is non-empty

    Includes full routing history for audit purposes.

    Returns:
        List of task dictionaries with routing history
    """
    queue = load_queue()
    cross_project_tasks = []

    # P26: Include all 6 queue statuses including proposed and review
    for status in [
        "proposed",
        "pending",
        "in_progress",
        "blocked",
        "review",
        "completed",
    ]:
        for task in queue.get(status, []):
            is_cross_project = task.get("cross_project", False)
            has_routing_history = bool(task.get("routing_history"))

            if is_cross_project or has_routing_history:
                # Include task with status info
                task_info = {
                    **task,
                    "current_status": status,
                }
                cross_project_tasks.append(task_info)

    return cross_project_tasks


def cleanup_stale_tasks(
    max_age_hours: float = 24.0,
    archive: bool = True,
    dry_run: bool = False,
    stuck_in_progress_hours: float | None = None,
) -> dict:
    """
    P22 Fix 2: Clean up stale tasks from the queue.

    Tasks older than max_age_hours in pending/blocked status are either:
    - archived to completed with status="stale" (if archive=True)
    - deleted outright (if archive=False)

    Tasks stuck in in_progress longer than stuck_in_progress_hours with
    retry_count >= 2 are moved to completed with status="failed".

    Args:
        max_age_hours: Maximum age in hours for pending/blocked tasks (default: 24)
        archive: If True, move stale tasks to completed with stale marker
                 If False, delete them entirely
        dry_run: If True, report what would be done without making changes
        stuck_in_progress_hours: Hours before in_progress tasks are cleaned up

    Returns:
        Dict with cleanup results including released_count
    """
    now = datetime.now(timezone.utc)
    max_age_seconds = max_age_hours * 3600
    stale_tasks = []
    cleaned_count = 0

    with QueueLock(get_lock_path()):
        queue = load_queue()

        for status_key in ["pending", "blocked"]:
            tasks_to_keep = []
            for task in queue.get(status_key, []):
                created_at_str = task.get("created_at")
                if not created_at_str:
                    tasks_to_keep.append(task)
                    continue

                try:
                    created_at = datetime.fromisoformat(
                        created_at_str.replace("Z", "+00:00")
                    )
                    age_seconds = (now - created_at).total_seconds()

                    if age_seconds > max_age_seconds:
                        stale_tasks.append(
                            {
                                "task_id": task.get("task_id"),
                                "title": task.get("title"),
                                "status": status_key,
                                "age_hours": round(age_seconds / 3600, 1),
                                "created_at": created_at_str,
                            }
                        )
                        cleaned_count += 1

                        if not dry_run:
                            if archive:
                                # Mark as stale and move to completed
                                task["status"] = "stale"
                                task["completed_at"] = now.isoformat()
                                task["stale_reason"] = (
                                    f"Auto-archived after {max_age_hours}h"
                                )
                                queue.setdefault("completed", []).append(task)
                            # else: task is simply not kept (deleted)
                    else:
                        tasks_to_keep.append(task)
                except (ValueError, TypeError):
                    # Can't parse date, keep the task
                    tasks_to_keep.append(task)

            if not dry_run:
                queue[status_key] = tasks_to_keep

        # Handle stuck in_progress tasks
        released_count = 0
        if stuck_in_progress_hours is not None:
            stuck_seconds = stuck_in_progress_hours * 3600
            in_progress_to_keep = []
            for task in queue.get("in_progress", []):
                started_at_str = task.get("started_at")
                if not started_at_str:
                    in_progress_to_keep.append(task)
                    continue

                try:
                    started_at = datetime.fromisoformat(
                        started_at_str.replace("Z", "+00:00")
                    )
                    stuck_time = (now - started_at).total_seconds()

                    if stuck_time > stuck_seconds:
                        # Zombie watchdog: any task stuck >stuck_seconds is a zombie.
                        # Previously required retry_count >= 2, but true zombies
                        # (worker died silently) have retry_count=0 and never
                        # triggered cleanup — leaving tasks stuck for days.
                        if not dry_run:
                            task["status"] = "failed"
                            task["completed_at"] = now.isoformat()
                            task["failure_reason"] = (
                                f"Zombie: stuck {round(stuck_time / 3600, 1)}h in_progress "
                                f"(retry_count={task.get('retry_count', 0)})"
                            )
                            queue.setdefault("failed", []).append(task)
                        released_count += 1
                    else:
                        in_progress_to_keep.append(task)
                except (ValueError, TypeError):
                    in_progress_to_keep.append(task)

            if not dry_run:
                queue["in_progress"] = in_progress_to_keep

        if not dry_run and (cleaned_count > 0 or released_count > 0):
            save_queue(queue)

    return {
        "success": True,
        "cleaned_count": cleaned_count,
        "released_count": released_count,
        "stale_tasks": stale_tasks,
        "max_age_hours": max_age_hours,
        "archive": archive,
        "dry_run": dry_run,
    }


# ── P26: Employee Initiative Functions ──────────────────────────────────────


def submit_proposal(
    title: str,
    proposer_id: str,
    description: str = "",
    priority: int = 3,
    required_capabilities: list[str] | None = None,
    estimated_complexity: str = "standard",
    proposal_type: str = "improvement",
    project_id: str | None = None,
) -> dict:
    """
    Submit a task proposal from an employee.

    Proposals are added to the "proposed" queue and require manager approval
    before being moved to "pending" for execution.

    Args:
        title: Proposal title
        proposer_id: ID of the employee proposing the task
        description: Detailed description of the proposed work
        priority: Suggested priority (1-4, manager may adjust)
        required_capabilities: Suggested capabilities needed
        estimated_complexity: trivial|standard|complex|epic
        proposal_type: Type of proposal (todo, improvement, follow_up, research)
        project_id: Project ID (auto-detected if None)

    Returns:
        Dict with success status and proposal details
    """
    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Auto-detect project_id if not provided
        if project_id is None:
            project_id = get_current_project_id()

        # Check for duplicates (including in proposed queue)
        duplicate = find_duplicate_task(
            queue=queue,
            title=title,
            check_completed=True,
            max_completed_age_hours=RECENT_COMPLETION_HOURS,
        )

        # Also check proposed queue
        if not duplicate:
            for existing in queue.get("proposed", []):
                similarity = calculate_token_similarity(
                    title, existing.get("title", "")
                )
                if similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                    duplicate = {
                        "task_id": existing.get("task_id"),
                        "title": existing.get("title"),
                        "status": "proposed",
                        "similarity": round(similarity, 2),
                        "match_type": "existing_proposal",
                    }
                    break

        if duplicate:
            return {
                "success": False,
                "error": "duplicate_proposal",
                "message": f"Similar proposal already exists: {duplicate['title']}",
                "existing_task_id": duplicate.get("task_id"),
                "similarity": duplicate.get("similarity"),
            }

        # Validate and sanitize capabilities
        validated_capabilities = sanitize_capabilities(required_capabilities)

        # Create the proposal
        proposal_id = generate_task_id()
        proposal = {
            "task_id": proposal_id,
            "title": title,
            "description": description,
            "priority": max(1, min(4, priority)),
            "required_capabilities": validated_capabilities,
            "estimated_complexity": estimated_complexity,
            "project_id": project_id,
            "source": "self",  # Employee initiative
            "proposed_at": now,
            "proposed_by": proposer_id,
            "proposal_type": proposal_type,
            "status": "proposed",
            "review_notes": None,
            "approved_by": None,
            "approved_at": None,
            "rejected_by": None,
            "rejected_at": None,
            "rejection_reason": None,
        }

        # Phase 1 (autonomy): task-admission gate. Catch redundant / invalid /
        # already-done proposals before they reach the proposed queue (approved
        # proposals move straight to pending, bypassing add_task's backstop).
        admitted, admit_reason = _run_admission_gate(proposal)
        if not admitted:
            return {
                "success": False,
                "error": "admission_rejected",
                "reason": admit_reason,
                "title": title,
            }

        # Ensure proposed queue exists
        if "proposed" not in queue:
            queue["proposed"] = []

        queue["proposed"].append(proposal)
        save_queue(queue)

    return {
        "success": True,
        "task_id": proposal_id,
        "status": "proposed",
        "proposer": proposer_id,
        "message": f"Proposal submitted: {title}",
    }


def approve_proposal(
    task_id: str,
    reviewer_id: str,
    priority: int | None = None,
    notes: str | None = None,
) -> dict:
    """
    Approve a proposal and move it to pending queue.

    Args:
        task_id: The proposal task ID
        reviewer_id: ID of the manager/reviewer approving
        priority: Optional adjusted priority (uses proposer's if None)
        notes: Optional review notes

    Returns:
        Dict with success status
    """
    # Input validation
    if not task_id or not str(task_id).strip():
        return {
            "success": False,
            "reason": "invalid_task_id",
            "message": "task_id is required",
        }
    if not reviewer_id or not str(reviewer_id).strip():
        return {
            "success": False,
            "reason": "invalid_reviewer_id",
            "message": "reviewer_id is required",
        }

    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Find the proposal (defensive: use .get() for malformed entries)
        proposal = None
        for i, p in enumerate(queue.get("proposed", [])):
            if p.get("task_id") == task_id:
                proposal = p
                queue["proposed"].pop(i)
                break

        if not proposal:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Proposal {task_id} not found in proposed queue",
            }

        # Update proposal with approval info
        proposal["approved_by"] = reviewer_id
        proposal["approved_at"] = now
        proposal["status"] = "pending"

        # Validate priority is numeric before clamping
        if priority is not None:
            try:
                priority = int(priority)
                proposal["priority"] = max(1, min(4, priority))
            except (TypeError, ValueError):
                pass  # Invalid priority type, keep original

        if notes:
            proposal["review_notes"] = notes

        # Move to pending
        queue["pending"].append(proposal)
        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "previous_status": "proposed",
        "new_status": "pending",
        "approved_by": reviewer_id,
        "message": f"Proposal approved: {proposal['title']}",
    }


def reject_proposal(
    task_id: str,
    reviewer_id: str,
    reason: str,
) -> dict:
    """
    Reject a proposal with feedback.

    Rejected proposals are archived to completed with rejection metadata
    for learning purposes.

    Args:
        task_id: The proposal task ID
        reviewer_id: ID of the manager/reviewer rejecting
        reason: Reason for rejection (feedback for proposer)

    Returns:
        Dict with success status
    """
    # Input validation
    if not task_id or not str(task_id).strip():
        return {
            "success": False,
            "reason": "invalid_task_id",
            "message": "task_id is required",
        }
    if not reviewer_id or not str(reviewer_id).strip():
        return {
            "success": False,
            "reason": "invalid_reviewer_id",
            "message": "reviewer_id is required",
        }
    if not reason or not str(reason).strip():
        return {
            "success": False,
            "reason": "invalid_reason",
            "message": "reason is required",
        }

    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Find the proposal (defensive: use .get() for malformed entries)
        proposal = None
        for i, p in enumerate(queue.get("proposed", [])):
            if p.get("task_id") == task_id:
                proposal = p
                queue["proposed"].pop(i)
                break

        if not proposal:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Proposal {task_id} not found in proposed queue",
            }

        # Update proposal with rejection info
        proposal["rejected_by"] = reviewer_id
        proposal["rejected_at"] = now
        proposal["rejection_reason"] = reason
        proposal["status"] = "rejected"
        proposal["completed_at"] = now

        # Archive to completed for learning
        queue["completed"].append(proposal)
        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "previous_status": "proposed",
        "new_status": "rejected",
        "rejected_by": reviewer_id,
        "reason": reason,
        "message": f"Proposal rejected: {proposal['title']}",
    }


def submit_for_review(
    task_id: str,
) -> dict:
    """
    Submit a completed task for manager review.

    Moves the task from in_progress to review queue.

    Args:
        task_id: The task ID to submit for review

    Returns:
        Dict with success status
    """
    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Find the task in in_progress
        task = None
        for i, t in enumerate(queue.get("in_progress", [])):
            if t["task_id"] == task_id:
                task = t
                queue["in_progress"].pop(i)
                break

        if not task:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Task {task_id} not found in in_progress queue",
            }

        # Update task
        task["review_requested_at"] = now
        task["status"] = "review"

        # Ensure review queue exists
        if "review" not in queue:
            queue["review"] = []

        queue["review"].append(task)
        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "previous_status": "in_progress",
        "new_status": "review",
        "message": f"Task submitted for review: {task['title']}",
    }


def complete_review(
    task_id: str,
    reviewer_id: str,
    feedback: str | None = None,
    quality_score: float | None = None,
) -> dict:
    """
    Complete review and move task to completed.

    Args:
        task_id: The task ID being reviewed
        reviewer_id: ID of the manager/reviewer
        feedback: Optional feedback for the employee
        quality_score: Optional quality score (0.0-1.0)

    Returns:
        Dict with success status
    """
    # Input validation
    if not task_id or not str(task_id).strip():
        return {
            "success": False,
            "reason": "invalid_task_id",
            "message": "task_id is required",
        }
    if not reviewer_id or not str(reviewer_id).strip():
        return {
            "success": False,
            "reason": "invalid_reviewer_id",
            "message": "reviewer_id is required",
        }

    with QueueLock(get_lock_path()):
        queue = load_queue()
        now = datetime.now(timezone.utc).isoformat()

        # Find the task in review (defensive: use .get() for malformed entries)
        task = None
        for i, t in enumerate(queue.get("review", [])):
            if t.get("task_id") == task_id:
                task = t
                queue["review"].pop(i)
                break

        if not task:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Task {task_id} not found in review queue",
            }

        # Update task with review info
        task["reviewed_by"] = reviewer_id
        task["reviewed_at"] = now
        task["completed_at"] = now
        task["status"] = "completed"

        if feedback:
            task["review_feedback"] = feedback

        # Validate quality_score is numeric before clamping
        if quality_score is not None:
            try:
                quality_score = float(quality_score)
                task["quality_score"] = max(0.0, min(1.0, quality_score))
            except (TypeError, ValueError):
                pass  # Invalid quality_score type, ignore

        queue["completed"].append(task)
        save_queue(queue)

    return {
        "success": True,
        "task_id": task_id,
        "previous_status": "review",
        "new_status": "completed",
        "reviewed_by": reviewer_id,
        "message": f"Review completed: {task['title']}",
    }


def list_proposals(
    proposer_id: str | None = None,
    proposal_type: str | None = None,
    project_id: str | None = None,
) -> dict:
    """
    List proposals in the proposed queue.

    Args:
        proposer_id: Filter by proposer
        proposal_type: Filter by type (todo, improvement, follow_up, research)
        project_id: Filter by project (auto-detected if None)

    Returns:
        Dict with proposals
    """
    if project_id is None:
        project_id = get_current_project_id()

    queue = load_queue()
    proposals = queue.get("proposed", [])

    # Filter
    filtered = []
    for p in proposals:
        if project_id and p.get("project_id") != project_id:
            continue
        if proposer_id and p.get("proposed_by") != proposer_id:
            continue
        if proposal_type and p.get("proposal_type") != proposal_type:
            continue
        filtered.append(p)

    return {
        "success": True,
        "proposals": filtered,
        "count": len(filtered),
        "project_id": project_id,
    }


def list_reviews(
    project_id: str | None = None,
) -> dict:
    """
    List tasks awaiting review.

    Args:
        project_id: Filter by project (auto-detected if None)

    Returns:
        Dict with tasks in review
    """
    if project_id is None:
        project_id = get_current_project_id()

    queue = load_queue()
    reviews = queue.get("review", [])

    # Filter by project
    if project_id:
        reviews = [r for r in reviews if r.get("project_id") == project_id]

    return {
        "success": True,
        "reviews": reviews,
        "count": len(reviews),
        "project_id": project_id,
    }


def queue_health(
    queue_path: Path | None = None, stuck_threshold_hours: float = 2.0
) -> dict:
    """
    Return read-only observability metrics for the work queue.

    Args:
        queue_path: Optional explicit path to work_queue.json.
                    If None, uses the default resolution via get_queue_path().
        stuck_threshold_hours: Hours after which an in_progress task is considered stuck.

    Returns:
        Dict with queue health metrics including counts per status,
        oldest pending task age, stale count, stuck task count, and orphan count.
    """
    now = datetime.now(timezone.utc)

    # Load queue — use explicit path or default
    if queue_path is not None:
        if queue_path.exists():
            try:
                with open(queue_path, "r", encoding="utf-8") as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, OSError):
                queue = get_empty_queue()
        else:
            queue = get_empty_queue()
    else:
        queue = load_queue()

    pending = queue.get("pending", [])
    in_progress = queue.get("in_progress", [])
    blocked = queue.get("blocked", [])
    completed = queue.get("completed", [])

    # Compute ages for pending tasks
    oldest_pending_age_hours: float = 0.0
    stale_count = 0
    stale_threshold_seconds = 24 * 3600  # 24 hours

    for task in pending:
        created_at_str = task.get("created_at")
        if not created_at_str:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_seconds = (now - created_at).total_seconds()
            age_hours = age_seconds / 3600.0

            if age_hours > oldest_pending_age_hours:
                oldest_pending_age_hours = age_hours

            if age_seconds > stale_threshold_seconds:
                stale_count += 1
        except (ValueError, TypeError):
            # Malformed timestamp — skip gracefully
            continue

    # Detect stuck in_progress tasks: claimed but not completed within threshold
    stuck_threshold_seconds = stuck_threshold_hours * 3600
    stuck_count = 0
    oldest_in_progress_age_hours: float = 0.0

    for task in in_progress:
        started_at_str = task.get("started_at") or task.get("assigned_at")
        if not started_at_str:
            continue
        try:
            started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
            age_seconds = (now - started_at).total_seconds()
            age_hours = age_seconds / 3600.0

            if age_hours > oldest_in_progress_age_hours:
                oldest_in_progress_age_hours = age_hours

            if age_seconds > stuck_threshold_seconds:
                stuck_count += 1
        except (ValueError, TypeError):
            continue

    return {
        "pending_count": len(pending),
        "in_progress_count": len(in_progress),
        "blocked_count": len(blocked),
        "completed_count": len(completed),
        "oldest_pending_age_hours": round(oldest_pending_age_hours, 2),
        "stale_count": stale_count,
        "stuck_count": stuck_count,
        "oldest_in_progress_age_hours": round(oldest_in_progress_age_hours, 2),
        "orphan_count": len(in_progress),
        "timestamp": now.isoformat(),
    }


def print_help():
    """Print usage help."""
    help_text = """
Pull-Based Work Allocator

Commands:
    add             Add a new task to the queue
    pull            Pull next available task for an agent
    update          Update task status or metadata
    list            List tasks (optionally filtered by status)
    get             Get a specific task by ID
    workload        Check agent workload
    release         Release a task back to pending
    route           Route a task to a different project (cross-project)
    cross-project   List all cross-project tasks
    cleanup         Remove stale tasks (>24h old by default)

Add options:
    --title TEXT              Task title (required)
    --priority 1-4            Priority (1=Critical, 4=Low, default=3)
    --department ID           Department ID
    --capabilities LIST       Comma-separated required capabilities
    --deadline ISO            Deadline timestamp
    --complexity STR          trivial|standard|complex|epic (default=standard)
    --dependencies LIST       Comma-separated task IDs
    --description TEXT        Task description
    --project-id ID           Project ID (auto-detected if not specified)
    --source STR              Task origin: human|self|escalation|planning (default=self)
    --source-project-id ID    Original project where task was created (for cross-project)
    --allowed-projects LIST   Comma-separated project IDs that can access this task
    --dry-run                 Preview routing and admission without writing to queue

Pull options:
    --agent-id ID         Agent ID (required)
    --capabilities LIST   Comma-separated agent capabilities
    --department ID       Optional department filter
    --project-id ID       Filter by project (auto-detected if not specified)
    --all-projects        Pull from all projects (ignore project filter)
    --no-efficiency       Disable efficiency-aware routing (G6 Economics)

Update options:
    --task-id ID          Task ID (required)
    --status STATUS       pending|in_progress|blocked|completed
    --progress TEXT       Progress notes
    --notes TEXT          Additional notes

List options:
    --status STATUS       Filter by status
    --project-id ID       Filter by project (auto-detected if not specified)
    --all-projects        List from all projects

Get options:
    --task-id ID          Task ID (required)

Workload options:
    --agent-id ID         Agent ID (required)

Release options:
    --task-id ID          Task ID (required)
    --agent-id ID         Agent ID (required)

Route options (cross-project task routing):
    --task-id ID              Task ID to route (required)
    --target-project-id ID    Target project ID (required)
    --employee-id ID          Employee requesting route (required, for access validation)
    --reason TEXT             Reason for routing (required)

Examples:
    # Preview routing/admission without writing to queue
    python work_allocator.py add --title "Fix critical bug" --dry-run

    # Add a high-priority task
    python work_allocator.py add --title "Fix critical bug" --priority 1

    # Add a cross-project task with access restrictions
    python work_allocator.py add --title "Shared component" --allowed-projects "proj-a,proj-b"

    # Agent pulls next task
    python work_allocator.py pull --agent-id agent-001 --capabilities "code,debug"

    # Mark task as completed
    python work_allocator.py update --task-id task-123 --status completed

    # List pending tasks
    python work_allocator.py list --status pending

    # Route a task to another project
    python work_allocator.py route --task-id task-123 --target-project-id proj-b --employee-id emp-001 --reason "Requires backend expertise"

    # List all cross-project tasks
    python work_allocator.py cross-project
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "add":
            if "title" not in args:
                print("Error: --title required")
                sys.exit(1)

            capabilities = None
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]

            dependencies = None
            if "dependencies" in args:
                dependencies = [d.strip() for d in args["dependencies"].split(",")]

            allowed_projects = None
            if "allowed_projects" in args:
                allowed_projects = [
                    p.strip() for p in args["allowed_projects"].split(",")
                ]

            if args.get("dry_run"):
                task = create_task(
                    title=args["title"],
                    priority=int(args.get("priority", 3)),
                    department=args.get("department"),
                    required_capabilities=capabilities,
                    deadline=args.get("deadline"),
                    estimated_complexity=args.get("complexity", "standard"),
                    dependencies=dependencies,
                    description=args.get("description", ""),
                    project_id=args.get("project_id"),
                    source=args.get("source", "self"),
                    source_project_id=args.get("source_project_id"),
                    allowed_projects=allowed_projects,
                )
                admission_check_ran = True
                try:
                    ta = _import_task_admission()
                    if ta is not None:
                        repo_root = get_company_dir().parent
                        config = ta.load_admission_config(repo_root)
                        admitted, admit_reason = ta.admit_task(
                            task, repo_root=repo_root, config=config
                        )
                    else:
                        admitted, admit_reason = True, None
                        admission_check_ran = False
                except Exception:
                    admitted, admit_reason = True, None
                    admission_check_ran = False
                admission: dict = {"admitted": admitted, "reason": admit_reason}
                if not admission_check_ran:
                    admission["warning"] = (
                        "admission gate unavailable; result is fail-open"
                    )
                preview = {
                    "dry_run": True,
                    "title": task["title"],
                    "department": task["department"],
                    "priority": task["priority"],
                    "estimated_complexity": task["estimated_complexity"],
                    "source": task["source"],
                    "required_capabilities": task["required_capabilities"],
                    "requires_deliverable": task["requires_deliverable"],
                    "admission": admission,
                }
                print(json.dumps(preview, indent=2))
                return

            result = add_task(
                title=args["title"],
                priority=int(args.get("priority", 3)),
                department=args.get("department"),
                required_capabilities=capabilities,
                deadline=args.get("deadline"),
                estimated_complexity=args.get("complexity", "standard"),
                dependencies=dependencies,
                description=args.get("description", ""),
                project_id=args.get("project_id"),
                source=args.get("source", "self"),
                source_project_id=args.get("source_project_id"),
                allowed_projects=allowed_projects,
            )
            print(json.dumps(result, indent=2))

        elif command == "pull":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)

            capabilities = []
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]

            # Efficiency routing is enabled by default, --no-efficiency disables it
            use_efficiency = args.get("no_efficiency", False) is not True

            result = pull_next_task(
                agent_id=args["agent_id"],
                capabilities=capabilities,
                department=args.get("department"),
                project_id=args.get("project_id"),
                all_projects=args.get("all_projects", False) is True,
                use_efficiency_routing=use_efficiency,
            )
            print(json.dumps(result, indent=2))

        elif command == "update":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = update_task(
                task_id=args["task_id"],
                status=args.get("status"),
                progress=args.get("progress"),
                notes=args.get("notes"),
            )
            print(json.dumps(result, indent=2))

        elif command == "get":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = get_task(args["task_id"])
            print(json.dumps(result, indent=2))

        elif command == "list":
            result = list_tasks(
                status=args.get("status"),
                project_id=args.get("project_id"),
                all_projects=args.get("all_projects", False) is True,
            )
            print(json.dumps(result, indent=2))

        elif command == "workload":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)

            result = get_workload(args["agent_id"])
            print(json.dumps(result, indent=2))

        elif command == "release":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)

            result = release_task(args["task_id"], args["agent_id"])
            print(json.dumps(result, indent=2))

        elif command == "route":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "target_project_id" not in args:
                print("Error: --target-project-id required")
                sys.exit(1)
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            if "reason" not in args:
                print("Error: --reason required")
                sys.exit(1)

            result = route_task(
                task_id=args["task_id"],
                target_project_id=args["target_project_id"],
                employee_id=args["employee_id"],
                reason=args["reason"],
            )
            print(json.dumps(result, indent=2))

        elif command == "cross-project":
            tasks = get_cross_project_tasks()
            print(
                json.dumps(
                    {
                        "success": True,
                        "cross_project_tasks": tasks,
                        "count": len(tasks),
                    },
                    indent=2,
                )
            )

        elif command == "cleanup":
            # P22 Fix 2: Stale task cleanup
            max_age = float(args.get("max_age_hours", 24.0))
            archive = args.get("delete", False) is not True
            dry_run = args.get("dry_run", False) is True

            result = cleanup_stale_tasks(
                max_age_hours=max_age,
                archive=archive,
                dry_run=dry_run,
            )
            print(json.dumps(result, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except TimeoutError as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
