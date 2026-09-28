"""Setup must persist usable settings without exposing or damaging secrets."""
import os

import pytest

from ynab_categorizer import setup
from ynab_categorizer.config import load_config, _read_env


@pytest.fixture(autouse=True)
def isolated_setup(monkeypatch):
    monkeypatch.setattr(setup.webbrowser, 'open', lambda *args: True)
    monkeypatch.delenv('YNAB_TOOLKIT_CONFIG', raising=False)
    monkeypatch.delenv('YNAB_API_TOKEN', raising=False)


def test_first_setup_creates_usable_settings_without_echoing_token(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'private' / 'config.toml'
    monkeypatch.setattr(setup, 'getpass', lambda *args: 'synthetic-token')
    answers = iter(['Example Budget', 'codex'])
    monkeypatch.setattr('builtins.input', lambda *args: next(answers))
    setup.setup_token(path)
    config = load_config(path)
    assert config['YNAB_API_TOKEN'] == 'synthetic-token'
    assert config['default_budget'] == 'Example Budget'
    assert config['defaults']['backend'] == 'codex'
    assert 'synthetic-token' not in capsys.readouterr().out
    if os.name != 'nt':
        assert (path.parent / '.env').stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o600


def test_setup_preserves_existing_options_and_unrelated_credentials(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    original = '[defaults]\nbackend="codex"\nrebalance_keep={Transport=5000}\n'
    path.write_text(original)
    env_file = tmp_path / '.env'
    env_file.write_text('# keep this comment\nAMAZON_PASSWORD="example=quoted"\nYNAB_API_TOKEN=old\n')
    monkeypatch.setattr(setup, 'getpass', lambda *args: 'replacement')
    setup.setup_token(path)
    assert path.read_text() == original
    assert '# keep this comment\nAMAZON_PASSWORD="example=quoted"\n' in env_file.read_text()
    assert _read_env(env_file)['YNAB_API_TOKEN'] == 'replacement'


@pytest.mark.parametrize('token', ['', 'bad\nvalue'])
def test_invalid_new_token_does_not_create_files(tmp_path, monkeypatch, token):
    monkeypatch.setattr(setup, 'getpass', lambda *args: token)
    with pytest.raises(SystemExit):
        setup.setup_token(tmp_path / 'new' / 'config.toml')
    assert not (tmp_path / 'new').exists()


def test_amazon_credentials_round_trip_and_preserve_ynab(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.toml'
    path.write_text('')
    (tmp_path / '.env').write_text('YNAB_API_TOKEN=synthetic-token\n')
    password = ' example "quotes" \\ slash = end\' '
    secrets = iter([password, 'abcd efgh ijkl mnop'])
    answers = iter(['example@example.test', '', 'Example Budget'])
    monkeypatch.setattr(setup, 'getpass', lambda *args: next(secrets))
    monkeypatch.setattr('builtins.input', lambda *args: next(answers))
    setup.configure_amazon(path, {})
    config = load_config(path)
    assert config['YNAB_API_TOKEN'] == 'synthetic-token'
    assert config['AMAZON_PASSWORD'] == password
    assert config['AMAZON_DOMAIN'] == 'amazon.com'
    assert config['AMAZON_BUDGET_NAME'] == 'Example Budget'
    assert config['AMAZON_OTP_SECRET_KEY'] == 'ABCDEFGHIJKLMNOP'
    output = capsys.readouterr().out
    assert password not in output
    assert 'ABCDEFGHIJKLMNOP' not in output


def test_amazon_cancelled_prompt_does_not_save_partial_credentials(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    monkeypatch.setattr('builtins.input', lambda *args: 'example@example.test')
    monkeypatch.setattr(setup, 'getpass', lambda *args: '')
    with pytest.raises(SystemExit):
        setup.configure_amazon(path, {})
    assert not (tmp_path / '.env').exists()


@pytest.mark.parametrize('secret', ['123456', 'otpauth://totp/example', 'INVALID0000000000'])
def test_amazon_rejects_codes_and_invalid_secrets_before_saving(tmp_path, monkeypatch, secret):
    secrets = iter(['example-password', secret])
    monkeypatch.setattr('builtins.input', lambda *args: 'example@example.test')
    monkeypatch.setattr(setup, 'getpass', lambda *args: next(secrets))
    with pytest.raises(SystemExit, match='long base32 setup key'):
        setup.configure_amazon(tmp_path / 'config.toml', {})
    assert not (tmp_path / '.env').exists()
