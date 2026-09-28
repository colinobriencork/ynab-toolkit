"""Configure a YNAB token in the same private directory the CLI reads."""

from getpass import getpass
import base64
import json
import os
from pathlib import Path
import webbrowser

from .config import config_file_path, _read_env

YNAB_TOKEN_URL = "https://app.ynab.com/settings/developer"
YNAB_TOKEN_DOCS = "https://api.ynab.com/#personal-access-tokens"


def setup_token(config_path: str | Path | None = None):
    """Guide token creation; keep existing options and other secrets intact."""
    settings = config_file_path(config_path)
    env_file = settings.parent / ".env"
    print("=== YNAB Toolkit Setup ===\n")
    print(f"YNAB's token instructions: {YNAB_TOKEN_DOCS}")
    print('In Developer Settings, choose "New Token" under "Personal Access Tokens".')
    print("Use your own token; an OAuth application is not needed.")
    print(f"Opening {YNAB_TOKEN_URL} in your browser...\n")
    webbrowser.open(YNAB_TOKEN_URL)
    existing = _read_env(env_file)
    token = getpass("Paste your YNAB token (hidden; Enter keeps an existing token): ").strip()
    token = token or existing.get("YNAB_API_TOKEN", "")
    if not token or "\n" in token or "\r" in token:
        raise SystemExit("Enter a nonempty, single-line token. No files were changed.")

    starter = None
    if not settings.exists():
        budget = input("Default YNAB budget name (optional; Enter to choose each run): ").strip()
        backend = input("AI backend for categorization [claude/codex] (claude): ").strip() or "claude"
        while backend not in {"claude", "codex"}:
            backend = input("Enter claude or codex: ").strip()
        starter = (f"default_budget = {json.dumps(budget, ensure_ascii=False)}\n" if budget else "")
        starter += f'\n[defaults]\nbackend = "{backend}"\nspending_target_mode = "refill"\n'
    _save_values(env_file, {"YNAB_API_TOKEN": token})
    if starter is not None:
        _write_private(settings, starter)
    print(f"\nToken saved to {env_file}")
    print(f"Budget and AI settings: {settings}")
    print("Sign in to your chosen AI CLI (codex login or claude), then run ynab-toolkit categorize.")
    print("Or run ynab-toolkit budget-check without an AI backend.")


def configure_amazon(config_path: str | Path | None, current: dict):
    """Collect optional purchase-matching credentials without echoing secrets."""
    print("Amazon setup: your password and authenticator secret are hidden.")
    print("Use the base32 authenticator setup secret, not a six-digit code.")
    print("Find it in Amazon: Login & security -> Two-Step Verification -> add Authenticator App.")
    print("At the QR code, choose the manual setup / Can't scan the barcode option.")
    print("Complete Amazon's verification with your authenticator before continuing here.")
    print("Enter your normal password only; the toolkit appends a fresh code when needed.")
    prompts = {
        "AMAZON_USERNAME": "Amazon login/email: ",
        "AMAZON_PASSWORD": "Amazon password (hidden): ",
        "AMAZON_OTP_SECRET_KEY": "Authenticator base32 secret (hidden): ",
        "AMAZON_DOMAIN": "Amazon domain (amazon.com): ",
        "AMAZON_BUDGET_NAME": "Exact YNAB budget name for these purchases: ",
    }
    values = {}
    for key, prompt in prompts.items():
        previous = current.get(key, "amazon.com" if key == "AMAZON_DOMAIN" else "")
        if previous:
            prompt += "[Enter keeps existing value] "
        read = getpass if key in {"AMAZON_PASSWORD", "AMAZON_OTP_SECRET_KEY"} else input
        value = read(prompt) or previous
        if not value.strip() or "\n" in value or "\r" in value:
            raise SystemExit("All Amazon fields are required and must be single-line. No credentials were changed.")
        if key == "AMAZON_OTP_SECRET_KEY":
            value = "".join(value.split()).upper().rstrip("=")
            try:
                if len(value) < 16:
                    raise ValueError
                base64.b32decode(value + "=" * (-len(value) % 8))
            except ValueError:
                raise SystemExit("Use the long base32 setup key from the authenticator QR screen, not a six-digit code or QR URL. No credentials were changed.") from None
        values[key] = value
    env_file = config_file_path(config_path).parent / ".env"
    _save_values(env_file, values)
    print(f"Amazon settings saved to {env_file}")


def _save_values(path: Path, values: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Preserve comments, quoting, and unrelated credentials exactly as written.
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    lines.extend(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
    _write_private(path, "\n".join(lines) + "\n")


def _write_private(path: Path, content: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
