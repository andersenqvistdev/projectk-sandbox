#!/usr/bin/env python3
"""Execution-provider adapters for Forge employee workers.

The daemon owns task orchestration, worktree isolation, timeouts, and
deliverable verification.  Providers only translate a neutral worker request
to a concrete coding-agent CLI and normalize that CLI's output.

Provider selection lives in forge-config.json:

    "execution": {
      "defaultProvider": "claude-subscription",
      "providers": {
        "claude-subscription": {"type": "claude", "auth": "subscription",
                                "models": {"trivial": "...", "epic": "..."}},
        "codex-subscription":  {"type": "codex", "auth": "subscription",
                                "sandbox": "workspace-write",
                                "reasoningEffort": "low", "models": {...}},
        "claude-ollama":       {"type": "claude", "auth": "ollama",
                                "models": {"standard": "<ollama-model>"}}
      }
    }

Projects without an ``execution`` section keep the historical Claude
behavior (claude + subscription), making the migration backward compatible.

Codex has no equivalent of --allowedTools/--disallowedTools — its sandbox
(workspace-write / read-only) is the control surface, and the system
reinforcement travels inside the prompt via prepare_prompt() instead of a
system-prompt flag.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

SUPPORTED_PROVIDER_TYPES = {"claude", "codex"}

# Mirrors the historical effort mapping in employee_activator (WS-121).
EFFORT_BY_COMPLEXITY = {
    "trivial": "high",
    "standard": "high",
    "complex": "high",
    "epic": "max",
}


@dataclass(frozen=True)
class ProviderSpec:
    """Resolved provider configuration for one worker invocation."""

    name: str
    provider_type: str
    auth: str
    model: str | None
    sandbox: str = "workspace-write"
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a provider preflight check."""

    healthy: bool
    message: str


def resolve_provider(
    config: dict[str, Any], complexity: str, fallback_model: str | None = None
) -> ProviderSpec:
    """Resolve the configured provider and complexity-specific model.

    Projects without the new ``execution`` section retain the historical
    Claude behavior, making the configuration migration backward compatible.
    """

    execution = config.get("execution", {}) or {}
    name = execution.get("defaultProvider", "claude-subscription")
    providers = execution.get("providers", {}) or {}
    provider = providers.get(name)

    if provider is None:
        if execution:
            raise ValueError(f"execution provider {name!r} is not configured")
        provider = {"type": "claude", "auth": "subscription"}

    provider_type = str(provider.get("type", "")).lower()
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        raise ValueError(
            f"unsupported execution provider type {provider_type!r}; "
            f"expected one of {sorted(SUPPORTED_PROVIDER_TYPES)}"
        )

    models = provider.get("models", {}) or {}
    model = (
        models.get(complexity)
        or models.get("standard")
        or provider.get("model")
        or fallback_model
    )
    return ProviderSpec(
        name=name,
        provider_type=provider_type,
        auth=str(provider.get("auth", "subscription")),
        model=str(model) if model else None,
        sandbox=str(provider.get("sandbox", "workspace-write")),
        reasoning_effort=(
            str(provider["reasoningEffort"])
            if provider.get("reasoningEffort")
            else None
        ),
    )


def check_provider_health(spec: ProviderSpec, timeout: int = 15) -> ProviderHealth:
    """Fail fast when a worker CLI or its subscription login is unavailable."""

    if spec.provider_type == "claude" and spec.auth == "ollama":
        if shutil.which("ollama") is None:
            return ProviderHealth(False, "Ollama CLI is not installed or not on PATH")
        if shutil.which("claude") is None:
            return ProviderHealth(False, "Claude CLI is not installed or not on PATH")
        assert_spawn_allowed("agent_providers.check_provider_health", subprocess.run)
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=prepare_environment(spec),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProviderHealth(False, f"Ollama preflight failed: {exc}")
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".strip()
            return ProviderHealth(False, detail or "Ollama server is not reachable")
        installed = {
            line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()
        }
        requested_names = {spec.model, f"{spec.model}:latest"} if spec.model else set()
        if spec.model and not requested_names.intersection(installed):
            return ProviderHealth(
                False,
                f"Ollama model {spec.model!r} is not installed",
            )
        return ProviderHealth(True, f"{spec.name} is ready")

    executable = "claude" if spec.provider_type == "claude" else "codex"
    if shutil.which(executable) is None:
        return ProviderHealth(
            False, f"{executable} CLI is not installed or not on PATH"
        )

    if spec.auth != "subscription":
        return ProviderHealth(
            False,
            f"provider {spec.name!r} uses unsupported auth mode {spec.auth!r}; "
            "first release supports subscription auth only",
        )

    command = (
        ["claude", "auth", "status"]
        if spec.provider_type == "claude"
        else ["codex", "login", "status"]
    )
    assert_spawn_allowed("agent_providers.check_provider_health", subprocess.run)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=prepare_environment(spec),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProviderHealth(False, f"{spec.name} preflight failed: {exc}")

    combined = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        return ProviderHealth(False, combined or f"{spec.name} is not authenticated")
    if spec.provider_type == "claude":
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            status = {}
        if not status.get("loggedIn", False):
            return ProviderHealth(False, "Claude Code subscription is not logged in")
    return ProviderHealth(True, f"{spec.name} is ready")


