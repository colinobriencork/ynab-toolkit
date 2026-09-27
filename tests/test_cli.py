import pytest
from unittest.mock import MagicMock

from ynab_categorizer.__main__ import parse_args
import ynab_categorizer.__main__ as cli


def test_no_command_defaults_to_preview_categorize():
    args = parse_args([])
    assert args.command == "categorize"
    assert args.apply is False
    assert args.dry_run is False


def test_categorize_apply_and_report_are_parsed():
    args = parse_args(["categorize", "--apply", "--budget", "Home", "--report", "run.json"])
    assert args.apply is True
    assert args.budget == "Home"
    assert str(args.report) == "run.json"


def test_unknown_command_or_option_is_rejected():
    with pytest.raises(SystemExit):
        parse_args(["nonsense"])
    with pytest.raises(SystemExit):
        parse_args(["assign", "--wat"])


def test_new_mutating_commands_default_to_preview():
    assert parse_args(["correct", "--transaction", "t1", "--category", "Needs: Food"]).apply is False
    assert parse_args(["restore", "journal.json"]).apply is False


def test_categorize_wires_budget_options_and_preview(monkeypatch, tmp_path):
    calls = {}

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            calls["orchestrator"] = kwargs
            self._report = {"status": "preview"}
        def run(self):
            calls["ran"] = True

    class FakeCategorizer:
        def __init__(self, **kwargs):
            calls["categorizer"] = kwargs

    monkeypatch.setattr(cli, "load_config", lambda path: {
        "defaults": {"model": "small", "guess_model": "large", "timeout": 7,
                      "guess_timeout": 8, "merchant_rules": {"Coffee": "Food"}},
    })
    monkeypatch.setattr(cli, "_client_or_exit", lambda config: MagicMock())
    monkeypatch.setattr(cli, "_pick_budget", lambda client, selector: {"id": "b1", "name": "Home"})
    monkeypatch.setattr(cli, "Categorizer", FakeCategorizer)
    monkeypatch.setattr(cli, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli, "_build_enricher", lambda config: None)

    assert cli.main(["categorize", "--report", str(tmp_path / "run.json")]) == 0
    assert calls["categorizer"] == {"model": "small", "guess_model": "large",
                                     "timeout": 7, "guess_timeout": 8}
    assert calls["orchestrator"]["dry_run"] is True
    assert calls["orchestrator"]["budget_selector"] == "b1"
    assert calls["orchestrator"]["merchant_rules"] == {"Coffee": "Food"}


def test_help_never_loads_credentials_or_connects(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load_config', lambda *a: pytest.fail('Help must not load secrets'))
    with pytest.raises(SystemExit) as result:
        cli.main(['--help'])
    assert result.value.code == 0
    assert 'phantom-assign' in capsys.readouterr().out


def test_global_config_both_positions_and_exclusive_modes():
    assert parse_args(['--config', 'x.toml', 'assign']).config == parse_args(['assign', '--config', 'x.toml']).config
    with pytest.raises(SystemExit):
        parse_args(['categorize', '--apply', '--dry-run'])
    assert cli._status_code({'status': 'partial_failure'}) == 1


def test_assign_uses_default_budget_and_reports_failure(monkeypatch):
    selectors = []
    monkeypatch.setattr(cli, 'load_config', lambda *a: {'default_budget': 'Home'})
    client = MagicMock()
    monkeypatch.setattr(cli, '_client_or_exit', lambda *a: client)
    def pick(c, selector):
        selectors.append(selector)
        return {'id': 'b', 'name': 'Home'}
    monkeypatch.setattr(cli, '_pick_budget', pick)
    monkeypatch.setattr(cli, 'assign_funds', lambda *a, **kw: {'status': 'partial_failure'})
    assert cli.main(['assign', '--apply']) == 1
    assert selectors == ['Home']
    client.close.assert_called_once()
