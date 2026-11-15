import re
from typing import Any

from app.filters.decision import FilterDecision, passed, rejected, uncertain


def matches_seniority(job: dict[str, Any], titles: list[str]) -> FilterDecision:
    title = str(job.get("title") or "")
    if not title.strip():
        return uncertain("Job title is not stated.")
    for seniority in titles:
        term = seniority.strip()
        if term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, flags=re.I):
            return rejected(f"Title contains excluded seniority term: {term}.")
    return passed("Title does not contain an excluded seniority term.")
