# ApplySense — Phase 1

ApplySense discovers open roles from verified Greenhouse entries in the company registry, normalizes and filters them, stores canonical job URLs in SQLite by default, and can optionally notify a Telegram chat. Phase 1 contains configuration, persistence, one source integration, filtering, deduplication, notification plumbing, CLI commands, logging, and unit tests.

**Privacy:** This repository must stay private. Do not publish it or commit personal data, `.env`, `resume.md`, database files, or real service credentials.

## Setup

Python 3.12 is supported. Install dependencies with `python -m pip install -r requirements.txt`. Copy `.env.example` to `.env`, then edit `config/config.yaml` for runtime settings and filters. Add target companies to `data/company_registry.json`. YAML values are defaults; supported environment variables override database URL, log level, and Telegram values.

```yaml
filters:
  keywords: [python, backend]
  excluded_keywords: [staff]
  seniority_titles: [senior, sr, lead, staff, principal, manager, head, architect, director, vp, "vice president", chief]
  preferred_cities: [Bengaluru, Pune]
  max_min_years: 2
  employment_types: [full-time]
telegram:
  enabled: false
```

Filters return PASS, REJECT, or UNCERTAIN with a reason. Rejected jobs are removed; uncertain jobs remain stored with their reasons. Experience requirements above max_min_years are rejected; unstated requirements are uncertain. Remote roles pass when India-compatible or unrestricted and are rejected when restricted to another region. Preferred cities only affect ordering. Contract and internship roles require the matching type in employment_types. Greenhouse does not provide employment type in its public board feed, so that filter returns uncertain for Greenhouse jobs. Seniority terms match whole words in the title.

## CLI

Run commands from the repository root using the active virtual environment:

```sh
python -m app.cli validate-config
python -m app.cli run --mode discovery --dry-run --limit 20
python -m app.cli run --mode qualified --baseline
python -m app.cli baseline --dry-run
python -m app.cli baseline
python -m app.cli run --dry-run
python -m app.cli run
python -m app.cli check-company greenhouse-board-token --source greenhouse
python -m app.cli status
```

baseline records currently matching jobs as pre-existing; run --baseline is an equivalent flag. run --mode discovery sends new uncertain jobs with a short requirements-check note when --notify is supplied. In qualified mode uncertain jobs remain stored for a later matcher. --limit N caps the matching jobs processed. check-company queries the named public board, prints its job count and up to five titles, and does not write to the database. Telegram sends require TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, telegram.enabled: true (or TELEGRAM_ENABLED=true), and the explicit --notify flag. --dry-run never writes to the database or contacts Telegram.

Greenhouse discovery reads only registry companies whose ATS is `greenhouse`, whose `status` is `verified`, and whose `board_identifier` is non-empty. The JSON registry schema is extensible and keeps companies with unknown or unsupported ATS providers. Do not put unverified board IDs into runtime configuration; pending companies are skipped by the Greenhouse scout.

SQLite defaults to `./applysense.db`. Set `DATABASE_URL` to use another SQLAlchemy-supported database URL. The company registry path defaults to `data/company_registry.json` and can be changed with `company_registry_path` in `config/config.yaml`.

## First live run

Run these commands in order from the repository root. Before the last command, edit `.env` with the Telegram credentials and set `telegram.enabled: true` in `config/config.yaml` if you want live notifications.

```sh
cp .env.example .env
python -m app.cli validate-config
python -m app.cli check-company '<identifier>' --source greenhouse
python -m app.cli run --dry-run
python -m app.cli run --baseline
python -m app.cli run --notify
```

1. `cp` creates `.env` and prints nothing when it succeeds. Edit the file to set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; credentials are required only for Telegram sends.
2. `validate-config` prints `Configuration is valid`. Use a verified registry identifier in the next command.
3. `check-company` prints whether the Greenhouse board exists, its job count, and up to five titles. A successful check says the board exists; a 404 reports that it was not found.
4. `run --dry-run` prints matching job summaries and leaves the database and Telegram untouched. Seeing matching jobs without a fetch/configuration error is a successful dry run.
5. `run --baseline` fetches and stores currently matching roles as pre-existing, then prints how many it stored and that notifications were skipped. This sets the notification starting point.
6. `run --notify` performs a normal discovery run and sends only eligible unsent jobs. Since the baseline just stored current roles, zero new notifications is a normal successful first result; later runs notify new roles. A successful send is recorded only after Telegram confirms it.

The `<identifier>` placeholder is not a literal board token. Substitute a verified Greenhouse board identifier from `data/company_registry.json`.

## Phase 1 completion

| Phase 1 item | Status | Implementation |
| --- | --- | --- |
| Configuration and validation | DONE | `app/config.py`, `config/config.yaml` |
| SQLAlchemy database setup and persistence | DONE | `app/database/connection.py`, `app/database/models.py`, `app/database/repository.py` |
| Greenhouse API scout, retries, timeouts, and bounded concurrency | DONE | `app/scouts/greenhouse.py`, `tests/test_greenhouse.py` |
| Normalized Pydantic job schema and Greenhouse normalization | DONE | `app/schemas/job.py`, `app/scouts/greenhouse.py` |
| Keyword, location, experience, seniority, and employment filters | DONE | `app/filters/` |
| URL normalization, hashes, dedupe, and notification claims | DONE | `app/utils/urls.py`, `app/utils/hashing.py`, `app/database/repository.py` |
| Telegram HTML formatting, safe truncation, cap, retries, and state handling | DONE | `app/notifications/`, `app/main.py` |
| CLI, baseline, dry-run, and company check | DONE | `app/cli.py`, `app/main.py` |
| Logging and secret redaction | DONE | `app/utils/logger.py` |
| Automated test coverage | DONE | `tests/` |
| Company registry coverage by verified Greenhouse boards | PARTIAL | `data/company_registry.json`, `app/companies/registry.py`; companies without verified identifiers remain pending and are not fetched |
| Live Telegram delivery verification | PARTIAL | `app/notifications/telegram.py`; behavior is covered with mocked HTTP, but no live message was sent during verification |

## Verification

Run tests with `python -m pytest`. Configuration can be checked without network access using `python -m app.cli validate-config`.
