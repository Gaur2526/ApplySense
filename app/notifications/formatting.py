def format_job(job: dict) -> str:
    lines = [
        f"{job.get('title', 'Untitled')} — {job.get('company', '')}",
        f"Location: {job.get('location') or 'Unspecified'}",
    ]
    if job.get("uncertainty_reasons"):
        lines.append("Check requirements: some role details are uncertain.")
    lines.append(job["canonical_url"])
    return "\n".join(lines)
