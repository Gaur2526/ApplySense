import httpx
import pytest

from app import cli
from app.config import AppConfig


def test_run_cli_passes_mode_baseline_dry_run_and_limit(monkeypatch):
    config = AppConfig()
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "run", lambda config, **kwargs: captured.update(kwargs))

    assert cli.main([
        "run", "--mode", "qualified", "--baseline", "--dry-run", "--limit", "7",
    ]) == 0
    assert captured == {
        "mode": "qualified", "baseline_mode": True, "dry_run": True,
        "notify": False, "limit": 7, "explain": False, "force": False,
    }


def test_run_cli_passes_force_for_workday_interval(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "run", lambda _config, **kwargs: captured.update(kwargs))
    assert cli.main(["run", "--force"]) == 0
    assert captured["force"] is True


def test_baseline_subcommand_remains_available(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "baseline", lambda _config, **kwargs: captured.update(kwargs))

    assert cli.main(["baseline", "--dry-run", "--limit", "3"]) == 0
    assert captured == {"dry_run": True, "limit": 3}


def test_check_company_reports_count_and_first_five_without_database(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "make_engine", lambda *_args, **_kwargs: pytest.fail("must not open database"))

    class ScoutStub:
        def __init__(self, **_kwargs):
            pass

        async def fetch_jobs(self, identifier):
            assert identifier == "synthetic_test_board"
            return [{"title": f"Role {index}"} for index in range(1, 8)]

        async def close(self):
            pass

    monkeypatch.setattr(cli, "GreenhouseScout", ScoutStub)
    assert cli.main(["check-company", "synthetic_test_board", "--source", "greenhouse"]) == 0
    output = capsys.readouterr().out
    assert "Greenhouse board synthetic_test_board: exists" in output
    assert "Jobs: 7" in output
    assert all(f"Role {index}" in output for index in range(1, 6))
    assert "Role 6" not in output


def test_check_company_reports_missing_board_on_404(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)

    class ScoutStub:
        def __init__(self, **_kwargs):
            pass

        async def fetch_jobs(self, _identifier):
            request = httpx.Request("GET", "https://boards-api.greenhouse.io/test")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

        async def close(self):
            pass

    monkeypatch.setattr(cli, "GreenhouseScout", ScoutStub)
    assert cli.main(["check-company", "synthetic_test_board", "--source", "greenhouse"]) == 0
    assert "Greenhouse board synthetic_test_board: not found" in capsys.readouterr().out


def test_check_company_requires_greenhouse_source():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["check-company", "id"])


@pytest.mark.parametrize("yaml_text", ["filters:\n  keywords: []\n", "filters: {}\n", "{}\n"])
def test_validate_config_rejects_missing_or_empty_keywords(tmp_path, capsys, yaml_text):
    config = tmp_path / "empty-keywords.yaml"
    config.write_text(yaml_text, encoding="utf-8")
    assert cli.main(["--config", str(config), "validate-config"]) == 2
    assert "Configuration invalid" in capsys.readouterr().out


def test_run_cli_passes_explain_flag_only_with_dry_run(monkeypatch):
    config = AppConfig()
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "run", lambda _config, **kwargs: captured.update(kwargs))
    assert cli.main(["run", "--dry-run", "--explain"]) == 0
    assert captured["dry_run"] is True
    assert captured["explain"] is True


def test_run_cli_rejects_explain_without_dry_run():
    with pytest.raises(SystemExit):
        cli.main(["run", "--explain"])
