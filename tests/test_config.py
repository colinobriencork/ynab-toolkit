import pytest
from pathlib import Path

from ynab_categorizer.config import budget_options, load_config


def test_toml_and_env_are_loaded_with_process_env_precedence(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[defaults]\nwants_mode = "refill"\n')
    env_path = tmp_path / ".env"
    env_path.write_text("GMAIL_ADDRESS=file@example.test\n")
    monkeypatch.setenv("GMAIL_ADDRESS", "process@example.test")

    config = load_config(config_path, env_path)

    assert config["defaults"]["wants_mode"] == "refill"
    assert config["GMAIL_ADDRESS"] == "process@example.test"


def test_budget_options_merge_by_id_or_exact_name():
    config = {
        "defaults": {"wants_mode": "refill", "priority_groups": ["Needs"]},
        "budgets": {"b1": {"wants_mode": "accumulate"}},
    }
    assert budget_options(config, {"id": "b1", "name": "Home"}) == {
        "wants_mode": "accumulate", "priority_groups": ["Needs"], "currency_decimal_digits": 2
    }


@pytest.mark.parametrize('body', [
    '[defaults]\nwants_overrides = {Dining = -1}',
    '[defaults]\nmerchant_rules = {Coffee = 100}',
    '[defaults]\nwarn_ratio = 2',
    '[defaults]\npriority_groups = ["Bills", "Bills"]',
    '[defaults]\nmonthly_income = 12.5',
    '[defaults]\nrebalance_keep = {Food = -1}',
    '[defaults]\nrebalance_keep = {Food = true}',
    '[defaults]\nrebalance_protected_categories = "Food"',
    '[defaults]\nrebalance_last_categories = [2]',
])
def test_invalid_options_fail_before_connection(tmp_path, body):
    path = tmp_path / 'config.toml'
    path.write_text(body)
    with pytest.raises(ValueError):
        load_config(path, env_path=tmp_path / 'missing.env')


@pytest.mark.parametrize('body', [
    'backend = "unknown"',
    'backend = ["codex"]',
    'backend_command = "wrapper --model local"',
    'backend_command = []',
    'guess_backend_command = [42]',
])
def test_invalid_backend_options_fail_during_configuration(tmp_path, body):
    path = tmp_path / 'config.toml'
    path.write_text('[defaults]\n' + body)
    with pytest.raises(ValueError):
        load_config(path, env_path=tmp_path / 'missing.env')


def test_budget_backend_override_drops_inherited_model_aliases():
    config = {
        'defaults': {'backend': 'claude', 'model': 'haiku', 'guess_model': 'opus'},
        'budgets': {'Home': {'backend': 'codex'}},
    }
    options = budget_options(config, {'id': 'b', 'name': 'Home'})
    assert options['backend'] == 'codex'
    assert 'model' not in options and 'guess_model' not in options
    config['budgets']['Home']['model'] = 'example-model'
    assert budget_options(config, {'name': 'Home'})['model'] == 'example-model'


def test_custom_command_configuration_round_trip(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[defaults]\nbackend = "command"\n'
                    'backend_command = ["wrapper", "--model", "{model}"]\nmodel = "local"\n')
    options = budget_options(load_config(path, env_path=tmp_path / 'missing.env'), {'name': 'Home'})
    assert options['backend_command'] == ['wrapper', '--model', '{model}']


def test_installed_config_is_independent_of_working_directory(tmp_path, monkeypatch):
    import ynab_categorizer.config as module
    monkeypatch.delenv('YNAB_TOOLKIT_CONFIG', raising=False)
    monkeypatch.delenv('YNAB_API_TOKEN', raising=False)
    monkeypatch.setattr(module, 'PROJECT_ROOT', tmp_path / 'site-packages')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'settings'))
    settings = tmp_path / 'settings' / 'ynab-toolkit'
    settings.mkdir(parents=True)
    (settings / 'config.toml').write_text('default_budget = "Example"\n')
    (settings / '.env').write_text('YNAB_API_TOKEN=synthetic-user-token\n')
    unrelated = tmp_path / 'elsewhere'
    unrelated.mkdir()
    (unrelated / '.env').write_text('YNAB_API_TOKEN=wrong-project-token\n')
    monkeypatch.chdir(unrelated)
    assert load_config()['YNAB_API_TOKEN'] == 'synthetic-user-token'
    assert load_config()['default_budget'] == 'Example'


def test_explicit_config_uses_only_its_sibling_credentials(tmp_path, monkeypatch):
    import ynab_categorizer.config as module
    monkeypatch.delenv('YNAB_API_TOKEN', raising=False)
    legacy = tmp_path / 'source'
    legacy.mkdir()
    (legacy / '.env').write_text('YNAB_API_TOKEN=wrong-source-token\n')
    monkeypatch.setattr(module, 'PROJECT_ROOT', legacy)
    selected = tmp_path / 'selected'
    selected.mkdir()
    (selected / 'config.toml').write_text('')
    (selected / '.env').write_text('YNAB_API_TOKEN=synthetic-selected-token\n')
    monkeypatch.setenv('YNAB_TOOLKIT_CONFIG', str(selected / 'config.toml'))
    assert load_config()['YNAB_API_TOKEN'] == 'synthetic-selected-token'
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / 'missing.toml')


def test_source_checkout_keeps_legacy_settings(tmp_path, monkeypatch):
    import ynab_categorizer.config as module
    monkeypatch.delenv('YNAB_TOOLKIT_CONFIG', raising=False)
    monkeypatch.delenv('YNAB_API_TOKEN', raising=False)
    monkeypatch.setattr(module, 'PROJECT_ROOT', tmp_path)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "ynab-toolkit"\n')
    (tmp_path / '.env').write_text('YNAB_API_TOKEN=synthetic-source-token\n')
    assert load_config()['YNAB_API_TOKEN'] == 'synthetic-source-token'
