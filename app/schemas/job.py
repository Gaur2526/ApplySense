"""Validated normalized job record shared by source scouts and persistence."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """Normalized job data, validated before conversion to the SQLAlchemy ORM."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    location: str = Field(default="", max_length=500)
    employment_type: str = Field(default="", max_length=100)
    description: str = ""
    canonical_url: str = Field(max_length=2000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$", max_length=64)
    experience_requirement: int | None = Field(default=None, ge=0, le=50)
    posted_at: datetime | None = None
    age_source: str | None = Field(default=None, max_length=16)
    age_days: int | None = Field(default=None, ge=0, le=36500)
    discovered_at: datetime = Field(default_factory=utc_now)
    uncertainty_reasons: list[str] = Field(default_factory=list)

    @field_validator("source", "source_id", "company", "title")
    @classmethod
    def required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("canonical_url")
    @classmethod
    def require_absolute_http_url(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            valid = parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
            # Accessing .port also rejects malformed port syntax.
            _ = parts.port
        except ValueError as exc:
            raise ValueError("canonical_url must be an absolute HTTP(S) URL") from exc
        if not valid:
            raise ValueError("canonical_url must be an absolute HTTP(S) URL")
        return value

    @field_validator("content_hash")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("posted_at")
    @classmethod
    def normalize_posted_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("posted_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("discovered_at")
    @classmethod
    def normalize_discovered_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("discovered_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("uncertainty_reasons")
    @classmethod
    def reasons_must_be_nonblank(cls, value: list[str]) -> list[str]:
        if any(not reason.strip() for reason in value):
            raise ValueError("uncertainty reasons must not be blank")
        return value

    def to_orm_dict(self) -> dict:
        """Return exactly the persisted fields accepted by JobRepository."""
        return self.model_dump(mode="python")
