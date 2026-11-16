import json
import sqlite3

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.cli as cli
import app.main as main
from app.config import AppConfig
from app.companies.registry import CompanyEntry
from app.database.connection import (
    SCHEMA_VERSION, SCHEMA_VERSION_ERROR, DatabaseVersionError,
    initialize_database, make_engine, validate_database_version,
)
from app.database.models import DatabaseMeta


def create_legacy_database(path, *, with_jobs_table=True):
    with sqlite3.connect(path) as connection:
        if with_jobs_table:
            connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")


def configured_workday(tmp_path, database_path):
    entry = CompanyEntry.model_validate({
        "company_name": "Example Workday", "ats_provider": "workday", "status": "verified",
        "workday": {
            "host": "example.wd1.myworkdayjobs.com", "tenant": "example", "site": "External",
            "country_facet_id": "facet", "search_texts": ["engineer"],
            "min_interval_seconds": 0,
        },
    })
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps([entry.model_dump(mode="json")]))
    return AppConfig(
        database_url=f"sqlite:///{database_path}",
        company_registry_path=str(registry_path),
        filters={"keywords": ["engineer"]},
    )


def test_existing_database_with_older_version_is_rejected(tmp_path):
    database_path = tmp_path / "applysense.db"
    create_legacy_database(database_path)
    engine = make_engine(f"sqlite:///{database_path}")
    try:
        try:
            initialize_database(engine)
        except DatabaseVersionError as exc:
            assert str(exc) == SCHEMA_VERSION_ERROR
        else:
            raise AssertionError("older database version must be rejected")
    finally:
        engine.dispose()


def test_existing_database_missing_schema_version_is_rejected(tmp_path):
    database_path = tmp_path / "missing-version.db"
    create_legacy_database(database_path)
    engine = make_engine(f"sqlite:///{database_path}")
    try:
        try:
            validate_database_version(engine)
        except DatabaseVersionError as exc:
            assert str(exc) == SCHEMA_VERSION_ERROR
        else:
            raise AssertionError("database without schema version must be rejected")
    finally:
        engine.dispose()


def test_matching_database_version_is_accepted_and_written_for_new_database(tmp_path):
    database_path = tmp_path / "current.db"
    engine = make_engine(f"sqlite:///{database_path}")
    try:
        initialize_database(engine)
        validate_database_version(engine)
        initialize_database(engine)
        with Session(engine) as session:
            assert session.scalar(
                select(DatabaseMeta.value).where(DatabaseMeta.key == "schema_version")
            ) == str(SCHEMA_VERSION)
    finally:
        engine.dispose()


def test_missing_database_with_dry_run_is_not_created_or_rejected(tmp_path, monkeypatch):
    database_path = tmp_path / "not-created.db"
    config = configured_workday(tmp_path, database_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    class EmptyWorkdayScout:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, *_args, **_kwargs):
            return []
        async def close(self):
            pass

    monkeypatch.setattr(main, "WorkdayScout", EmptyWorkdayScout)
    assert cli.main(["run", "--dry-run"]) == 0
    assert not database_path.exists()


def test_cli_reports_old_database_without_traceback(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "old.db"
    create_legacy_database(database_path)
    config = AppConfig(database_url=f"sqlite:///{database_path}")
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    result = cli.main(["status"])
    output = capsys.readouterr().out
    assert result == 2
    assert output.strip() == SCHEMA_VERSION_ERROR
    assert "Traceback" not in output


def test_cli_run_rejects_old_database_before_discovery(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "old-run.db"
    create_legacy_database(database_path)
    config = AppConfig(database_url=f"sqlite:///{database_path}")
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    def forbidden_discovery(*_args, **_kwargs):
        raise AssertionError("database version must be checked before discovery")

    monkeypatch.setattr(main, "discover", forbidden_discovery)
    result = cli.main(["run"])
    output = capsys.readouterr().out
    assert result == 2
    assert output.strip() == SCHEMA_VERSION_ERROR
    assert "Traceback" not in output


def test_dry_run_rejects_existing_database_without_schema_version(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "old-dry-run.db"
    create_legacy_database(database_path)
    config = configured_workday(tmp_path, database_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    class ForbiddenWorkdayScout:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, *_args, **_kwargs):
            raise AssertionError("version check must happen before scout fetch")
        async def close(self):
            pass

    monkeypatch.setattr(main, "WorkdayScout", ForbiddenWorkdayScout)
    assert cli.main(["run", "--dry-run"]) == 2
    output = capsys.readouterr().out
    assert output.strip() == SCHEMA_VERSION_ERROR
    assert "Traceback" not in output


def test_status_creates_missing_database_with_current_version(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "new.db"
    config = AppConfig(database_url=f"sqlite:///{database_path}")
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "Stored jobs: 0"
    engine = make_engine(f"sqlite:///{database_path}")
    try:
        validate_database_version(engine)
    finally:
        engine.dispose()
