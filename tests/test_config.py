import pytest
from pydantic import ValidationError
import logging

from app.config import AppConfig


@pytest.fixture
def sample_config():
    return AppConfig.model_validate({
        "company_registry_path": "data/company_registry.json",
        "filters": {"keywords": ["software engineer"]},
        "telegram": {"enabled": False},
        "notifications": {
            "claim_lease_seconds": 900,
            "max_attempts": 3,
            "per_run_cap": 20,
        },
    })


def test_sample_config_validates(sample_config):
    config = sample_config
    assert config.telegram.enabled is False
    assert config.company_registry_path == "data/company_registry.json"
    assert config.notifications.claim_lease_seconds == 900
    assert config.notifications.max_attempts == 3
    assert config.notifications.per_run_cap == 20


def test_telegram_token_is_masked_from_config_representations_and_logs(caplog):
    token = "123456:FAKE-TOKEN"
    config = AppConfig.model_validate({
        "filters": {"keywords": ["software engineer"]},
        "telegram": {"bot_token": token, "chat_id": "test-chat", "enabled": True},
    })

    representations = (
        repr(config),
        str(config),
        repr(config.model_dump()),
        str(config.model_dump()),
        config.model_dump_json(),
        repr(config.telegram),
        str(config.telegram),
    )
    representation_leaked = any(token in rendered for rendered in representations)
    assert representation_leaked is False, "Sensitive value appeared in a configuration representation"
    simulated_assertion_message = f"Configuration assertion failed: {config!r}"
    assertion_message_leaked = token in simulated_assertion_message
    assert assertion_message_leaked is False, "Sensitive value appeared in a pytest-style assertion message"

    logging.getLogger("test.config.secrets").warning("config=%s", config)
    logging.getLogger("test.config.secrets").warning("dump=%r", config.model_dump())
    logs_leaked = token in caplog.text
    assert logs_leaked is False, "Sensitive value appeared in captured logs"


def test_invalid_maximum_minimum_experience_limit_rejected():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"filters": {"max_min_years": 51}})


def test_empty_keywords_list_is_rejected():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"filters": {"keywords": []}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"filters": {"keywords": ["", "  "]}})


def test_unknown_location_policy_accepts_only_allow_or_strict():
    assert AppConfig.model_validate({"filters": {"keywords": ["engineer"]}}).filters.unknown_location_policy == "strict"
    assert AppConfig.model_validate({"filters": {"keywords": ["engineer"], "unknown_location_policy": "allow"}}).filters.unknown_location_policy == "allow"
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"filters": {"keywords": ["engineer"], "unknown_location_policy": "lenient"}})


def test_notification_lease_and_attempt_limits_are_validated():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"notifications": {"claim_lease_seconds": 0}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"notifications": {"max_attempts": 0}})
