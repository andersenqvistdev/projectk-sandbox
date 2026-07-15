#!/usr/bin/env python3
"""model_profile_switcher.py — Model profile switching logic with worker-count sync.

Invoked by bin/forge-model-profile and testable as a module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_CONFIG_PATH = Path("forge-config.json")


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_config(config_path: Path, config: dict) -> None:
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def get_profiles(config: dict) -> dict:
    return config.get("modelProfiles", {}).get("profiles", {})


def get_current_profile(config: dict) -> str:
    return config.get("modelProfile", "")


def get_max_parallel_workers(config: dict) -> Optional[int]:
    return config.get("adaptiveScheduler", {}).get("maxParallelWorkers")


# ---------------------------------------------------------------------------
# Mismatch detection and sync
# ---------------------------------------------------------------------------


def check_worker_mismatch(
    config: dict, profile_name: str
) -> Tuple[bool, Optional[int], Optional[int]]:
    """Return (is_mismatch, profile_workers, current_max_workers).

    is_mismatch is False when the profile has no 'workers' field (not applicable).
    """
    profile = get_profiles(config).get(profile_name, {})
    profile_workers: Optional[int] = profile.get("workers")
    if profile_workers is None:
        return False, None, None
    current_workers = get_max_parallel_workers(config)
    return current_workers != profile_workers, profile_workers, current_workers


def sync_workers(config: dict, profile_name: str) -> Optional[int]:
    """Set adaptiveScheduler.maxParallelWorkers from profile's workers field.

    Returns the new value, or None if the profile has no workers field.
    """
    profile = get_profiles(config).get(profile_name, {})
    profile_workers: Optional[int] = profile.get("workers")
    if profile_workers is None:
        return None
    if "adaptiveScheduler" not in config:
        config["adaptiveScheduler"] = {}
    config["adaptiveScheduler"]["maxParallelWorkers"] = profile_workers
    return profile_workers


def switch_profile(config: dict, profile_name: str) -> None:
    config["modelProfile"] = profile_name


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _short_model(model: str) -> str:
    if "opus" in model:
        return "Opus"
    if "sonnet" in model:
        return "Sonnet"
    if "haiku" in model:
        return "Haiku"
    return model


_SEP = "=" * 65


def display_profiles(config: dict) -> None:
    current = get_current_profile(config)
    profiles = get_profiles(config)
    current_workers = get_max_parallel_workers(config)

    print(_SEP)
    print(" MODEL PROFILES                                    [forge-config]")
    print(_SEP)
    print(f" Active: {current}")
    if current_workers is not None:
        print(f" adaptiveScheduler.maxParallelWorkers: {current_workers}")
    print(_SEP)
    print()
    print(
        f"{'':1}{'Profile':<13} {'Workers':>7}  {'Trivial':<7} {'Standard':<8}"
        f" {'Complex':<7} {'Epic':<7} {'Executive':<9} Subscription"
    )
    print("-" * 90)
    for name, prof in profiles.items():
        marker = ">" if name == current else " "
        workers = str(prof.get("workers", "—"))
        trivial = _short_model(prof.get("trivial", ""))
        standard = _short_model(prof.get("standard", ""))
        complex_ = _short_model(prof.get("complex", ""))
        epic = _short_model(prof.get("epic", ""))
        executive = _short_model(prof.get("executive", ""))
        sub = prof.get("subscription", "")
        print(
            f"{marker} {name:<13} {workers:>7}  {trivial:<7} {standard:<8}"
            f" {complex_:<7} {epic:<7} {executive:<9} {sub}"
        )
    print()
    print("To switch:             /model-profile <name>")
    print("To switch+sync workers: /model-profile <name> --sync-workers")
    print(_SEP)


def display_switch_result(
    old_profile: str,
    new_profile: str,
    profiles: dict,
    mismatch: bool,
    profile_workers: Optional[int],
    current_workers: Optional[int],
    synced_workers: Optional[int],
) -> None:
    print(_SEP)
    print(" MODEL PROFILE SWITCHED                              [success]")
    print(_SEP)
    print(f" Previous: {old_profile}")
    print(f" Active:   {new_profile}")
    prof = profiles.get(new_profile, {})
    print(f" Description: {prof.get('description', '')}")
    if synced_workers is not None:
        print(f" adaptiveScheduler.maxParallelWorkers: {synced_workers} (synced)")
    elif mismatch and profile_workers is not None:
        print(
            f" adaptiveScheduler.maxParallelWorkers: {current_workers} "
            f"(unchanged — profile wants {profile_workers}, use --sync-workers)"
        )
    print(_SEP)
    print()
    print("| Complexity | Model    |")
    print("|------------|----------|")
    for tier in ("trivial", "standard", "complex", "epic", "executive"):
        model_str = _short_model(prof.get(tier, ""))
        print(f"| {tier:<10} | {model_str:<8} |")
    print()
    print("Takes effect on next daemon task (no restart needed).")
    print(_SEP)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge-model-profile",
        description="Switch the active Forge model profile.",
    )
    parser.add_argument("profile", nargs="?", help="Profile name to switch to")
    parser.add_argument("--list", action="store_true", help="List all profiles")
    parser.add_argument(
        "--sync-workers",
        action="store_true",
        help=(
            "Set adaptiveScheduler.maxParallelWorkers to the selected profile's "
            "'workers' field. Without this flag, a mismatch is warned but not fixed."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help="Path to forge-config.json (default: forge-config.json)",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config)

    if not config_path.exists():
        print(f"Error: config not found: {config_path}", file=sys.stderr)
        return 1

    config = load_config(config_path)

    # Display-only mode
    if args.list or args.profile is None:
        display_profiles(config)
        return 0

    profile_name: str = args.profile
    profiles = get_profiles(config)

    if profile_name not in profiles:
        available = ", ".join(profiles.keys())
        print(
            f'Profile "{profile_name}" not found.\n\nAvailable profiles: {available}',
            file=sys.stderr,
        )
        return 1

    old_profile = get_current_profile(config)
    mismatch, profile_workers, current_workers = check_worker_mismatch(
        config, profile_name
    )

    # Warn on mismatch when --sync-workers is not given
    if mismatch and not args.sync_workers:
        print(
            f"Warning: profile '{profile_name}' specifies workers={profile_workers} but "
            f"adaptiveScheduler.maxParallelWorkers={current_workers}. "
            f"Use --sync-workers to align them.",
            file=sys.stderr,
        )

    switch_profile(config, profile_name)

    synced_workers: Optional[int] = None
    if args.sync_workers:
        synced_workers = sync_workers(config, profile_name)

    save_config(config_path, config)

    display_switch_result(
        old_profile,
        profile_name,
        profiles,
        mismatch,
        profile_workers,
        current_workers,
        synced_workers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
