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
])
def test_invalid_options_fail_before_connection(tmp_path, body):
    path = tmp_path / 'config.toml'
    path.write_text(body)
    with pytest.raises(ValueError):
        load_config(path, env_path=tmp_path / 'missing.env')
