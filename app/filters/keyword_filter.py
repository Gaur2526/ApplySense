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
    text = _normalize(" ".join(str(job.get(k, "")) for k in ("title", "description", "company")))
    if not text.strip():
        return uncertain("Job text is missing.")
    for term in exclude or []:
        if _matches_term(text, term):
            return rejected(f"Excluded keyword found: {term}.")
    if not include:
        return passed("No required include keywords are configured.")
    if any(_matches_term(text, term) for term in include):
        return passed("At least one configured include keyword matched.")
    return rejected("No configured include keyword matched.")
