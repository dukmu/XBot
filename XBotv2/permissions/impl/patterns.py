"""Bounded permission regexes; failures must never mean a nonmatching deny."""

from functools import lru_cache
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time

import regex

MAX_PATTERN_CHARS = 4096
MAX_VALUE_CHARS = 1_048_576
MATCH_TIMEOUT_SECONDS = 0.01
MATCH_BUDGET_SECONDS = 0.05
MAX_MATCHES = 1024
_ACTIVE_BUDGET: ContextVar["MatchBudget | None"] = ContextVar(
    "permission_match_budget", default=None,
)


@dataclass(slots=True)
class MatchBudget:
    deadline: float = field(default_factory=lambda: time.monotonic() + MATCH_BUDGET_SECONDS)
    remaining: int = MAX_MATCHES

    def timeout(self) -> float:
        self.remaining -= 1
        remaining_time = self.deadline - time.monotonic()
        if self.remaining < 0 or remaining_time <= 0:
            raise ValueError("Permission policy exceeded aggregate matching budget")
        return min(MATCH_TIMEOUT_SECONDS, remaining_time)


@contextmanager
def matching_budget():
    current = _ACTIVE_BUDGET.get()
    if current is not None:
        yield
        return
    token = _ACTIVE_BUDGET.set(MatchBudget())
    try:
        yield
    finally:
        _ACTIVE_BUDGET.reset(token)


@lru_cache(maxsize=512)
def compile_pattern(pattern: str) -> regex.Pattern:
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError("Permission pattern exceeds 4096 characters")
    try:
        return regex.compile(pattern, regex.VERSION0)
    except (regex.error, RecursionError) as exc:
        raise ValueError("Invalid permission regular expression") from exc


def fullmatch(pattern: str, value: str) -> bool:
    if len(value) > MAX_VALUE_CHARS:
        raise ValueError("Permission parameter exceeds matching size limit")
    try:
        budget = _ACTIVE_BUDGET.get() or MatchBudget()
        return compile_pattern(pattern).fullmatch(
            value, timeout=budget.timeout(),
        ) is not None
    except TimeoutError as exc:
        raise ValueError("Permission regular expression exceeded matching time limit") from exc
