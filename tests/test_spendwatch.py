"""Spend-watch behavior using invented toy budgets, never household records.

Amounts are chosen for simple arithmetic and threshold coverage. Budget labels,
IDs, categories, and relationships are fictional. SMTP and YNAB are mocked.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from ynab_categorizer.spendwatch import (
    approaching_categories, overspent_categories, savings_parity, spend_watch,
    spending_status, total_spent, ytd_contributions,
)

PAIRS = [("Learning", ("learning", "course")), ("Equipment", ("equipment", "tools"))]


def _cat(name, group="Needs", activity=0, balance=0, budgeted=0, goal_target=0):
    return dict(name=name, group_name=group, activity=activity, balance=balance,
                budgeted=budgeted, goal_target=goal_target)


def test_total_spent_sums_spending_categories_only():
    cats = [
        _cat("Food", activity=-7_000), _cat("Supplies", activity=-4_000),
        _cat("Card Alpha", group="Credit Card Payments", activity=-29_000),
        _cat("Inflow: Ready to Assign", group="Internal Master Category", activity=120_000),
    ]
    assert total_spent(cats) == 11_000


def test_total_spent_nets_refunds_against_spending():
    assert total_spent([_cat("Supplies", activity=-17_000),
                        _cat("Supplies refund", activity=6_000)]) == 11_000


def test_spending_status_ok_below_warn_threshold():
    status = spending_status(spent=37_000, income=120_000, warn_ratio=0.8)
    assert status["level"] == "ok"
    assert status["headroom"] == 83_000


def test_spending_status_warns_when_approaching_income():
    assert spending_status(102_000, 120_000, warn_ratio=0.8)["level"] == "warning"


def test_spending_status_over_when_spending_exceeds_income():
    status = spending_status(131_000, 120_000, warn_ratio=0.8)
    assert status["level"] == "over"
    assert status["headroom"] == -11_000


def test_overspent_categories_lists_negative_balances_worst_first():
    cats = [_cat("Food", balance=7_000), _cat("Supplies", balance=-13_000),
            _cat("Hobbies", balance=-3_000)]
    assert [c["name"] for c in overspent_categories(cats)] == ["Supplies", "Hobbies"]


def test_overspent_categories_ignores_card_payment_categories():
    assert overspent_categories([_cat("Card Alpha", group="Credit Card Payments", balance=-3_000)]) == []


def test_approaching_categories_flags_nearly_spent_budgets():
    cats = [
        _cat("Food", budgeted=20_000, activity=-19_000, balance=1_000),
        _cat("Travel", budgeted=10_000, activity=-5_000, balance=5_000),
        _cat("Supplies", budgeted=10_000, activity=-13_000, balance=-3_000),
        _cat("No budget", budgeted=0, activity=-2_000, balance=-2_000),
    ]
    assert [c["name"] for c in approaching_categories(cats, ratio=0.9)] == ["Food"]


def test_approaching_categories_ignores_bills_consumed_exactly():
    assert approaching_categories([_cat("Fixed bill", budgeted=31_000,
                                        activity=-31_000, balance=0)], ratio=0.9) == []


def test_savings_parity_compares_monthly_targets_across_budgets():
    alpha = [_cat("📘 Learning", group="Savings", goal_target=7_000)]
    beta = [_cat("🖊 Course fund ", group="Savings", goal_target=11_000)]
    rows = savings_parity([("Alpha", alpha), ("Beta", beta)], pairs=PAIRS)
    assert rows[0]["targets"] == {"Alpha": 7_000, "Beta": 11_000}


def test_savings_parity_matches_configured_aliases():
    alpha = [_cat("🔧 Tools", group="Savings", goal_target=13_000)]
    beta = [_cat("📦 Equipment ", group="Savings", goal_target=19_000)]
    rows = savings_parity([("Alpha", alpha), ("Beta", beta)], pairs=PAIRS)
    assert rows[1]["targets"] == {"Alpha": 13_000, "Beta": 19_000}


def test_savings_parity_flags_category_missing_on_one_side():
    rows = savings_parity([("Alpha", [_cat("Learning", goal_target=7_000)]),
                          ("Beta", [_cat("Food")])], pairs=PAIRS)
    assert rows[0]["targets"] == {"Alpha": 7_000, "Beta": None}


def test_savings_comparisons_require_explicit_configuration():
    assert savings_parity([("Alpha", [_cat("Learning", goal_target=7_000)])]) == []


def test_ytd_contributions_sums_assigned_across_the_year():
    months = [[_cat("Learning", budgeted=7_000, balance=7_000)],
              [_cat("Learning", budgeted=11_000, balance=18_000)],
              [_cat("Learning", budgeted=19_000, balance=37_000)]]
    assert ytd_contributions(months, ("learning",)) == dict(assigned=37_000, spent=0, balance=37_000)


def test_ytd_contributions_counts_money_spent_from_the_category():
    months = [[_cat("Equipment", budgeted=13_000, balance=13_000)],
              [_cat("Equipment", budgeted=19_000, activity=-23_000, balance=9_000)]]
    assert ytd_contributions(months, ("equipment",)) == dict(assigned=32_000, spent=23_000, balance=9_000)


def test_ytd_contributions_none_when_category_never_existed():
    assert ytd_contributions([[_cat("Food", budgeted=7_000)]], ("learning",)) is None


def _watch_client():
    client = MagicMock()
    client.get_budgets.return_value = [
        {"id": "budget-alpha", "name": "Alpha Demo"},
        {"id": "budget-beta", "name": "Beta Demo"},
        {"id": "budget-unused", "name": "Unused Demo"},
    ]
    client.get_categories.side_effect = lambda bid: {
        "budget-alpha": [_cat("Supplies", activity=-102_000, balance=-3_000),
                         _cat("Learning", goal_target=7_000)],
        "budget-beta": [_cat("Fixed bill", activity=-31_000),
                        _cat("Course fund", goal_target=11_000)],
    }[bid]
    client.get_month.side_effect = lambda bid, month: {"categories": {
        "budget-alpha": [_cat("Learning", budgeted=7_000, balance=21_000)],
        "budget-beta": [_cat("Course fund", budgeted=11_000, activity=-2_000, balance=27_000)],
    }[bid]}
    return client


CONFIG = {
    "MONTHLY_INCOME_ALPHA": "120",
    "MONTHLY_INCOME_BETA": "75",
    "GMAIL_ADDRESS": "sender@example.com",
    "GMAIL_APP_PASSWORD": "test-placeholder",
    "savings_pairs": dict(PAIRS),
}


def test_spend_watch_reports_each_configured_budget(capsys):
    spend_watch(_watch_client(), CONFIG, send=False)
    out = capsys.readouterr().out
    assert "Alpha" in out and "Beta" in out and "Unused" not in out
    assert "WARNING" in out  # 102 / 120 = 85%, above the 80% threshold.


def test_spend_watch_parity_shows_year_to_date_contributions(capsys):
    spend_watch(_watch_client(), CONFIG, send=False, today=date(2015, 3, 15))
    out = capsys.readouterr().out
    assert "put in $21.00" in out  # 3 x 7
    assert "put in $33.00" in out  # 3 x 11
    assert "spent $6.00" in out    # 3 x 2
    assert "left $27.00" in out


def test_spend_watch_fetches_months_for_the_year_so_far():
    client = _watch_client()
    spend_watch(client, CONFIG, send=False, today=date(2015, 3, 15))
    assert {c.args[1] for c in client.get_month.call_args_list} == {"2015-01-01", "2015-02-01", "2015-03-01"}


def test_spend_watch_dry_run_sends_no_email():
    with patch("ynab_categorizer.spendwatch.smtplib") as smtp:
        spend_watch(_watch_client(), CONFIG, send=False)
    smtp.SMTP_SSL.assert_not_called()


def test_spend_watch_send_emails_the_report():
    with patch("ynab_categorizer.spendwatch.smtplib") as smtp:
        spend_watch(_watch_client(), CONFIG, send=True)
    smtp.SMTP_SSL.assert_called_once()
    server = smtp.SMTP_SSL.return_value.__enter__.return_value
    server.login.assert_called_once_with("sender@example.com", "test-placeholder")
    msg = server.send_message.call_args.args[0]
    assert "WARNING" in msg["Subject"]
    assert "Alpha" in msg.get_body(("plain",)).get_content()


def test_spend_watch_email_has_a_styled_html_version():
    with patch("ynab_categorizer.spendwatch.smtplib") as smtp:
        spend_watch(_watch_client(), CONFIG, send=True, today=date(2015, 3, 15))
    server = smtp.SMTP_SSL.return_value.__enter__.return_value
    html = server.send_message.call_args.args[0].get_body(("html",)).get_content()
    assert "Alpha" in html and "Beta" in html and "WARNING" in html
    assert "<table" in html and "$21.00" in html


def test_spend_watch_send_without_credentials_degrades_gracefully(capsys):
    with patch("ynab_categorizer.spendwatch.smtplib") as smtp:
        spend_watch(_watch_client(), {"MONTHLY_INCOME_ALPHA": "120"}, send=True)
    smtp.SMTP_SSL.assert_not_called()
    assert "GMAIL" in capsys.readouterr().out


def test_explicit_budget_income_does_not_depend_on_first_word(capsys):
    config = {"budgets": {"Beta Demo": {"monthly_income": 75_000}}}
    spend_watch(_watch_client(), config, budget_selector="Beta Demo")
    out = capsys.readouterr().out
    assert "Beta Demo" in out and "Alpha Demo" not in out
