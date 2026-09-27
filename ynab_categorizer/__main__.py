"""Command line entrypoint for YNAB Categorizer."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .amazon import AmazonEnricher
from .budgeting import assign_funds, phantom_assign
from .client import YNABClient
from .categorizer import Categorizer
from .config import budget_options, load_config
from .orchestrator import Orchestrator, select_budget
from .setup import setup_token
from .spendwatch import spend_watch

AMAZON_KEYS = ("AMAZON_USERNAME", "AMAZON_PASSWORD", "AMAZON_OTP_SECRET_KEY")
COMMANDS = {"categorize", "assign", "phantom-assign", "spend-watch",
            "budget-check", "correct", "restore", "spending-report",
            "setup", "amazon-login"}


def _parser():
    p = argparse.ArgumentParser(prog="ynab-categorizer")
    p.add_argument("--config", type=Path, help="TOML configuration file")
    sub = p.add_subparsers(dest="command")

    def command(name, help_text, apply=False):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("--budget", help="Budget ID or exact budget name")
        if apply:
            child.add_argument("--apply", action="store_true",
                               help="write changes (default is preview)")
        return child

    cat = command("categorize", "categorize unapproved transactions")
    mode = cat.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview without approving")
    mode.add_argument("--apply", action="store_true", help="approve categorized transactions")
    cat.add_argument("--report", type=Path, help="write a JSON run report")
    assign = command("assign", "assign Ready-to-Assign money", apply=True)
    assign.add_argument("--report", type=Path)
    phantom = command("phantom-assign", "forward-plan expected income", apply=True)
    phantom.add_argument("--report", type=Path)
    watch = command("spend-watch", "report spending against configured income")
    watch.add_argument("--send", action="store_true", help="email the report")
    check = command("budget-check", "run budget health checks")
    check.add_argument("--report", type=Path)
    correct = command("correct", "correct one transaction", apply=True)
    correct.add_argument("--transaction", required=True)
    correct.add_argument("--category", required=True)
    correct.add_argument("--report", type=Path)
    restore = command("restore", "restore a run journal", apply=True)
    restore.add_argument("path", type=Path)
    restore.add_argument("--report", type=Path)
    report = command("spending-report", "report spending for a date range")
    report.add_argument("--start", required=True)
    report.add_argument("--end", required=True)
    report.add_argument("--report", type=Path)
    command("setup", "set up the YNAB token")
    command("amazon-login", "log in to Amazon")
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
        raise SystemExit("Missing YNAB token. Run: pdm run setup")
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
    config = load_config(args.config)
    if args.command == "setup":
        setup_token()
        return
    if args.command == "amazon-login":
        from .amazon import interactive_login
        if not _export_amazon_env(config):
            raise SystemExit("Missing Amazon credentials in configuration")
        raise SystemExit(0 if interactive_login() else 1)

    client = _client_or_exit(config)
    try:
        return _dispatch(client, args, config)
    finally:
        client.close()


def _dispatch(client, args, config):
    if args.command in {"assign", "phantom-assign", "budget-check", "correct",
                        "spending-report"}:
        budget = _pick_budget(client, getattr(args, "budget", None) or config.get("default_budget"))
        options = budget_options(config, budget)
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
    categorizer = Categorizer(
        model=options.get("model", "haiku"),
        guess_model=options.get("guess_model", "opus"),
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