def prepare_environment(spec: ProviderSpec) -> dict[str, str]:
    """Prepare a worker environment without leaking nested-agent markers.

    P71 lesson: start from the FULL environment — the CLIs need auth vars we
    cannot enumerate, so only known-problematic prefixes are stripped.  The
    API-key strip below is a deliberate, auth-mode-conditional exception to
    P71: under subscription auth a stray ANTHROPIC_API_KEY / OPENAI_API_KEY
    would silently override the OAuth login and bill the API instead.
    """

    env = dict(os.environ)
    prefixes = ["UV_", "VIRTUAL_ENV"]
    if spec.provider_type == "claude":
        prefixes.extend(["CLAUDECODE", "CLAUDE_CODE_"])
        if spec.auth == "subscription":
            prefixes.extend(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
    else:
        prefixes.extend(["CODEX_THREAD_ID", "CODEX_INTERNAL_"])
        if spec.auth == "subscription":
            prefixes.extend(["OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"])

    for key in list(env):
        if any(key.startswith(prefix) for prefix in prefixes):
            del env[key]

    uv_prefix = str(Path.home() / ".cache" / "uv" / "environments")
    env["PATH"] = (
        os.pathsep.join(
            part
            for part in env.get("PATH", "").split(os.pathsep)
            if part
            and not part.startswith(uv_prefix)
            and "virtualenv" not in part.lower()
        )
        or "/usr/bin:/bin"
    )
    env["TERM"] = "xterm-256color"
    env["LANG"] = "en_US.UTF-8"
    # Worker marker — forge_daemon's CLI refuses stop/start/restart when set
    # (prevents workers from unloading the LaunchAgent). Not stripped, so the
    # marker propagates to child subprocesses.
    env["FORGE_WORKER_CONTEXT"] = "1"
    return {k: v for k, v in env.items() if v}


def prepare_prompt(
    spec: ProviderSpec, prompt: str, system_reinforcement: str | None
) -> str:
    """Fold the system reinforcement into the prompt for providers that lack
    a system-prompt flag (codex).  Claude gets it via --append-system-prompt
    in build_command, so the prompt passes through unchanged."""

    if spec.provider_type == "codex" and system_reinforcement:
        return f"{system_reinforcement}\n\n{prompt}"
    return prompt


def build_command(
    spec: ProviderSpec,
    *,
    project_root: Path,
    complexity: str,
    allowed_tools: list[str],
    disallowed_tools: list[str],
    system_reinforcement: str,
) -> list[str]:
    """Build a non-interactive command for the selected coding agent.

    The prompt is always delivered via stdin (codex takes ``-``; claude runs
    in full agent mode and reads stdin per WS-099-003)."""

    if spec.provider_type == "codex":
        command = ["codex"]
        if spec.reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{spec.reasoning_effort}"'])
        command.extend(
            [
                "--ask-for-approval",
                "never",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                spec.sandbox,
                "--cd",
                str(project_root),
            ]
        )
        if spec.model and spec.model != "default":
            command.extend(["--model", spec.model])
        command.append("-")
        return command

    effort = EFFORT_BY_COMPLEXITY.get(complexity, "high")
    claude_flags = [
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--effort",
        effort,
        "--append-system-prompt",
        system_reinforcement,
    ]
    if allowed_tools:
        claude_flags.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools:
        claude_flags.extend(["--disallowedTools", ",".join(disallowed_tools)])

    if spec.auth == "ollama":
        # ``ollama launch claude -- <flags>`` passes everything after ``--``
        # straight to Claude Code: flags only, never the binary name.  The
        # --model before ``--`` belongs to ollama (it serves the local model).
        return [
            "ollama",
            "launch",
            "claude",
            "--yes",
            "--model",
            spec.model or "",
            "--",
            *claude_flags,
        ]

    return [
        "uv",
        "run",
        "claude",
        "--model",
        spec.model or "claude-sonnet-5",
        *claude_flags,
    ]


def normalize_output(spec: ProviderSpec, stdout: str) -> str:
    """Convert provider-specific machine output to useful task output."""

    if spec.provider_type != "codex":
        return stdout

    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return "\n\n".join(messages) or stdout
