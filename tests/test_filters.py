from app.filters.decision import FilterStatus
from app.filters.employment_filter import matches_employment
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.keyword_filter import matches_keywords
from app.filters.location_filter import matches_location, preferred_city_rank
from app.filters.seniority_filter import matches_seniority
from app.config import AppConfig
from app.database.connection import initialize_database, make_engine, make_session_factory
from app.database.repository import JobRepository
from app.main import filter_jobs, run
from app.notifications.formatting import format_job


JOB = {
    "title": "Software Engineer",
    "company": "Acme",
    "location": "Bengaluru, India",
    "description": "Requires 2 years Python experience",
    "employment_type": "Full-Time",
}


def test_keyword_filter_returns_tri_state_decisions():
    assert matches_keywords(JOB, ["python"]).status is FilterStatus.PASS
    assert matches_keywords(JOB, ["designer"]).status is FilterStatus.REJECT
    assert matches_keywords(JOB, ["python"], ["acme"]).status is FilterStatus.REJECT
    assert matches_keywords({}, ["python"]).status is FilterStatus.UNCERTAIN
    assert matches_keywords(JOB, []).reason


def test_experience_parses_required_patterns_and_ranges():
    assert parse_experience_requirement("3+ years required") == 3
    assert parse_experience_requirement("2-4 years experience") == 2
    assert parse_experience_requirement("1-3 years experience") == 1
    assert parse_experience_requirement("minimum 3 years") == 3
    assert parse_experience_requirement("at least 2 years") == 2
    assert parse_experience_requirement("0-1 year") == 0
    assert parse_experience_requirement("0-2 years") == 0
    assert parse_experience_requirement("Fresher role") == 0
    assert parse_experience_requirement("No stated requirement") is None


def test_experience_rejects_only_minimum_above_configured_limit():
    assert matches_experience({"description": "2-4 years"}).status is FilterStatus.PASS
    assert matches_experience({"description": "1-3 years"}).status is FilterStatus.PASS
    assert matches_experience({"description": "3+ years"}).status is FilterStatus.REJECT
    assert matches_experience({"description": "3+ years"}, max_min_years=3).status is FilterStatus.PASS
    unknown = matches_experience({"description": "Experience not stated"})
    assert unknown.status is FilterStatus.UNCERTAIN
    assert unknown.reason


def test_experience_ignores_company_boilerplate_year_mentions():
    description = (
        "Over 25 years of experience serving clients. Founded 30 years ago. "
        "Our 15-year journey continues. For 10 years we have supported customers."
    )
    assert parse_experience_requirement(description) is None
    decision = matches_experience({"description": description})
    assert decision.status is FilterStatus.UNCERTAIN


def test_experience_requirement_section_takes_precedence_over_boilerplate():
    description = (
        "Over 25 years of experience serving clients.\n"
        "Requirements:\n2-4 years of relevant experience."
    )
    assert parse_experience_requirement(description) == 2
    assert matches_experience({"description": description}).status is FilterStatus.PASS


def test_experience_conflicting_requirements_are_uncertain():
    decision = matches_experience({
        "description": "Requirements: 2+ years of experience. Minimum 4 years required."
    })
    assert decision.status is FilterStatus.UNCERTAIN
    assert "Conflicting" in decision.reason


def test_experience_required_range_and_plus_edge_cases():
    assert matches_experience({"description": "Experience: 1 to 3 years"}).status is FilterStatus.PASS
    assert matches_experience({"description": "3+ years of experience"}).status is FilterStatus.REJECT
    assert matches_experience({"description": "2-4 years of relevant experience"}).status is FilterStatus.PASS
    assert matches_experience({"description": "No experience requirement stated"}).status is FilterStatus.UNCERTAIN


def test_real_description_experience_regressions():
    assert matches_experience({"description": "Years of Experience:1 to 3 years"}).status is FilterStatus.PASS
    assert matches_experience({"description": "Years of Experience:0 to 1 years"}).status is FilterStatus.PASS
    history = (
        "For over 25 years we have served customers, supporting 784,000 people "
        "in 120 countries. Years of Experience: 1 to 3 Years."
    )
    assert matches_experience({"description": history}).status is FilterStatus.PASS
    assert matches_experience({"description": "Experience: 0-2 years"}).status is FilterStatus.PASS
    assert matches_experience({"description": "5+ years of experience in Java"}).status is FilterStatus.REJECT


