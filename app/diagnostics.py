from collections import Counter

from app.config import AppConfig
from app.filters.decision import FilterStatus
from app.filters.evaluation import FILTER_NAMES, evaluate_filter_decisions
from app.filters.location_filter import is_india_location


def format_filter_diagnostics(jobs: list[dict], config: AppConfig) -> str:
    counts = {name: Counter({status.value: 0 for status in FilterStatus}) for name in FILTER_NAMES}
    first_counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    india_jobs = []
    keyword_rejections = []

    for job in jobs:
        individual, pipeline, first = evaluate_filter_decisions(job, config)
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
