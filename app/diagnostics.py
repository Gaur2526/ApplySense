from collections import Counter
from datetime import datetime, timezone

from app.config import AppConfig
from app.filters.decision import FilterStatus
from app.filters.age_filter import age_days_at
from app.filters.evaluation import FILTER_NAMES, evaluate_filter_decisions
from app.filters.location_filter import is_india_location


def format_filter_diagnostics(
    jobs: list[dict], config: AppConfig, *, run_time: datetime | None = None
) -> str:
    if run_time is None:
        run_time = datetime.now(timezone.utc)
    counts = {name: Counter({status.value: 0 for status in FilterStatus}) for name in FILTER_NAMES}
    first_counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    india_jobs = []
    keyword_rejections = []
    unrecognized_locations: Counter[str] = Counter()
    unrecognized_location_sources: dict[str, Counter[str]] = {}
    age_only_rejections = []
    location_decisions = []

    for job in jobs:
        individual, pipeline, first = evaluate_filter_decisions(job, config, run_time=run_time)
        location_decisions.append((job, individual["location"]))
        for name, decision in individual.items():
            counts[name][decision.status.value] += 1
        if first:
            first_counts[first] += 1
        else:
            first_counts["none"] += 1
        reasons.update(
            decision.reason for decision in pipeline.values()
            if decision.status is FilterStatus.REJECT
        )
        if individual["keyword"].status is FilterStatus.REJECT:
            keyword_rejections.append(str(job.get("title") or "Untitled"))
        if (
            config.filters.unknown_location_policy == "strict"
            and individual["location"].status is FilterStatus.REJECT
            and individual["location"].reason
            == "Location is unrecognized under the strict unknown-location policy."
        ):
            location = str(job.get("location") or "").strip()
            source = str(job.get("source") or "unknown")
            unrecognized_locations[location] += 1
            unrecognized_location_sources.setdefault(location, Counter())[source] += 1
        rejecting_filters = [
            name for name, decision in pipeline.items()
            if decision.status is FilterStatus.REJECT
        ]
        if rejecting_filters == ["age"]:
            age_only_rejections.append((job, age_days_at(job, run_time)))
        if is_india_location(job, config.filters.preferred_cities + config.filters.locations):
            india_jobs.append((job, individual, first))

    lines = [
        f"Dry-run filter diagnostic: {len(jobs)} fetched jobs",
        f"Unknown location policy: {config.filters.unknown_location_policy}",
        "Text fields: keyword=title; excluded keyword=title; seniority=title; location=location; experience=experience_requirement/description; employment=employment_type/title",
        "",
        "Filter decision counts:",
    ]
    for name in FILTER_NAMES:
        lines.append(
            f"  {name}: PASS={counts[name]['PASS']} UNCERTAIN={counts[name]['UNCERTAIN']} REJECT={counts[name]['REJECT']}"
        )
    lines.extend(["", "First rejection filter counts:"])
    for name in (*FILTER_NAMES, "none"):
        lines.append(f"  {name}: {first_counts[name]}")

    lines.extend(["", "Top 10 rejection reasons (each rejected filter decision counted):"])
    for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:10]:
        lines.append(f"  {count} — {reason}")
    if not reasons:
        lines.append("  None")

    lines.extend(["", "Unrecognized locations (top 25):"])
    if config.filters.unknown_location_policy != "strict" or not unrecognized_locations:
        lines.append("  None")
    else:
        for location, count in sorted(
            unrecognized_locations.items(),
            key=lambda item: (-item[1], item[0].casefold()),
        )[:25]:
            sources = ", ".join(
                f"{source} ({source_count})"
                for source, source_count in sorted(
                    unrecognized_location_sources[location].items(),
                    key=lambda item: item[0].casefold(),
                )
            )
            lines.append(f"  {count} — {location} — {sources}")

    lines.extend(["", f"Near misses (rejected only by age): {len(age_only_rejections)}"])
    if not age_only_rejections:
        lines.append("  None")
    for job, age_days in age_only_rejections:
        age_text = str(age_days) if age_days is not None else "unknown"
        lines.append(
            f"  {job.get('title') or 'Untitled'} — {job.get('company') or 'Unknown company'} — "
            f"{job.get('location') or 'Unspecified'} — {age_text} days — "
            f"age_source={job.get('age_source') or 'unknown'}"
        )

    lines.extend(["", "Location decisions (all jobs):"])
    if not jobs:
        lines.append("  None")
    for job, decision in location_decisions:
        lines.append(
            f"  {job.get('title') or 'Untitled'} — {job.get('company') or 'Unknown company'} — "
            f"{job.get('location') or 'Unspecified'} — {decision.status.value}: {decision.reason}"
        )

    lines.extend(["", f"India-located jobs ({len(india_jobs)}):"])
    if not india_jobs:
        lines.append("  None")
    for job, decisions, first in india_jobs:
        lines.append(f"  {job.get('title') or 'Untitled'} — {job.get('location') or 'Unspecified'}")
        for name in FILTER_NAMES:
            decision = decisions[name]
            lines.append(f"    {name}: {decision.status.value} — {decision.reason}")
        lines.append(f"    first rejection: {first or 'none'}")

    lines.extend(["", "Keyword-filter rejected job titles (up to 10):"])
    if keyword_rejections:
        lines.extend(f"  - {title}" for title in keyword_rejections[:10])
    else:
        lines.append("  None")
    return "\n".join(lines)
