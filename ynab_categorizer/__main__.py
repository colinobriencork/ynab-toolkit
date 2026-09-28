"""Shared command line implementation for YNAB Toolkit and its legacy entrypoint."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from .amazon import AmazonEnricher
from .budgeting import assign_funds, phantom_assign
from .client import YNABClient
from .categorizer import Categorizer
from .config import budget_options, load_config
from .orchestrator import Orchestrator, select_budget
from .setup import setup_token, configure_amazon
from .spendwatch import spend_watch
from .rebalance import rebalance

AMAZON_KEYS = ("AMAZON_USERNAME", "AMAZON_PASSWORD", "AMAZON_OTP_SECRET_KEY")
COMMANDS = {"categorize", "assign", "phantom-assign", "spend-watch",
            "budget-check", "correct", "restore", "spending-report",
            "setup", "amazon-login", "rebalance"}


def _budget_month(value):
    try:
        month = date.fromisoformat(value + "-01")
        if month.strftime("%Y-%m") != value:
            raise ValueError
        return month.isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("month must be YYYY-MM") from None


def _parser():
    p = argparse.ArgumentParser(
        prog="ynab-toolkit",
        description="Categorize transactions, plan your budget, cover overspending, and review spending.",
        epilog="Use ynab-toolkit COMMAND --help for command options. Budget changes preview by default; add --apply to save them to YNAB.",
    )
    p.add_argument("--config", type=Path, help="settings file (reads .env beside it)")
    sub = p.add_subparsers(dest="command")

    def command(name, help_text, apply=False):
        child = sub.add_parser(name, help=help_text, description=help_text)
        child.add_argument("--budget", help="Budget ID or exact budget name")
        if apply:
            child.add_argument("--apply", action="store_true",
                               help="write changes (default is preview)")
        return child

    cat = command("categorize", "review categories for unapproved transactions")
    mode = cat.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview without approving")
    mode.add_argument("--apply", action="store_true", help="approve categorized transactions")
    cat.add_argument("--report", type=Path, help="write a JSON run report")
    cat.add_argument("--backend", choices=("claude", "codex", "command"),
                     help="model backend (overrides configuration)")
    cat.add_argument("--model", help="first-pass model for the selected backend")
    cat.add_argument("--guess-model", help="second-pass model for the selected backend")
    assign = command("assign", "budget money you already have available", apply=True)
    assign.add_argument("--report", type=Path)
    phantom = command("phantom-assign", "plan a month using expected income", apply=True)
    phantom.add_argument("--report", type=Path)
    sweep = command("rebalance", "move unused money this month to cover overspending", apply=True)
    sweep.add_argument("--report", type=Path)
    for planner in (assign, phantom):
        planner.add_argument("--refill-rebalanced", action="store_true",
                             help="allow funding categories released by rebalance this month")
    watch = command("spend-watch", "compare spending with your configured income")
    watch.add_argument("--send", action="store_true", help="email the report")
    check = command("budget-check", "check overspending, targets, and card funding")
    check.add_argument("--report", type=Path)
    for planner in (assign, phantom, check):
        planner.add_argument("--month", type=_budget_month, metavar="YYYY-MM",
                             help="budget month (default: current month)")
    correct = command("correct", "fix one expense's category and matching funding", apply=True)
    correct.add_argument("--transaction", required=True)
    correct.add_argument("--category", required=True)
    correct.add_argument("--report", type=Path)
    restore = command("restore", "reverse supported changes from a saved run journal", apply=True)
    restore.add_argument("path", type=Path)
    restore.add_argument("--report", type=Path)
    report = command("spending-report", "analyze spending over a date range")
    report.add_argument("--start", required=True)
    report.add_argument("--end", required=True)
    report.add_argument("--report", type=Path)
    command("setup", "save your YNAB token and create starter settings")
    amazon = command("amazon-login", "save an Amazon session for purchase matching")
    amazon.add_argument("--install-browser", action="store_true",
                        help="install this toolkit's Playwright Chromium before signing in")
    amazon.add_argument("--configure", action="store_true",
                        help="prompt for Amazon credentials and the matching budget again")
    return p


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # argparse global options normally have to precede the subcommand. Accept
    # the friendlier post-command spelling too by moving it before parsing.
    for i, token in enumerate(argv):
        if token == "--config" and i + 1 < len(argv):
            value = argv[i + 1]
            argv = ["--config", value] + argv[:i] + argv[i + 2:]
            break
        if token.startswith("--config="):
            value = token.split("=", 1)[1]
            argv = ["--config", value] + argv[:i] + argv[i + 1:]
            break
    offset = 2 if argv[:1] == ["--config"] else 0
    rest = argv[offset:]
    if not rest or (rest[0].startswith("-") and rest[0] not in {"-h", "--help"}):
        argv.insert(offset, "categorize")
    return _parser().parse_args(argv)


def _export_amazon_env(config: dict) -> bool:
    if not all(config.get(k) for k in AMAZON_KEYS):
        return False
    for key in AMAZON_KEYS:
        os.environ[key] = str(config[key])
    if config.get("AMAZON_DOMAIN"):
        os.environ["AMAZON_DOMAIN"] = str(config["AMAZON_DOMAIN"])
    return True


def _build_enricher(config: dict) -> AmazonEnricher | None:
    return AmazonEnricher() if _export_amazon_env(config) else None


def _client_or_exit(config: dict) -> YNABClient:
    if not config.get("YNAB_API_TOKEN"):
        raise SystemExit("Missing YNAB token. Run: ynab-toolkit setup")
    return YNABClient(config["YNAB_API_TOKEN"])


def _pick_budget(client, selector):
    return select_budget(client, selector)


def _status_code(result):
    if not isinstance(result, dict):
        return 0
    status = result.get("status")
    return 1 if status in {"failed", "partial_failure", "incomplete", "verification_failed", "stale"} else 0


def main(argv=None):
    args = parse_args(argv)
    if args.command == "setup":
        setup_token(args.config)
        return
    config = load_config(args.config)
    if args.command == "amazon-login":
        from .amazon import interactive_login
        if args.configure or not all(config.get(key) for key in (*AMAZON_KEYS, "AMAZON_BUDGET_NAME")):
            configure_amazon(args.config, config)
            config = load_config(args.config)
        if not _export_amazon_env(config):
            raise SystemExit("Missing Amazon credentials in configuration")
        if args.install_browser:
            result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
            if result.returncode:
                raise SystemExit(result.returncode)
        raise SystemExit(0 if interactive_login() else 1)

    client = _client_or_exit(config)
    try:
        return _dispatch(client, args, config)
    finally:
        client.close()


def _dispatch(client, args, config):
    if args.command in {"assign", "phantom-assign", "budget-check", "correct",
                        "spending-report", "rebalance"}:
        budget = _pick_budget(client, getattr(args, "budget", None) or config.get("default_budget"))
        options = budget_options(config, budget)
        if getattr(args, "month", None):
            options["month"] = args.month
        if getattr(args, "refill_rebalanced", False):
            options["refill_rebalanced"] = True
        if args.command == "rebalance":
            options["report_path"] = args.report or options.get("report_path")
            return _status_code(rebalance(client, budget["id"], apply=args.apply, options=options))
        if args.command == "assign":
            options["report_path"] = args.report or options.get("report_path")
            return _status_code(assign_funds(client, budget["id"], apply=args.apply,
                                             options=options))
        elif args.command == "phantom-assign":
            options["report_path"] = args.report or options.get("report_path")
            return _status_code(phantom_assign(client, budget["id"], today=date.today(),
                                               apply=args.apply, options=options))
        elif args.command == "budget-check":
            from .budgeting import budget_check
            result = budget_check(client, budget["id"], options=options)
            if args.report:
                from .audit import write_report
                write_report(result, args.report)
            return 1 if result['overspending'] or any(c['surplus'] < 0 for c in result['cards']) or result['funding_status'] == 'uncovered' else 0
        elif args.command == "correct":
            from .operations import correct_transaction
            return _status_code(correct_transaction(client, budget["id"], args.transaction,
                                args.category, apply=args.apply, report_path=args.report))
        else:
            from .reporting import spending_report
            result = spending_report(client, budget["id"], args.start, args.end, options=options)
            if args.report:
                from .audit import write_report
                write_report(result, args.report)
            return 0
    if args.command == "restore":
        from .operations import restore_run
        return _status_code(restore_run(client, args.path, apply=args.apply,
                                        report_path=args.report))
    configured_budget = config.get("default_budget")
    if args.command == "spend-watch":
        spend_watch(client, config, send=args.send,
                    budget_selector=args.budget or configured_budget)
        return 0

    budget = _pick_budget(client, args.budget or configured_budget)
    options = budget_options(config, budget)
    # Switching provider on the command line must not reuse another provider's
    # configured model aliases.
    if args.backend and args.backend != options.get("backend", "claude"):
        for key in ("model", "guess_model"):
            options.pop(key, None)
    options["backend"] = args.backend or options.get("backend", "claude")
    for key in ("model", "guess_model"):
        if getattr(args, key):
            options[key] = getattr(args, key)
    categorizer = Categorizer(
        backend=options["backend"],
        backend_command=options.get("backend_command"),
        guess_backend_command=options.get("guess_backend_command"),
        model=options.get("model"),
        guess_model=options.get("guess_model"),
        timeout=options.get("timeout", 60),
        guess_timeout=options.get("guess_timeout", 180),
    )
    orchestrator = Orchestrator(
        client, categorizer, enricher=_build_enricher(config),
        amazon_budget_name=config.get("AMAZON_BUDGET_NAME"),
        dry_run=not args.apply, budget_selector=budget["id"],
        merchant_rules=options.get("merchant_rules", {}),
        report_path=args.report,
    )
    return _status_code(orchestrator.run())


if __name__ == "__main__":
    raise SystemExit(main() or 0)
