"""Public configuration loading for the command line tools.

Configuration is intentionally boring: a TOML file provides non-secret defaults
and per-budget options, while environment variables provide secrets and can
override any supported scalar setting.  The loader never invents personal
budget names or credentials.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from datetime import date

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"

# Keep this list in one place so spend-watch and the categorizer receive the
# same environment precedence rules.
SUPPORTED_ENV_KEYS = {
    "YNAB_API_TOKEN", "AMAZON_USERNAME", "AMAZON_PASSWORD",
    "AMAZON_OTP_SECRET_KEY", "AMAZON_BUDGET_NAME", "AMAZON_DOMAIN",
    "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "SPEND_WATCH_RECIPIENTS",
}
OPTION_KEYS = {
    "priority_groups", "discretionary_group", "wants_mode", "wants_overrides",
    "income_schedule", "group_roles", "merchant_rules", "model", "guess_model",
    "timeout", "guess_timeout", "report_path", "monthly_income", "warn_ratio",
    "near_limit_ratio", "currency_decimal_digits",
}
TOP_LEVEL_KEYS = {"defaults", "budgets", "savings_pairs", "default_budget"} | SUPPORTED_ENV_KEYS


def _read_toml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        if path is not None:
            raise FileNotFoundError(f"Configuration file not found: {path}")
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a TOML table: {path}")
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"Unknown configuration key(s): {', '.join(sorted(unknown))}")
    _validate_options(data.get("defaults", {}), "defaults")
    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("[budgets] must be a TOML table")
    for name, options in budgets.items():
        _validate_options(options, f"budgets.{name}")
    if "savings_pairs" in data:
        pairs = data["savings_pairs"]
        if not isinstance(pairs, dict) or any(not isinstance(v, list) or not all(isinstance(x, str) for x in v)
                                               for v in pairs.values()):
            raise ValueError("savings_pairs must map labels to lists of keywords")
    if "default_budget" in data and not isinstance(data["default_budget"], str):
        raise ValueError("default_budget must be a budget ID or name")
    return data


def _validate_options(options: Any, where: str) -> None:
    if not isinstance(options, dict):
        raise ValueError(f"[{where}] must be a TOML table")
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"Unknown option(s) in [{where}]: {', '.join(sorted(unknown))}")
    if "currency_decimal_digits" in options and (type(options["currency_decimal_digits"]) is not int or options["currency_decimal_digits"] not in range(4)):
        raise ValueError("currency_decimal_digits must be 0, 1, 2, or 3")
    if "wants_mode" in options and options["wants_mode"] not in {"refill", "accumulate"}:
        raise ValueError("wants_mode must be 'refill' or 'accumulate'")
    for key in ("model", "guess_model", "discretionary_group"):
        if key in options and not isinstance(options[key], str):
            raise ValueError(f"{key} must be a string")
    for key in ("timeout", "guess_timeout"):
        if key in options and (isinstance(options[key], bool) or not isinstance(options[key], int)
                               or options[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")
    if "priority_groups" in options and (
        not isinstance(options["priority_groups"], list)
        or not all(isinstance(x, str) for x in options["priority_groups"])
    ):
        raise ValueError("priority_groups must be a list of strings")
    for key in ("monthly_income", "warn_ratio", "near_limit_ratio"):
        if key in options and (isinstance(options[key], bool) or not isinstance(options[key], (int, float))):
            raise ValueError(f"{key} must be numeric")
    for key in ("wants_overrides", "merchant_rules", "group_roles"):
        if key in options and not isinstance(options[key], dict):
            raise ValueError(f"{key} must be a table")
    if "monthly_income" in options and (not isinstance(options["monthly_income"], int) or options["monthly_income"] <= 0):
        raise ValueError("monthly_income must be positive integer milliunits")
    for key in ("warn_ratio", "near_limit_ratio"):
        if key in options and not 0 < options[key] <= 1:
            raise ValueError(f"{key} must be between zero and one")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in options.get("wants_overrides", {}).values()):
        raise ValueError("wants_overrides must map categories to nonnegative integer milliunits")
    if any(not isinstance(v, str) or not v.strip() for v in options.get("merchant_rules", {}).values()):
        raise ValueError("merchant_rules must map payees to nonempty category strings")
    if any(v not in {"spending", "saving", "repayment", "exclude"} for v in options.get("group_roles", {}).values()):
        raise ValueError("group_roles values must be spending, saving, repayment, or exclude")
    if "report_path" in options and not isinstance(options["report_path"], str):
        raise ValueError("report_path must be a path string")
    if "priority_groups" in options and len(set(options["priority_groups"])) != len(options["priority_groups"]):
        raise ValueError("priority_groups must not contain duplicates")
    if "income_schedule" in options:
        schedule = options["income_schedule"]
        if not isinstance(schedule, list):
            raise ValueError("income_schedule must be an array of tables")
        for payment in schedule:
            if not isinstance(payment, dict) or set(payment) != {"date", "amount"}:
                raise ValueError("income_schedule entries require date and amount")
            try:
                date.fromisoformat(payment["date"])
            except (TypeError, ValueError):
                raise ValueError("income_schedule dates must be YYYY-MM-DD") from None
            if (isinstance(payment["amount"], bool)
                    or not isinstance(payment["amount"], int)
                    or payment["amount"] < 0):
                raise ValueError("income_schedule amounts must be nonnegative integer milliunits")


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def load_config(config_path: str | Path | None = None,
                env_path: str | Path | None = None) -> dict[str, Any]:
    """Load TOML, ``.env``, and process environment settings.

    TOML is optional.  ``.env`` remains supported for existing installations;
    process environment wins over both files for every supported key. Unknown
    TOML options are rejected before connecting to YNAB.
    """
    toml_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    data = {} if config_path is None and not toml_path.exists() else _read_toml(toml_path)
    env_file = Path(env_path) if env_path else PROJECT_ROOT / ".env"
    for key, value in _read_env(env_file).items():
        data[key] = value
    for key in SUPPORTED_ENV_KEYS | {
        key for key in os.environ if key.startswith("MONTHLY_INCOME_")
    }:
        if os.environ.get(key) is not None:
            data[key] = os.environ[key]
    return data


def budget_options(config: dict[str, Any], budget: dict) -> dict[str, Any]:
    """Resolve defaults and a budget override by ID or exact budget name."""
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("[defaults] must be a TOML table")
    result = {"currency_decimal_digits": budget.get("currency_format", {}).get("decimal_digits", 2), **defaults}
    budgets = config.get("budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("[budgets] must be a TOML table")
    for key in (budget.get("id"), budget.get("name")):
        if key and isinstance(budgets.get(key), dict):
            for option, value in budgets[key].items():
                if isinstance(value, dict) and isinstance(result.get(option), dict):
                    result[option] = {**result[option], **value}
                else:
                    result[option] = value
    return result
