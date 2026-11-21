import html
import re
from datetime import date, datetime, timezone
from urllib.parse import quote

from app.filters.age_filter import age_days_at

HTML_TOKEN = re.compile(r"(<[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);)")
TAG_NAME = re.compile(r"^</?\s*([a-zA-Z][a-zA-Z0-9]*)")


def _telegram_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def truncate_html(message: str, limit: int = 4096) -> str:
    """Truncate serialized HTML without splitting a tag or entity."""
    if limit <= 0:
        return ""
    tokens = HTML_TOKEN.split(message)
    output: list[str] = []
    open_tags: list[str] = []
    used = 0
    truncated = False

    def add_token(token: str) -> bool:
        nonlocal used
        match = TAG_NAME.match(token) if token.startswith("<") else None
        closing_tags = list(reversed(open_tags))
        ellipsis_len = 1
        if match:
            name = match.group(1).lower()
            is_closing = token.startswith("</")
            self_closing = token.rstrip().endswith("/>") or name in {"br", "hr", "img"}
            if is_closing:
                if name in open_tags:
                    closing_tags.remove(name)
            elif not self_closing:
                closing_tags.insert(0, name)
        reserve = sum(_telegram_length(f"</{tag}>") for tag in closing_tags)
        if used + _telegram_length(token) + reserve + ellipsis_len > limit:
            return False
        output.append(token)
        used += _telegram_length(token)
        if match:
            name = match.group(1).lower()
            if token.startswith("</"):
                if name in open_tags:
                    open_tags.reverse()
                    open_tags.remove(name)
                    open_tags.reverse()
            elif not (token.rstrip().endswith("/>") or name in {"br", "hr", "img"}):
                open_tags.append(name)
        return True

    for part in tokens:
        if not part:
            continue
        if HTML_TOKEN.fullmatch(part):
            if not add_token(part):
                truncated = True
                break
        else:
            for character in part:
                if not add_token(character):
                    truncated = True
                    break
            if truncated:
                break

    if truncated and used < limit:
        closings = "".join(f"</{tag}>" for tag in reversed(open_tags))
        if used + 1 + len(closings) <= limit:
            output.append("…")
            used += 1
    output.extend(f"</{tag}>" for tag in reversed(open_tags))
    return "".join(output)


def _uncertainty_labels(job: dict) -> list[str]:
    labels: list[str] = []
    source = str(job.get("source") or "").casefold()
    for reason in job.get("uncertainty_reasons") or []:
        reason = str(reason)
        lowered = reason.casefold()
        if source == "greenhouse" and lowered == "employment type is not stated.":
            continue
        if "conflicting minimum experience requirements" in lowered:
            match = re.search(r"found:\s*(.+?)\s+years?\.?$", reason, re.I)
            label = f"experience conflicting ({match.group(1)} years)" if match else "experience conflicting"
        elif "minimum experience requirement is not stated" in lowered:
            label = "experience not stated"
        elif "location" in lowered:
            label = "location unclear"
        elif "employment type is not stated" in lowered:
            label = "employment type not stated"
        elif "employment type" in lowered:
            label = "employment type unclear"
        else:
            shortened = " ".join(re.sub(r"[.!?]+$", "", reason).split()[:6])
            label = shortened.casefold() or "details unclear"
        if label not in labels:
            labels.append(label)
    return labels


def format_job(job: dict, *, limit: int = 4096, run_time=None) -> str:
    title = html.escape(str(job.get("title") or "Untitled"), quote=True)
    company = html.escape(str(job.get("company") or ""), quote=True)
    location = html.escape(str(job.get("location") or "Unspecified"), quote=True)
    url = html.escape(quote(str(job.get("canonical_url") or ""), safe=":/?#[]@!$&'()*+,;=%"), quote=True)
    lines = [f"<b>{title} — {company}</b>", f"Location: {location}"]
    age_days = age_days_at(job, run_time)
    if age_days is None:
        lines.append("Posting date unknown")
    else:
        age_source = "Updated" if job.get("age_source") == "updated" else "Posted"
        if age_days <= 0:
            lines.append(f"{age_source} today")
        else:
            unit = "day" if age_days == 1 else "days"
            lines.append(f"{age_source} {age_days} {unit} ago")
    apply_by = job.get("apply_by")
    if apply_by is not None:
        if isinstance(apply_by, str):
            try:
                apply_by = date.fromisoformat(apply_by[:10])
            except ValueError:
                apply_by = None
        if isinstance(apply_by, datetime):
            apply_by = apply_by.date()
        if isinstance(apply_by, date):
            today = (run_time or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
            days_left = max(0, (apply_by - today).days - 1)
            countdown = f"{days_left} {'day' if days_left == 1 else 'days'} left"
            remaining = "closing soon" if days_left <= 1 else countdown
            lines.append(f"Apply by {apply_by.strftime('%b')} {apply_by.day} ({remaining})")
    if job.get("experience_requirement") is not None:
        lines.append(f"Experience: {job['experience_requirement']}+ years")
    checks = _uncertainty_labels(job)
    if checks:
        lines.append("Check: " + "; ".join(checks))
    lines.append(f'<a href="{url}">View job</a>')
    return truncate_html("\n".join(lines), limit=limit)
