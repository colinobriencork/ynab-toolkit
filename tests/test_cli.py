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
    assert parse_args(["rebalance"]).apply is False
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
                                     "timeout": 7, "guess_timeout": 8, "backend": "claude",
                                     "backend_command": None, "guess_backend_command": None}
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


@pytest.mark.parametrize('extra, expected', [
    ([], None),
    (['--model', 'example-model'], 'example-model'),
])
def test_cli_backend_switch_does_not_reuse_claude_model_defaults(monkeypatch, extra, expected):
    monkeypatch.setattr(cli, 'load_config', lambda *a: {
        'defaults': {'backend': 'claude', 'model': 'haiku', 'guess_model': 'opus'}})
    monkeypatch.setattr(cli, '_client_or_exit', lambda *a: MagicMock())
    monkeypatch.setattr(cli, '_pick_budget', lambda *a: {'id': 'b', 'name': 'Home'})
    monkeypatch.setattr(cli, '_build_enricher', lambda *a: None)
    categorizer = MagicMock()
    monkeypatch.setattr(cli, 'Categorizer', categorizer)
    monkeypatch.setattr(cli, 'Orchestrator', MagicMock())
    cli.main(['categorize', '--backend', 'codex', *extra])
    options = categorizer.call_args.kwargs
    assert options['backend'] == 'codex'
    assert options['model'] == expected
    assert options['guess_model'] is None


@pytest.mark.parametrize('command', ['assign', 'phantom-assign', 'budget-check'])
def test_budget_month_option(command):
    assert parse_args([command, '--month', '2015-10']).month == '2015-10-01'
    assert parse_args([command]).month is None


@pytest.mark.parametrize('month', ['2015-13', '2015-1', '2015-10-02', 'next'])
def test_invalid_budget_month_is_rejected(month):
    with pytest.raises(SystemExit):
        parse_args(['phantom-assign', '--month', month])


def test_phantom_cli_passes_selected_month_and_defaults_to_preview(monkeypatch):
    monkeypatch.setattr(cli, 'load_config', lambda *a: {})
    monkeypatch.setattr(cli, '_client_or_exit', lambda *a: MagicMock())
    monkeypatch.setattr(cli, '_pick_budget', lambda *a: {'id': 'b', 'name': 'Home'})
    planner = MagicMock(return_value={'status': 'preview'})
    monkeypatch.setattr(cli, 'phantom_assign', planner)
    assert cli.main(['phantom-assign', '--month', '2015-10']) == 0
    assert planner.call_args.kwargs['options']['month'] == '2015-10-01'
    assert planner.call_args.kwargs['apply'] is False


def test_rebalance_cli_wires_preview_apply_and_refill_override(monkeypatch):
    monkeypatch.setattr(cli, 'load_config', lambda *a: {})
    monkeypatch.setattr(cli, '_client_or_exit', lambda *a: MagicMock())
    monkeypatch.setattr(cli, '_pick_budget', lambda *a: {'id': 'b', 'name': 'Home'})
    planner = MagicMock(return_value={'status': 'preview'})
    monkeypatch.setattr(cli, 'rebalance', planner)
    assert cli.main(['rebalance', '--report', 'run.json']) == 0
    assert planner.call_args.kwargs['apply'] is False
    assert cli.main(['rebalance', '--apply']) == 0
    assert planner.call_args.kwargs['apply'] is True
    assert parse_args(['assign', '--refill-rebalanced']).refill_rebalanced is True


def test_setup_can_create_an_explicit_missing_settings_file(monkeypatch, tmp_path):
    setup = MagicMock()
    monkeypatch.setattr(cli, 'setup_token', setup)
    monkeypatch.setattr(cli, 'load_config', lambda *a: pytest.fail('Setup must run before config loading'))
    path = tmp_path / 'new' / 'config.toml'
    cli.main(['setup', '--config', str(path)])
    setup.assert_called_once_with(path)


def test_amazon_login_configures_missing_credentials_and_uses_own_browser_install(monkeypatch):
    import ynab_categorizer.amazon as amazon
    full = {key: 'synthetic' for key in (*cli.AMAZON_KEYS, 'AMAZON_BUDGET_NAME')}
    config = MagicMock(side_effect=[{}, full])
    setup = MagicMock()
    login = MagicMock(return_value=True)
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(cli, 'load_config', config)
    monkeypatch.setattr(cli, 'configure_amazon', setup)
    monkeypatch.setattr(cli, '_export_amazon_env', lambda *a: True)
    monkeypatch.setattr(cli.subprocess, 'run', run)
    monkeypatch.setattr(amazon, 'interactive_login', login)
    with pytest.raises(SystemExit) as result:
        cli.main(['amazon-login', '--install-browser'])
    assert result.value.code == 0
    setup.assert_called_once_with(None, {})
    run.assert_called_once_with([cli.sys.executable, '-m', 'playwright', 'install', 'chromium'])
    login.assert_called_once()