def test_location_remote_eligibility_and_unknown_cases():
    assert matches_location({"location": "Remote - US only"}).status is FilterStatus.REJECT
    assert matches_location({"location": "Remote, Canada"}).status is FilterStatus.REJECT
    assert matches_location({"location": "Remote - EMEA"}).status is FilterStatus.REJECT
    assert matches_location({"location": "Remote - APAC"}).status is FilterStatus.PASS
    assert matches_location({"location": "Remote (India)"}).status is FilterStatus.PASS
    assert matches_location({"location": "Anywhere"}).status is FilterStatus.PASS
    assert matches_location({"location": "Bengaluru, India"}).status is FilterStatus.PASS
    assert matches_location({"location": "London, UK"}).status is FilterStatus.REJECT
    assert matches_location({"location": "Seattle"}).status is FilterStatus.REJECT
    assert matches_location({"location": ""}).status is FilterStatus.UNCERTAIN
    assert matches_location({"location": "", "description": "Remote (India)"}).status is FilterStatus.UNCERTAIN
    assert matches_location({"location": "Unknown"}).status is FilterStatus.UNCERTAIN
    assert matches_location({}).status is FilterStatus.UNCERTAIN


def test_preferred_city_only_affects_ordering():
    preferred = ["Bengaluru", "Pune"]
    assert preferred_city_rank({"location": "Pune, India"}, preferred) == 1
    assert preferred_city_rank({"location": "Remote - India"}, preferred) == 2
    assert matches_location({"location": "Remote - India"}).status is FilterStatus.PASS


def test_seniority_title_terms_are_whole_words():
    terms = ["senior", "sr", "lead", "staff", "principal", "manager", "head", "architect", "director", "vp"]
    assert matches_seniority({"title": "Senior Software Engineer"}, terms).status is FilterStatus.REJECT
    assert matches_seniority({"title": "Sr. Backend Engineer"}, terms).status is FilterStatus.REJECT
    assert matches_seniority({"title": "Seniority Compliance Analyst"}, terms).status is FilterStatus.PASS
    assert matches_seniority({"title": "Software Engineer"}, terms).status is FilterStatus.PASS
    assert matches_seniority({"title": ""}, terms).status is FilterStatus.UNCERTAIN


def test_employment_type_unknown_is_uncertain_and_contract_internship_require_opt_in():
    assert matches_employment({"employment_type": "Full-Time"}, ["full-time"]).status is FilterStatus.PASS
    assert matches_employment({"employment_type": ""}, ["full-time"]).status is FilterStatus.UNCERTAIN
    assert matches_employment({"employment_type": "unspecified"}, ["full-time"]).status is FilterStatus.UNCERTAIN
    assert matches_employment({"employment_type": "Contract"}, ["full-time"]).status is FilterStatus.REJECT
    assert matches_employment({"employment_type": "Internship"}, ["full-time"]).status is FilterStatus.REJECT
    assert matches_employment({"employment_type": "Contract"}, ["full-time", "contract"]).status is FilterStatus.PASS
    assert matches_employment({"employment_type": "Internship"}, ["full-time", "internship"]).status is FilterStatus.PASS


def test_uncertain_jobs_continue_and_keep_reasons_in_storage(tmp_path, monkeypatch):
    job = {
        "source": "greenhouse", "source_id": "uncertain-1", "company": "Example",
        "title": "Software Engineer", "location": "Remote", "employment_type": "",
        "description": "Requirements vary by team.", "canonical_url": "https://example.com/uncertain",
        "content_hash": "a" * 64, "experience_requirement": None,
    }
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'step1.db'}")
    filtered = filter_jobs([job], config)
    assert len(filtered) == 1
    assert filtered[0]["uncertainty_reasons"]

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=len(filtered), filtered=len(filtered), errors=0)
        return filtered

    monkeypatch.setattr("app.main.discover", fake_discover)
    assert run(config) == 1
    engine = make_engine(config.database_url)
    initialize_database(engine)
    with make_session_factory(engine)() as session:
        stored = JobRepository(session).get(job["canonical_url"])
        assert stored is not None
        assert stored.experience_requirement is None
        assert stored.discovered_at is not None
        assert stored.uncertainty_reasons == filtered[0]["uncertainty_reasons"]
    engine.dispose()


def test_discovery_message_has_short_uncertainty_note():
    message = format_job({
        "title": "Engineer", "company": "Example", "location": "Unknown",
        "canonical_url": "https://example.com/job", "uncertainty_reasons": ["Location is unknown."],
    })
    assert "Check requirements: some role details are uncertain." in message
