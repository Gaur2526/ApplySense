import pytest
from pydantic import ValidationError

from app.config import AppConfig, load_config


def test_sample_config_validates():
    config = load_config("config/config.yaml", env_file=None)
    assert config.telegram.enabled is False
    assert config.company_registry_path == "data/company_registry.json"
    assert config.notifications.claim_lease_seconds == 900
    assert config.notifications.max_attempts == 3
    assert config.notifications.per_run_cap == 20


def test_invalid_maximum_minimum_experience_limit_rejected():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"filters": {"max_min_years": 51}})


def test_notification_lease_and_attempt_limits_are_validated():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"notifications": {"claim_lease_seconds": 0}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"notifications": {"max_attempts": 0}})
