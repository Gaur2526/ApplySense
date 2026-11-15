import re
from typing import Any

from app.filters.decision import FilterDecision, passed, rejected, uncertain

ALIASES = {
    "full-time": {"full time", "full-time", "fulltime", "permanent"},
    "part-time": {"part time", "part-time", "parttime"},
    "contract": {"contract", "contractor", "fixed term"},
    "internship": {"internship", "intern", "intern student"},
    "temporary": {"temporary", "temp"},
}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def matches_employment(job: dict[str, Any], allowed: list[str]) -> FilterDecision:
    value = _normalized(str(job.get("employment_type") or ""))
    if not value:
        return uncertain("Employment type is not stated.")

    padded_value = f" {value} "
    category = next(
        (
            name for name, aliases in ALIASES.items()
            if any(f" {_normalized(alias)} " in padded_value for alias in aliases)
        ),
        None,
    )
    if category is None:
        return uncertain(f"Employment type could not be classified: {value}.")

    enabled = {_normalized(item) for item in allowed}
    enabled_categories = {
        name for name, aliases in ALIASES.items()
        if any(_normalized(alias) in enabled for alias in aliases) or _normalized(name) in enabled
    }
    if category in {"contract", "internship"} and category not in enabled_categories:
        return rejected(f"{category.title()} roles are disabled in configuration.")
    if not allowed or category in enabled_categories:
        return passed(f"Employment type {category} is enabled.")
    return rejected(f"Employment type {category} is not enabled.")
