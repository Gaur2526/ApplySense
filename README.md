# ApplySense — Phase 1

ApplySense discovers open roles from verified Greenhouse entries in the company registry, normalizes and filters them, stores canonical job URLs in SQLite by default, and can optionally notify a Telegram chat. Phase 1 contains configuration, persistence, one source integration, filtering, deduplication, notification plumbing, CLI commands, logging, and unit tests.

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

## Verification

Run tests with `python -m pytest`. Configuration can be checked without network access using `python -m app.cli validate-config`.
