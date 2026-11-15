from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_KEYS = {"gh_src", "source", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "referrer"}


def normalize_url(url: str) -> str:
    """Canonicalize job URLs by removing fragments, tracking args, and default ports."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"Invalid job URL: {url!r}")
    port = parts.port
    netloc = host if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f"{host}:{port}"
    if parts.username:
        raise ValueError("Job URLs must not contain credentials")
    path = parts.path.rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS and not k.lower().startswith("utm_")]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))
