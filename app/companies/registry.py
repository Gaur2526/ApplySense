"""Load and validate the extensible target company registry."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator, model_validator


class CompanyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str
    ats_provider: str | None
    board_identifier: str | None = None
    status: Literal["pending_verification", "verified", "unsupported", "inactive"]
    careers_url: HttpUrl | None = None

    @field_validator("company_name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("ats_provider")
    @classmethod
    def provider_is_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value == "pending_verification" or not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
            raise ValueError("ats_provider must be a provider identifier or null")
        return value

    @field_validator("board_identifier")
    @classmethod
    def board_identifier_is_nonblank(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("board_identifier must not be whitespace-only")
        return value

    @model_validator(mode="after")
    def identifier_must_be_verified(self) -> "CompanyEntry":
        if self.status != "verified" and self.board_identifier is not None:
            raise ValueError("unverified registry entries cannot have a board_identifier")
        if self.status == "verified" and self.ats_provider == "greenhouse" and not self.board_identifier:
            raise ValueError("verified Greenhouse entries require a board_identifier")
        return self


def load_registry(path: str | Path = "data/company_registry.json") -> list[CompanyEntry]:
    registry_path = Path(path)
    values: Any = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("Company registry must be a JSON array")
    entries = [CompanyEntry.model_validate(value) for value in values]
    names = [entry.company_name for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("Company registry contains duplicate company names")
    return entries


def verified_greenhouse_entries(entries: list[CompanyEntry]) -> list[CompanyEntry]:
    """Return only explicitly verified Greenhouse entries with a usable token."""
    return [entry for entry in entries if entry.status == "verified" and (entry.ats_provider or "").casefold() == "greenhouse" and entry.board_identifier]
