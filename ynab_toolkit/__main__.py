"""Run the toolkit while preserving the original Python integration package."""

from ynab_categorizer.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main() or 0)
