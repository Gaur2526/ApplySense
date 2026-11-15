"""Shared tri-state filter result."""
from dataclasses import dataclass
from enum import Enum


class FilterStatus(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class FilterDecision:
    status: FilterStatus
    reason: str

    def __bool__(self) -> bool:
        """Keep legacy boolean callers permissive for uncertain jobs."""
        return self.status is not FilterStatus.REJECT


def passed(reason: str) -> FilterDecision:
    return FilterDecision(FilterStatus.PASS, reason)


def rejected(reason: str) -> FilterDecision:
    return FilterDecision(FilterStatus.REJECT, reason)


def uncertain(reason: str) -> FilterDecision:
    return FilterDecision(FilterStatus.UNCERTAIN, reason)
