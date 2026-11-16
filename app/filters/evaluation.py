"""Shared filter evaluation used by discovery and dry-run diagnostics."""
from app.config import AppConfig
from app.filters.decision import FilterDecision, FilterStatus
from app.filters.age_filter import matches_age
from app.filters.employment_filter import matches_employment
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.keyword_filter import matches_keywords
from app.filters.location_filter import matches_location, matches_workday_location
from app.filters.seniority_filter import matches_seniority

FILTER_NAMES = ("keyword", "excluded keyword", "seniority", "location", "experience", "employment", "age")


def evaluate_filter_decisions(
    job: dict,
    config: AppConfig,
    *,
    location_assume_country: str | None = None,
    run_time=None,
) -> tuple[dict[str, FilterDecision], dict[str, FilterDecision], str | None]:
    """Return individual diagnostic decisions, pipeline decisions, and first rejection filter."""
    filters = config.filters
    if location_assume_country is None:
        location_assume_country = job.get("_workday_assume_country")
    if job.get("experience_requirement") is None:
        job["experience_requirement"] = parse_experience_requirement(
            f"{job.get('title', '')} {job.get('description', '')}"
        )

    keyword = matches_keywords(job, filters.keywords, [])
    excluded = matches_keywords(job, [], filters.excluded_keywords)
    seniority = matches_seniority(job, filters.seniority_titles)
    experience = matches_experience(job, filters.max_min_years)
    if job.get("source") == "workday":
        location = matches_workday_location(
            job, filters.unknown_location_policy, location_assume_country
        )
    else:
        location = matches_location(job, filters.unknown_location_policy)
    employment = matches_employment(job, filters.employment_types)
    age = matches_age(
        job, filters.max_age_days, filters.unknown_age_policy, run_time=run_time
    )
    individual = {
        "keyword": keyword,
        "excluded keyword": excluded,
        "seniority": seniority,
        "location": location,
        "experience": experience,
        "employment": employment,
        "age": age,
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
        "age": age,
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
