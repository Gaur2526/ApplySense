"""Validated application configuration loaded from YAML and environment."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoutConfig(StrictModel):
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    retries: int = Field(default=3, ge=0, le=10)
    concurrency: int = Field(default=4, ge=1, le=20)


class FilterConfig(StrictModel):
    keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    seniority_titles: list[str] = Field(default_factory=lambda: [
        "senior", "sr", "lead", "staff", "principal", "manager", "head",
        "architect", "director", "vp", "vice president", "chief",
    ])
    locations: list[str] = Field(default_factory=list)
    preferred_cities: list[str] = Field(default_factory=list)
    max_min_years: int = Field(default=2, ge=0, le=50)
    employment_types: list[str] = Field(default_factory=lambda: ["full-time"])


class TelegramConfig(StrictModel):
    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool = False


class NotificationConfig(StrictModel):
    claim_lease_seconds: int = Field(default=900, ge=1, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=20)


class AppConfig(StrictModel):
    database_url: str = "sqlite:///./applysense.db"
    company_registry_path: str = "data/company_registry.json"
    log_level: str = "INFO"
    scout: ScoutConfig = Field(default_factory=ScoutConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return value

    @field_validator("database_url")
    @classmethod
    def valid_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("database_url must not be empty")
        return value

    def require_telegram(self) -> None:
        if not self.telegram.bot_token or not self.telegram.chat_id:
            raise ValueError("Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")


def load_config(path: str | Path = "config/config.yaml", *, env_file: str | Path | None = ".env") -> AppConfig:
    """Load YAML defaults, then overlay supported environment variables."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    config_path = Path(path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError("Configuration YAML root must be a mapping")
            raw = loaded
    raw.setdefault("scout", {})
    raw.setdefault("filters", {})
    raw.setdefault("telegram", {})
    raw.setdefault("notifications", {})
    for env, key in (("DATABASE_URL", "database_url"), ("LOG_LEVEL", "log_level")):
        if os.getenv(env) is not None:
            raw[key] = os.environ[env]
    for env, key in (("TELEGRAM_BOT_TOKEN", "bot_token"), ("TELEGRAM_CHAT_ID", "chat_id")):
        if os.getenv(env) is not None:
            raw["telegram"][key] = os.environ[env]
    if os.getenv("TELEGRAM_ENABLED") is not None:
        raw["telegram"]["enabled"] = os.environ["TELEGRAM_ENABLED"].lower() in {"1", "true", "yes", "on"}
    return AppConfig.model_validate(raw)
