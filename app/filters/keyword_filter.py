import re

from app.filters.decision import FilterDecision, passed, rejected, uncertain


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _matches_term(text: str, term: str) -> bool:
    normalized_term = _normalize(term)
    if not normalized_term:
        return False
    pattern = rf"(?<!\w){re.escape(normalized_term)}(?!\w)"
    return re.search(pattern, text) is not None


def matches_keywords(job: dict, include: list[str], exclude: list[str] | None = None) -> FilterDecision:
    title = str(job.get("title") or "")
    text = _normalize(title)
    if not text:
        return uncertain("Job title is missing.")
    for term in exclude or []:
        if _matches_term(text, term):
            return rejected(f'Title "{title}" matched excluded keyword "{term}".')
    if not include:
        return passed("No required include keywords are configured.")
    for term in include:
        if _matches_term(text, term):
            return passed(f'Title "{title}" matched include keyword "{term}".')
    return rejected("No configured include keyword matched.")
