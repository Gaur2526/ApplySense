import pytest
from pydantic import ValidationError

from app.config import AppConfig, load_config


def test_sample_config_validates():
    config = load_config("config/config.yaml", env_file=None)
    assert config.telegram.enabled is False
    assert config.company_registry_path == "data/company_registry.json"


def test_invalid_maximum_minimum_experience_limit_rejected():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"filters": {"max_min_years": 51}})
