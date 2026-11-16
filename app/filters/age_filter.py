"""Tri-state age decisions and timestamp calculations shared by all sources."""
from __future__ import annotations

from datetime import datetime, timezone
from math import floor
from typing import Any, Literal

from app.filters.decision import FilterDecision, passed, rejected, uncertain


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_days_at(
    job: dict[str, Any],
    run_time: datetime | None = None,
) -> int | None:
    """Return floored whole age days using posted_at, or age_days plus discovery age."""
    now = parse_timestamp(run_time) if run_time is not None else datetime.now(timezone.utc)
    assert now is not None
    posted_at = parse_timestamp(job.get("posted_at"))
    if posted_at is not None:
        # Clock skew must not create negative ages or violate the normalized
        # schema's non-negative age constraint.
        return max(0, floor((now - posted_at).total_seconds() / 86400))

    source_age = job.get("age_days")
    if isinstance(source_age, int) and not isinstance(source_age, bool) and source_age >= 0:
        discovered_at = parse_timestamp(job.get("discovered_at"))
        elapsed_days = (
            max(0, floor((now - discovered_at).total_seconds() / 86400))
            if discovered_at is not None
            else 0
        )
        return source_age + elapsed_days
    return None


def matches_age(
    job: dict[str, Any],
    max_age_days: int = 7,
    unknown_age_policy: Literal["allow", "reject"] = "allow",
    *,
    run_time: datetime | None = None,
) -> FilterDecision:
    age_days = age_days_at(job, run_time)
    if age_days is None:
        reason = "Posting date is unknown."
        if job.get("_age_unknown_is_uncertain") or unknown_age_policy == "allow":
            return uncertain(reason)
        return rejected(f"{reason} Unknown-age policy is reject.")
    if age_days > max_age_days:
        return rejected(f"Posting is {age_days} days old; maximum allowed is {max_age_days}.")
    return passed(f"Posting is {max(0, age_days)} days old; maximum allowed is {max_age_days}.")
