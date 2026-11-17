from pathlib import Path

import httpx
import pytest

import app.cli as cli
from app.companies.registry import load_registry, verified_workday_entries


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _install_live_config_http_fixture(monkeypatch, tmp_path):
    registry_path = PROJECT_ROOT / "data" / "company_registry.json"
    registry = load_registry(registry_path)
    walmart = next(entry for entry in verified_workday_entries(registry) if entry.company_name == "Walmart")
    assert walmart.workday is not None and walmart.workday.assume_country == "IN"

    list_jobs = [
        ("loc-2", "Software Engineer — Two Locations", "2 Locations", None),
        ("loc-in", "Software Engineer — Bangalore", "IN KA BANGALORE Home Office PW II", "IN"),
        ("loc-ie", "Software Engineer — Dublin", "Dublin", "IE"),
    ]
    details_by_id = {}
    postings = []
    for req_id, title, location, country_code in list_jobs:
        postings.append({
            "title": title,
            "externalPath": f"/job/locations/{req_id}",
            "locationsText": location,
            "postedOn": "Posted Today",
            "bulletFields": [req_id],
        })
        info = {
            "jobReqId": req_id,
            "title": title,
            "jobDescription": "Minimum Qualifications: 1 year of software engineering experience.",
            "location": location,
            "externalUrl": f"https://walmart.example/jobs/{req_id}",
            "startDate": None,
            "timeType": "Full time",
        }
        if country_code is not None:
            info["jobRequisitionLocation"] = {"country": {"alpha2Code": country_code}}
        details_by_id[req_id] = {"jobPostingInfo": info}

    requests = []

    def handler(request):
        requests.append((request.method, request.url.host, request.url.path))
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}, request=request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"total": len(postings), "jobPostings": postings, "userAuthenticated": False},
                request=request,
            )
        req_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=details_by_id[req_id], request=request)

    real_async_client = httpx.AsyncClient

    def mock_transport_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_transport_client)
    # Keep the real config loader and YAML, while isolating its optional
    # database read so tenant throttling cannot suppress the HTTP fixture.
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli-e2e.db'}")
    return requests


def test_real_cli_explain_uses_walmart_assumed_country_for_locations(monkeypatch, capsys, tmp_path):
    requests = _install_live_config_http_fixture(monkeypatch, tmp_path)
    assert cli.main(["run", "--dry-run", "--explain"]) == 0
    captured = capsys.readouterr()
    explain_output = captured.out
    assert any(method == "POST" and path.endswith("/jobs") and host == "walmart.wd504.myworkdayjobs.com"
               for method, host, path in requests), f"Expected mocked Workday list request; saw {requests!r}; stderr={captured.err}"
    assert "location: PASS=1 UNCERTAIN=1 REJECT=1" in explain_output
    assert (
        "Software Engineer — Two Locations — Walmart — 2 Locations — UNCERTAIN: "
        "Location lists multiple unspecified locations."
    ) in explain_output
    assert (
        "Software Engineer — Bangalore — Walmart — IN KA BANGALORE Home Office PW II — PASS: "
        "Workday requisition location country is India."
    ) in explain_output
    assert "Software Engineer — Dublin" in explain_output
    assert "Software Engineer — Dublin — Walmart — Dublin — REJECT:" in explain_output
    unrecognized = explain_output.split("Unrecognized locations (top 25):\n", 1)[1].split("\n\n", 1)[0]
    assert "2 Locations" not in unrecognized
    assert any(method == "GET" and path.startswith("/v1/boards/mongodb/jobs") for method, host, path in requests)


def test_real_cli_dry_run_keeps_assumed_country_jobs_and_rejects_dublin(monkeypatch, capsys, caplog, tmp_path):
    requests = _install_live_config_http_fixture(monkeypatch, tmp_path)
    caplog.set_level("INFO", logger="app.main")
    assert cli.main(["run", "--dry-run"]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert any(method == "POST" and path.endswith("/jobs") and host == "walmart.wd504.myworkdayjobs.com"
               for method, host, path in requests), f"Expected mocked Workday list request; saw {requests!r}; stderr={captured.err}"
    assert "Software Engineer — Two Locations" in output
    assert "Software Engineer — Bangalore" in output
    assert "Software Engineer — Dublin" not in output
