from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, DatabaseMeta

# Increment this whenever an ORM model or its persisted schema changes.
SCHEMA_VERSION = 1
SCHEMA_VERSION_ERROR = (
    "applysense.db was created by an older version. Delete it (rm applysense.db) "
    "and run again. No migration is provided."
)


class DatabaseVersionError(RuntimeError):
    """Raised when an existing database cannot be used by this schema version."""


def make_engine(database_url: str) -> Engine:
    kwargs = {"connect_args": {"check_same_thread": False}} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, **kwargs)


def initialize_database(engine: Engine) -> None:
    if _database_already_exists(engine):
        validate_database_version(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        version = connection.execute(
            select(DatabaseMeta.value).where(DatabaseMeta.key == "schema_version")
        ).scalar_one_or_none()
        if version is None:
            connection.execute(DatabaseMeta.__table__.insert().values(
                key="schema_version", value=str(SCHEMA_VERSION)
            ))


def validate_database_version(engine: Engine) -> None:
    """Require an existing database to carry this code's schema version."""
    try:
        tables = set(inspect(engine).get_table_names())
        if DatabaseMeta.__tablename__ not in tables:
            raise DatabaseVersionError(SCHEMA_VERSION_ERROR)
        with engine.connect() as connection:
            version = connection.execute(
                select(DatabaseMeta.value).where(DatabaseMeta.key == "schema_version")
            ).scalar_one_or_none()
    except DatabaseVersionError:
        raise
    except Exception as exc:
        raise DatabaseVersionError(SCHEMA_VERSION_ERROR) from exc
    if version is None:
        raise DatabaseVersionError(SCHEMA_VERSION_ERROR)
    try:
        parsed_version = int(version)
    except (TypeError, ValueError):
        raise DatabaseVersionError(SCHEMA_VERSION_ERROR) from None
    if parsed_version != SCHEMA_VERSION:
        raise DatabaseVersionError(SCHEMA_VERSION_ERROR)


def _database_already_exists(engine: Engine) -> bool:
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if database in (None, "", ":memory:") or str(database).startswith("file::memory:"):
            return bool(inspect(engine).get_table_names())
        return Path(database).expanduser().exists()
    try:
        return bool(inspect(engine).get_table_names())
    except Exception:
        return False


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
