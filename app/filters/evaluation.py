"""Shared filter evaluation used by discovery and dry-run diagnostics."""
from functools import lru_cache
from pathlib import Path

from app.config import AppConfig
from app.companies.registry import load_registry, verified_workday_entries
from app.filters.decision import FilterDecision, FilterStatus
from app.filters.age_filter import matches_age
from app.filters.employment_filter import matches_employment
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.keyword_filter import matches_keywords
from app.filters.location_filter import matches_location, matches_workday_location
from app.filters.seniority_filter import matches_seniority

FILTER_NAMES = ("keyword", "excluded keyword", "seniority", "location", "experience", "employment", "age")


@lru_cache(maxsize=8)
def _registry_for_path(path: str, modified_ns: int):
    # Include mtime in the cache key so edits to the registry take effect in
    # long-running processes without repeatedly parsing the JSON file.
    del modified_ns
    return load_registry(path)


def location_evaluation_context(job: dict, config: AppConfig) -> tuple[str, str | None]:
    """Return the location branch and registry-derived assumed country.

    Workday source IDs are stored as ``<tenant>/<requisition-id>``. Resolving
    from that stable identity keeps the policy available to persisted jobs,
    even when no transient scout fields are present.
    """
    if str(job.get("source") or "").casefold() != "workday":
        return "generic", None
    source_id = str(job.get("source_id") or "")
    tenant_key = source_id.split("/", 1)[0].strip().casefold()
    if not tenant_key:
        return "workday", None
    registry_path = Path(config.company_registry_path)
    try:
        modified_ns = registry_path.stat().st_mtime_ns
    except OSError:
        return "workday", None
    entries = verified_workday_entries(
        _registry_for_path(str(registry_path.resolve()), modified_ns)
    )
    for entry in entries:
        tenant = entry.workday
        if tenant is not None and tenant.tenant.casefold() == tenant_key:
            return "workday", tenant.assume_country
    return "workday", None


def evaluate_filter_decisions(
    job: dict,
    config: AppConfig,
    *,
    run_time=None,
) -> tuple[dict[str, FilterDecision], dict[str, FilterDecision], str | None]:
    """Return individual diagnostic decisions, pipeline decisions, and first rejection filter."""
    filters = config.filters
    location_branch, location_assume_country = location_evaluation_context(job, config)
    if job.get("experience_requirement") is None:
        job["experience_requirement"] = parse_experience_requirement(
            f"{job.get('title', '')} {job.get('description', '')}",
            required_headings=filters.experience_requirement_headings,
            preferred_headings=filters.experience_preferred_headings,
        )

    keyword = matches_keywords(job, filters.keywords, [])
    excluded = matches_keywords(job, [], filters.excluded_keywords)
    seniority = matches_seniority(job, filters.seniority_titles)
    experience = matches_experience(
        job, filters.max_min_years,
        required_headings=filters.experience_requirement_headings,
        preferred_headings=filters.experience_preferred_headings,
    )
    if location_branch == "workday":
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
