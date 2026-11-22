# ApplySense: project state (2026-11-22)

Personal job-discovery assistant (Python 3.12, repo ~/Documents/ApplySense, private GitHub, .venv). It finds new jobs from company hiring systems, filters them, and alerts me on Telegram. I apply myself. It NEVER applies or messages recruiters automatically.

## Rules for every agent
- No network in agent sandboxes: make no live requests; I run live commands.
- Never print, open or commit secrets (.env, tokens, API keys, resume.md, *.db). Telegram token is a SecretStr.
- Do not scrape LinkedIn, Naukri, Indeed or sites whose terms forbid it. Respect robots.txt and rate limits. Never copy browser identity headers or cookies; never evade blocks.
- One task per branch. Full suite must pass. Schema changes bump the schema version (now 4); only the migrations task may change models.
- Keep filters tri-state (PASS/UNCERTAIN/REJECT). run, --dry-run and --explain share one evaluation function.
- Always write end-to-end tests through the real CLI (mock only HTTP): unit tests alone missed three real bugs.
- Report: files changed, test results, limitations; wait for approval.

## Built (322 tests pass)
- Config (config/config.yaml, .env), SQLAlchemy models and repository, schema version check.
- Scouts: Greenhouse; generic Workday (per-tenant config, two-stage list then details, 7-day age filter at list stage, apply-by dates, rate-limit handling with Retry-After, circuit breaker, cooldown, adaptive slowdown, resumable scans, pending-details memory).
- Filters: title-only whole-word keywords and excludes, seniority (incl. SMTS/LMTS), location (strict unknown policy, Workday assume_country from registry), experience parser (headings, alternatives, boilerplate ignored), employment type, age (max_age_days 7).
- Dedupe (URL hash; company+title+location across different sources only), atomic notification claims with lease and retries, runs table.
- Telegram: HTML-escaped messages, 4096 truncation, per-run cap 20 with queue, 429 handling, token redaction, age and apply-by lines.
- CLI: run (--notify --dry-run --explain --baseline --force --limit), baseline, check-company, check-workday, validate-config, status.
- Verified sources: MongoDB (Greenhouse board "mongodb"), Walmart (Workday walmart.wd504, site WalmartExternal), Salesforce (Workday salesforce.wd12, site External_Career_Site, custom facet key). The other ~44 registry companies are pending_verification.
- Live result: real alerts received on Telegram (Walmart SE III, Salesforce MTS).

## Key settings
max_min_years 2 (reject only if the MINIMUM required years exceeds it); max_age_days 7; employment_types [full-time]; unknown_location_policy strict; per_run_cap 20; India city list extended; Walmart pace 5s; Salesforce min interval 6h, retries 2.

## Known issues
1. Walmart list scan read only 2 pages on 2026-11-22 and reported ok (earlier ~11-15 pages). Investigate first.
2. Salesforce returns 429 even on its first request; slower pacing does not help. Treat as best-effort (poll rarely; use its own alerts as a backup). Do not evade.
3. No migrations: every schema bump forces deleting applysense.db and loses history.
4. Nothing is scheduled; I run `python -m app.cli run --notify` by hand.

## Roadmap, in order
1. Fix issue 1, then add pages-read X of Y to the run summary.
2. Versioned migrations (keep history across schema changes).
3. Daily scheduling on my Mac (launchd or cron).
4. Lever and Ashby scouts (public APIs; verify endpoints against docs).
5. Generic custom-feed adapter (config-driven JSON feeds) and a detect-ats command; Accenture (findjobs feed), then IBM.
6. AI matcher (Claude Haiku 5.5, model id claude-haiku-5-5): structured JSON, Pydantic validation, cache by job+resume+model+prompt hash, per-run and daily call caps, untrusted job text, no tools. Needs ~$5 API credit.
7. Resume tailoring with a deterministic grounding check (never invent skills); drafts only.
8. Application tracker and Telegram commands (poll getUpdates each run, allowed user id only), follow-up reminders.
9. Optional LangGraph wrapper.
10. Frontend last: a hosted static overview with sample data only; real use means running locally with your own API key. No login.

## Samples and fixtures
Raw captures live outside the repo in ~/applysense-samples/. Only trimmed, name-free fixtures go in tests/fixtures/. The Accenture findjobs sample was seen but no adapter exists.

## Status log (agents append one line per task)