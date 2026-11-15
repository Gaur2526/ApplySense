import html
import re
from urllib.parse import quote


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


def format_job(job: dict, *, limit: int = 4096) -> str:
    title = html.escape(str(job.get("title") or "Untitled"), quote=True)
    company = html.escape(str(job.get("company") or ""), quote=True)
    location = html.escape(str(job.get("location") or "Unspecified"), quote=True)
    url = html.escape(quote(str(job.get("canonical_url") or ""), safe=":/?#[]@!$&'()*+,;=%"), quote=True)
    lines = [f"<b>{title} — {company}</b>", f"Location: {location}"]
    if job.get("uncertainty_reasons"):
        lines.append("Check requirements: some role details are uncertain.")
    lines.append(f'<a href="{url}">View job</a>')
    return truncate_html("\n".join(lines), limit=limit)
