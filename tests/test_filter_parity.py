import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import app.diagnostics as diagnostics
import app.main as main
from app.companies.registry import WorkdayTenantConfig
from app.config import AppConfig
from app.filters.evaluation import FILTER_NAMES, evaluate_filter_decisions
from app.scouts.greenhouse import normalize_greenhouse_job
from app.scouts.workday import _list_job, normalize_workday_job


FIXTURES = Path(__file__).parent / "fixtures"
RUN_TIME = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def test_run_and_explain_paths_return_identical_per_filter_decisions(monkeypatch):
    workday_list = json.loads((FIXTURES / "workday_walmart_list.json").read_text())
    workday_details = json.loads((FIXTURES / "workday_walmart_details.json").read_text())
    greenhouse_raw = json.loads((FIXTURES / "greenhouse_parity_job.json").read_text())
    tenant = WorkdayTenantConfig.model_validate({
        "host": "walmart.wd504.myworkdayjobs.com",
        "tenant": "walmart",
        "site": "WalmartExternal",
        "country_facet_id": "facet",
        "assume_country": "IN",
    })
    workday_item = _list_job(workday_list["jobPostings"][0], "Walmart", tenant)
    jobs = [
        normalize_workday_job(workday_item, workday_details, tenant, run_time=RUN_TIME),
        normalize_greenhouse_job(greenhouse_raw, "Example Greenhouse Co", run_time=RUN_TIME),
    ]
    config = AppConfig(filters={
        "keywords": ["software engineer"],
        "excluded_keywords": ["recruiter"],
        "seniority_titles": ["senior", "staff", "manager"],
        "unknown_location_policy": "strict",
        "max_min_years": 2,
        "employment_types": ["full-time"],
        "max_age_days": 7,
    })

    run_trace = {}
    explain_filter_trace = {}
    explain_report_trace = {}

    def recorder(trace):
        def evaluate(job, cfg, **kwargs):
            individual, pipeline, first = evaluate_filter_decisions(job, cfg, **kwargs)
            trace[job["source_id"]] = {
                "individual": {
                    name: (decision.status.value, decision.reason)
                    for name, decision in individual.items()
                },
                "pipeline": {
                    name: (decision.status.value, decision.reason)
                    for name, decision in pipeline.items()
                },
                "first_rejection": first,
            }
            return individual, pipeline, first
        return evaluate

    monkeypatch.setattr(main, "evaluate_filter_decisions", recorder(run_trace))
    main.filter_jobs(copy.deepcopy(jobs), config, run_time=RUN_TIME)

    explain_jobs = copy.deepcopy(jobs)
    monkeypatch.setattr(main, "evaluate_filter_decisions", recorder(explain_filter_trace))
    monkeypatch.setattr(diagnostics, "evaluate_filter_decisions", recorder(explain_report_trace))
    main.filter_jobs(
        explain_jobs, config, preserve_workday_context=True, run_time=RUN_TIME
    )
    # discover(include_rejected=True) returns the prepared job list, including
    # rejected jobs, after filter_jobs has evaluated and annotated it.
    diagnostics.format_filter_diagnostics(explain_jobs, config, run_time=RUN_TIME)

    assert set(run_trace) == {job["source_id"] for job in jobs}
    assert run_trace == explain_filter_trace == explain_report_trace
    for result in run_trace.values():
        assert set(result["individual"]) == set(FILTER_NAMES) | {"excluded keyword"}

