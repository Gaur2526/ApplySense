"""Stable hash for the effective filter configuration."""
from __future__ import annotations

import hashlib
import json

from app.config import AppConfig


def effective_filter_settings_hash(config: AppConfig) -> str:
    payload = config.filters.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
