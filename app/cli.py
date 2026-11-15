import argparse
import asyncio
import logging

import httpx

from app.config import load_config
from app.database.connection import initialize_database, make_engine, make_session_factory
from app.database.repository import JobRepository
from app.main import baseline, run
from app.scouts.greenhouse import GreenhouseScout
from app.utils.logger import configure_logging


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="applysense", description="Discover and filter Greenhouse jobs")
    root.add_argument("--config", default="config/config.yaml", help="YAML configuration path")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config", help="Validate configuration without network access")
    base = commands.add_parser("baseline", help="Fetch current jobs and mark them as pre-existing")
    base.add_argument("--dry-run", action="store_true", help="Fetch and report without database writes")
    base.add_argument("--limit", type=positive_int)
    execute = commands.add_parser("run", help="Discover new matching jobs")
    execute.add_argument("--mode", choices=("discovery", "qualified"), default="discovery")
    execute.add_argument("--baseline", action="store_true", help="Store fetched jobs as a baseline; skip notifications")
    execute.add_argument("--dry-run", action="store_true", help="Fetch and report without writes or notifications")
    execute.add_argument("--explain", action="store_true", help="With --dry-run, report every filter decision")
    execute.add_argument("--notify", action="store_true", help="Send Telegram notifications if enabled in configuration")
    execute.add_argument("--limit", type=positive_int, help="Maximum matching jobs to process")
    check = commands.add_parser("check-company", help="Check a Greenhouse public board without database writes")
    check.add_argument("identifier", help="Greenhouse board token")
    check.add_argument("--source", required=True, choices=("greenhouse",))
    commands.add_parser("status", help="Show the number of jobs stored in the database")
    return root


async def check_greenhouse_company(identifier: str, config) -> tuple[bool, list[dict]]:
    scout = GreenhouseScout(
        timeout=config.scout.request_timeout_seconds,
        retries=config.scout.retries,
        concurrency=config.scout.concurrency,
    )
    try:
        return True, await scout.fetch_jobs(identifier)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False, []
        raise
    finally:
        await scout.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run" and args.explain and not args.dry_run:
        parser().error("--explain requires --dry-run")
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Configuration invalid: {exc}")
        return 2
    configure_logging(config.log_level)
    if args.command == "validate-config":
        print("Configuration is valid")
        return 0
    if args.command == "check-company":
        try:
            exists, jobs = asyncio.run(check_greenhouse_company(args.identifier, config))
        except Exception as exc:
            print(f"Greenhouse check failed: {exc}")
            return 1
        print(f"Greenhouse board {args.identifier}: {'exists' if exists else 'not found'}")
        if exists:
            print(f"Jobs: {len(jobs)}")
            print("First 5 titles:")
            for job in jobs[:5]:
                print(f"- {job['title']}")
        return 0
    if args.command == "baseline":
        baseline(config, dry_run=args.dry_run, limit=args.limit)
    elif args.command == "run":
        run(
            config,
            mode=args.mode,
            baseline_mode=args.baseline,
            dry_run=args.dry_run,
            notify=args.notify,
            limit=args.limit,
            explain=args.explain,
        )
    elif args.command == "status":
        engine = make_engine(config.database_url)
        initialize_database(engine)
        with make_session_factory(engine)() as session:
            print(f"Stored jobs: {JobRepository(session).count()}")
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
