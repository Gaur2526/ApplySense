"""Shared filter evaluation used by discovery and dry-run diagnostics."""
from app.config import AppConfig
from app.filters.decision import FilterDecision, FilterStatus
from app.filters.employment_filter import matches_employment
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.keyword_filter import matches_keywords
from app.filters.location_filter import matches_location
from app.filters.seniority_filter import matches_seniority

FILTER_NAMES = ("keyword", "excluded keyword", "seniority", "location", "experience", "employment")


def evaluate_filter_decisions(
    job: dict, config: AppConfig
) -> tuple[dict[str, FilterDecision], dict[str, FilterDecision], str | None]:
    """Return individual diagnostic decisions, pipeline decisions, and first rejection filter."""
    filters = config.filters
    if job.get("experience_requirement") is None:
        job["experience_requirement"] = parse_experience_requirement(
            f"{job.get('title', '')} {job.get('description', '')}"
        )

    keyword = matches_keywords(job, filters.keywords, [])
    excluded = matches_keywords(job, [], filters.excluded_keywords)
    seniority = matches_seniority(job, filters.seniority_titles)
    experience = matches_experience(job, filters.max_min_years)
    location = matches_location(job, filters.unknown_location_policy)
    employment = matches_employment(job, filters.employment_types)
    individual = {
        "keyword": keyword,
        "excluded keyword": excluded,
        "seniority": seniority,
        "location": location,
        "experience": experience,
        "employment": employment,
    }

    # Keep production behavior: the combined keyword filter checks exclusions
    # before include terms, followed by seniority, experience, location, and type.
    pipeline_keyword = matches_keywords(job, filters.keywords, filters.excluded_keywords)
    pipeline = {
        "keyword": pipeline_keyword,
        "seniority": seniority,
        "experience": experience,
        "location": location,
        "employment": employment,
    }
    first_rejection = None
    for name, decision in pipeline.items():
        if decision.status is FilterStatus.REJECT:
            if name == "keyword" and excluded.status is FilterStatus.REJECT:
                first_rejection = "excluded keyword"
            else:
                first_rejection = name
            break
    return individual, pipeline, first_rejection
