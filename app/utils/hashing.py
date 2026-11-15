import hashlib
import re

from app.utils.urls import normalize_url


def content_hash(*parts: str) -> str:
    normalized = "\x1f".join((part or "").strip() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def url_hash(canonical_url: str) -> str:
    """Hash the canonical form so equivalent job URLs share one identity."""
    canonical = normalize_url(canonical_url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def secondary_job_hash(company: str, title: str, location: str) -> str:
    """Hash a normalized company/title/location identity across ATS sources."""
    parts = [re.sub(r"\s+", " ", value or "").strip().casefold() for value in (company, title, location)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
