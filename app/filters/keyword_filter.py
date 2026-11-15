from app.filters.decision import FilterDecision, passed, rejected, uncertain


def matches_keywords(job: dict, include: list[str], exclude: list[str] | None = None) -> FilterDecision:
    text = " ".join(str(job.get(k, "")) for k in ("title", "description", "company")).casefold()
    if not text.strip():
        return uncertain("Job text is missing.")
    for term in exclude or []:
        if term.casefold() in text:
            return rejected(f"Excluded keyword found: {term}.")
    if not include:
        return passed("No required include keywords are configured.")
    if any(term.casefold() in text for term in include):
        return passed("At least one configured include keyword matched.")
    return rejected("No configured include keyword matched.")
