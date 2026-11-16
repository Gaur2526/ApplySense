from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.config import AppConfig
from app.filters.age_filter import age_days_at, matches_age, parse_timestamp
from app.filters.decision import FilterStatus
from app.filters.evaluation import FILTER_NAMES, evaluate_filter_decisions
from app.filters.settings import effective_filter_settings_hash
from app.notifications.formatting import format_job


NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def test_age_filter_default_and_config_validation():
    filters = AppConfig(filters={"keywords": ["engineer"]}).filters
    assert filters.max_age_days == 7
    assert filters.unknown_age_policy == "allow"
    assert filters.baseline_include_recent is False
    with pytest.raises(ValidationError):
        AppConfig(filters={"keywords": ["engineer"], "max_age_days": -1})
    with pytest.raises(ValidationError):
        AppConfig(filters={"keywords": ["engineer"], "unknown_age_policy": "maybe"})


def test_age_filter_unknown_policy_is_tri_state():
    job = {"posted_at": None}
    allowed = matches_age(job, unknown_age_policy="allow", run_time=NOW)
    rejected = matches_age(job, unknown_age_policy="reject", run_time=NOW)
    assert allowed.status is FilterStatus.UNCERTAIN
    assert allowed.reason == "Posting date is unknown."
    assert rejected.status is FilterStatus.REJECT


@pytest.mark.parametrize(
    ("age", "expected"),
    [(0, FilterStatus.PASS), (6, FilterStatus.PASS), (7, FilterStatus.PASS), (8, FilterStatus.REJECT)],
)
def test_age_filter_uses_inclusive_maximum_age(age, expected):
    job = {"posted_at": NOW - timedelta(days=age)}
    assert matches_age(job, 7, "allow", run_time=NOW).status is expected


def test_age_is_floored_in_utc_and_offset_timestamps_are_normalized():
    posted = datetime.fromisoformat("2026-11-09T07:30:00+05:30")
    assert posted.astimezone(timezone.utc) == datetime(2026, 11, 9, 2, tzinfo=timezone.utc)
    assert age_days_at({"posted_at": posted}, NOW) == 7
    assert matches_age({"posted_at": NOW - timedelta(days=7, hours=23)}, 7, run_time=NOW).status is FilterStatus.PASS
    assert matches_age({"posted_at": NOW - timedelta(days=8)}, 7, run_time=NOW).status is FilterStatus.REJECT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-09T07:30:00-04:00", datetime(2026, 11, 9, 11, 30, tzinfo=timezone.utc)),
        ("2026-11-09T07:30:00+05:30", datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)),
        ("2026-11-09T07:30:00", datetime(2026, 11, 9, 7, 30, tzinfo=timezone.utc)),
    ],
)
def test_age_timestamp_offsets_and_naive_values_normalize_safely(value, expected):
    assert parse_timestamp(value) == expected


def test_future_timestamp_due_to_clock_skew_has_age_zero_and_passes():
    future = NOW + timedelta(days=2)
    assert age_days_at({"posted_at": future}, NOW) == 0
    assert matches_age({"posted_at": future}, 7, run_time=NOW).status is FilterStatus.PASS


def test_unknown_age_uncertainty_is_in_shared_explain_evaluation():
    config = AppConfig(filters={"keywords": ["engineer"]})
    assert "age" in FILTER_NAMES
    individual, pipeline, _ = evaluate_filter_decisions(
        {"title": "Software Engineer", "source": "greenhouse", "posted_at": None},
        config,
        run_time=NOW,
    )
    assert individual["age"].status is FilterStatus.UNCERTAIN
    assert pipeline["age"].reason == "Posting date is unknown."


def test_effective_filter_hash_changes_with_age_settings():
    base = AppConfig(filters={"keywords": ["engineer"]})
    age_changed = AppConfig(filters={"keywords": ["engineer"], "max_age_days": 8})
    policy_changed = AppConfig(filters={"keywords": ["engineer"], "unknown_age_policy": "reject"})
    assert len(effective_filter_settings_hash(base)) == 64
    assert effective_filter_settings_hash(base) != effective_filter_settings_hash(age_changed)
    assert effective_filter_settings_hash(base) != effective_filter_settings_hash(policy_changed)


@pytest.mark.parametrize(
    ("age_source", "age", "expected"),
    [
        ("posted", 0, "Posted today"),
        ("posted", 3, "Posted 3 days ago"),
        ("updated", 2, "Updated 2 days ago"),
    ],
)
def test_telegram_age_line(age_source, age, expected):
    job = {
        "title": "Engineer", "company": "Example", "location": "Bengaluru",
        "canonical_url": "https://jobs.example/job",
        "age_source": age_source, "age_days": age,
        "discovered_at": NOW,
    }
    assert expected in format_job(job, run_time=NOW)


def test_telegram_unknown_posting_date_line():
    message = format_job({
        "title": "Engineer", "company": "Example", "location": "Bengaluru",
        "canonical_url": "https://jobs.example/job",
    }, run_time=NOW)
    assert "Posting date unknown" in message
