"""First-time setup: get YNAB token configured."""

import os
import shutil
import webbrowser
from pathlib import Path

from .config import load_config

YNAB_TOKEN_URL = "https://app.ynab.com/settings/developer"
ENV_FILE = Path(__file__).parent.parent / ".env"


def setup_token():
    """Guide the user through getting a YNAB personal access token."""
    print("=== YNAB Categorizer Setup ===\n")

    # Check claude CLI is available
    if not shutil.which("claude"):
        print("Warning: 'claude' CLI not found on PATH.")
        print("Install Claude Code or ensure it's on your PATH.\n")

    existing = _load_env()

    print("You need a YNAB Personal Access Token (NOT an OAuth Application).")
    print(f"Opening {YNAB_TOKEN_URL} in your browser...")
    print()
    print('Look for the "Personal Access Tokens" section (scroll down).')
    print('Click "New Token", enter your YNAB password, and copy the token.')
    print('(Ignore the "OAuth Applications" section — that\'s for something else.)\n')
    webbrowser.open(YNAB_TOKEN_URL)
    token = input("Paste your YNAB Personal Access Token here: ").strip()
    existing["YNAB_API_TOKEN"] = token

    _save_env(existing)
    print(f"\nSaved to {ENV_FILE}")
    print("You're all set! Run: pdm run categorize")


def _load_env() -> dict[str, str]:
    result = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                result[key.strip()] = val.strip()
    return result


def _save_env(config: dict[str, str]):
    lines = [f"{k}={v}" for k, v in config.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
