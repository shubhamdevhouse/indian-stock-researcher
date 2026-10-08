"""Pluggable user prompts used by tools (the CLI registers an interactive implementation)."""
from collections.abc import Callable

from . import config

# prompter(info) -> chosen count, or 0 to cancel
Prompter = Callable[[dict], int]


def default_prompter(info: dict) -> int:
    return min(config.DEFAULT_TOP_N, info["total"])


_prompter: Prompter = default_prompter
last_plan: dict | None = None  # result of the most recent plan_analysis call (read by the CLI to save reports)


def set_prompter(fn: Prompter | None) -> None:
    global _prompter
    _prompter = fn or default_prompter


def ask_count(info: dict) -> int:
    n = int(_prompter(info))
    return max(0, min(n, info["total"]))
