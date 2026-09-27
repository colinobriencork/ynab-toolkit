"""Tests for month-end budgeting helpers: auto-assigning Ready-to-Assign money
to credit-card debt and category targets, and forward-planning the month.

The planning logic is pure and tested directly; the drivers are tested against a
mocked client so no real money or dates move.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest

from ynab_categorizer.budgeting import (
    DEFAULT_PRIORITY_GROUPS,
    assign_funds,
    average_monthly_spending,
    detect_biweekly_income,
    last_day_of_previous_month,
    phantom_assign,
    plan_assignments,
    plan_cc_debt_coverage,
    plan_phantom_assignments,
    previous_months,
)


@pytest.fixture(autouse=True)
def private_test_journals(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _cat(name, group, under_funded, budgeted=0, balance=0, id_=None):
    return {
        "id": id_ or name,
        "name": name,
        "group_name": group,
        "goal_under_funded": under_funded,
        "budgeted": budgeted,
        "balance": balance,
    }


# --- plan_assignments ----------------------------------------------------


def test_plan_assignments_funds_groups_in_priority_order():
    cats = [
        _cat("Wants thing", "Wants", 50_000),
        _cat("Rent", "Bills", 100_000),
        _cat("Groceries", "Needs", 40_000),
    ]
    # Enough to fund Bills + Needs fully, partial Wants.
    plan = plan_assignments(160_000, cats, ["Bills", "Needs", "Savings", "Wants"])

    ordered = [(c["name"], amt) for c, amt in plan]
    assert ordered == [
        ("Rent", 100_000),       # Bills first
        ("Groceries", 40_000),   # then Needs
        ("Wants thing", 20_000), # then Wants, capped by what's left
    ]


def test_plan_assignments_caps_total_at_ready_to_assign():
    cats = [_cat("Rent", "Bills", 100_000), _cat("Hydro", "Bills", 100_000)]
    plan = plan_assignments(150_000, cats, ["Bills"])

    assert sum(amt for _, amt in plan) == 150_000


def test_plan_assignments_skips_categories_with_no_underfunding():
    cats = [
        _cat("Rent", "Bills", 0),        # already funded — skip
        _cat("Hydro", "Bills", 30_000),
        _cat("NoGoal", "Bills", None),   # no target — skip
    ]
    plan = plan_assignments(500_000, cats, ["Bills"])

    assert [(c["name"], amt) for c, amt in plan] == [("Hydro", 30_000)]


def test_plan_assignments_ignores_groups_outside_priority_list():
    cats = [
        _cat("Annual gift", "Infrequent", 80_000),
        _cat("Rent", "Bills", 50_000),
    ]
    plan = plan_assignments(500_000, cats, ["Bills", "Needs", "Savings", "Wants"])

    names = [c["name"] for c, _ in plan]
    assert "Annual gift" not in names  # Infrequent never funded
    assert names == ["Rent"]


def test_plan_assignments_stops_when_money_runs_out():
    cats = [
        _cat("Rent", "Bills", 100_000),
        _cat("Wants thing", "Wants", 50_000),
    ]
    plan = plan_assignments(100_000, cats, ["Bills", "Wants"])

    # Bills consumed everything; Wants gets nothing (and isn't listed).
    assert [(c["name"], amt) for c, amt in plan] == [("Rent", 100_000)]


def test_default_priority_excludes_infrequent_includes_savings():
    assert "Infrequent" not in DEFAULT_PRIORITY_GROUPS
    assert DEFAULT_PRIORITY_GROUPS[0] == "Bills"
    assert "Savings" in DEFAULT_PRIORITY_GROUPS


def test_plan_assignments_matches_groups_with_parenthetical_suffix():
    # One budget names the group "Savings", another "Savings (Reserved)" —
    # the priority entry "Savings" must fund both, but not a different group
    # that merely shares the word.
    cats = [
        _cat("Education", "Savings", 50_000),
        _cat("Vacation", "Savings (Reserved)", 30_000),
        _cat("Decoy", "Savings Extra", 80_000),
    ]
    plan = plan_assignments(500_000, cats, ["Savings"])

    assert [(c["name"], amt) for c, amt in plan] == [
        ("Education", 50_000),
        ("Vacation", 30_000),
    ]


# --- plan_cc_debt_coverage -------------------------------------------------


def _card(name, balance, type_="creditCard", closed=False):
    return {"id": name, "name": name, "type": type_, "balance": balance,
            "closed": closed}


def _spend(account, d, amount):
    return {"account_id": account, "date": d, "amount": amount}


def test_plan_cc_debt_coverage_tops_payment_category_up_to_rolled_over_debt():
    # Card owes $50, all carried in from last month; the payment category only
    # holds $20 — cover the missing $30.
    accounts = [_card("Card Alpha", -50_000)]
    cats = [_cat("Card Alpha", "Credit Card Payments", None, balance=20_000)]
    prev = [{"id": "Card Alpha", "balance": 20_000}]

    plan = plan_cc_debt_coverage(accounts, cats, prev, [])

    assert [(c["name"], amt) for c, amt in plan] == [("Card Alpha", 30_000)]


def test_plan_cc_debt_coverage_skips_gaps_from_current_month_overspending():
    # Last month closed square ($30 owed, $30 set aside). This month $20 of
    # unfunded card spending opened a gap — but funding those spending
    # categories moves the money to the payment category automatically, so
    # topping it up here would double-fund the card.
    accounts = [_card("Card Alpha", -50_000)]
    cats = [_cat("Card Alpha", "Credit Card Payments", None, balance=30_000)]
    prev = [{"id": "Card Alpha", "balance": 30_000}]
    txns = [_spend("Card Alpha", "2015-07-04", -20_000)]

    assert plan_cc_debt_coverage(accounts, cats, prev, txns) == []


def test_plan_cc_debt_coverage_covers_rollover_but_not_new_overspending():
    # $30 rolled over unfunded and another $20 was overspent this month: only
    # the rolled-over $30 gets topped up.
    accounts = [_card("Card Alpha", -50_000)]
    cats = [_cat("Card Alpha", "Credit Card Payments", None, balance=0)]
    prev = [{"id": "Card Alpha", "balance": 0}]
    txns = [_spend("Card Alpha", "2015-07-04", -20_000)]

    plan = plan_cc_debt_coverage(accounts, cats, prev, txns)

    assert [(c["name"], amt) for c, amt in plan] == [("Card Alpha", 30_000)]


def test_plan_cc_debt_coverage_counts_this_months_assignments_as_coverage():
    # A re-run after $10 was already assigned to the payment category this
    # month covers only the remaining $20 of the rolled-over $30.
    accounts = [_card("Card Alpha", -30_000)]
    cats = [_cat("Card Alpha", "Credit Card Payments", None, budgeted=10_000,
                 balance=10_000)]
    prev = [{"id": "Card Alpha", "balance": 0}]

    plan = plan_cc_debt_coverage(accounts, cats, prev, [])

    assert [(c["name"], amt) for c, amt in plan] == [("Card Alpha", 20_000)]


def test_plan_cc_debt_coverage_never_assigns_more_than_the_card_is_missing():
    # A refund shrank the debt below the rolled-over amount — top up only to
    # the card's actual balance.
    accounts = [_card("Card Alpha", -5_000)]
    cats = [_cat("Card Alpha", "Credit Card Payments", None, balance=0)]
    prev = [{"id": "Card Alpha", "balance": 0}]
    txns = [_spend("Card Alpha", "2015-07-02", 25_000)]

    plan = plan_cc_debt_coverage(accounts, cats, prev, txns)

    assert [(c["name"], amt) for c, amt in plan] == [("Card Alpha", 5_000)]


def test_plan_cc_debt_coverage_skips_cards_already_covered():
    accounts = [
        _card("Card Alpha", -50_000),   # fully covered by its payment category
        _card("Card Gamma", 5_000),       # positive balance: a credit, nothing owed
    ]
    cats = [
        _cat("Card Alpha", "Credit Card Payments", None, balance=50_000),
        _cat("Card Gamma", "Credit Card Payments", None, balance=0),
    ]
    prev = [{"id": "Card Alpha", "balance": 50_000}, {"id": "Card Gamma", "balance": 0}]

    assert plan_cc_debt_coverage(accounts, cats, prev, []) == []


def test_plan_cc_debt_coverage_includes_closed_card_with_debt():
    accounts = [
        _card("Chequing", -80_000, type_="checking"),
        _card("Old card", -40_000, closed=True),
    ]
    cats = [
        _cat("Chequing", "Credit Card Payments", None, balance=0),
        _cat("Old card", "Credit Card Payments", None, balance=0),
    ]

    assert [(c["name"], amount) for c, amount in plan_cc_debt_coverage(accounts, cats, [], [])] == [("Old card", 40_000)]


# --- assign_funds driver -------------------------------------------------


def _assign_client(rta=120_000):
    client = MagicMock()

    def get_month(budget_id, month="current"):
        if month == "current":
            return {"month": "2015-06-01", "to_be_budgeted": rta}
        return {"month": month, "categories": []}

    client.get_month.side_effect = get_month
    client.get_accounts.return_value = []
    client.get_transactions.return_value = []
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 100_000, budgeted=0),
        _cat("Wants thing", "Wants", 50_000, budgeted=10_000),
    ]
    return client


def test_assign_funds_dry_run_does_not_apply():
    client = _assign_client()
    assign_funds(client, "b1", apply=False)
    client.update_month_category.assert_not_called()


def test_assign_funds_apply_writes_new_budgeted_amounts():
    client = _assign_client()
    assign_funds(client, "b1", apply=True)

    # Rent funded fully (100k), Wants gets the remaining 20k on top of its 10k.
    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls["Rent"] == 100_000
    assert calls["Wants thing"] == 30_000  # 10k existing + 20k assigned


def test_assign_funds_covers_cc_debt_before_targets():
    # Rolled-over card debt eats into RTA first; targets get what's left. The
    # 5k already assigned to the payment category this month counts toward the
    # 30k rollover, so only the remaining 25k is added.
    client = _assign_client()
    client.get_accounts.return_value = [_card("Card Alpha", -30_000)]
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 200_000, budgeted=0),
        _cat("Card Alpha", "Credit Card Payments", None, budgeted=5_000, balance=5_000),
    ]

    assign_funds(client, "b1", apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls["Card Alpha"] == 30_000   # 5k already budgeted + 25k debt coverage
    assert calls["Rent"] == 95_000   # the 120k RTA minus the 25k debt


def test_assign_funds_cc_coverage_capped_by_ready_to_assign():
    client = _assign_client(rta=20_000)
    client.get_accounts.return_value = [_card("Card Alpha", -30_000)]
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 200_000, budgeted=0),
        _cat("Card Alpha", "Credit Card Payments", None, budgeted=0, balance=0),
    ]

    assign_funds(client, "b1", apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls == {"Card Alpha": 20_000}  # all RTA to the card; targets get nothing


def test_assign_funds_leaves_current_month_overspending_to_target_funding():
    # The whole card balance is this month's spending — no rollover, so the
    # payment category gets nothing and RTA goes to targets.
    client = _assign_client()
    client.get_accounts.return_value = [_card("Card Alpha", -30_000)]
    client.get_transactions.return_value = [
        _spend("Card Alpha", "2015-06-03", -30_000),
    ]
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 200_000, budgeted=0),
        _cat("Card Alpha", "Credit Card Payments", None, budgeted=0, balance=0),
    ]

    assign_funds(client, "b1", apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls == {"Rent": 120_000}


def test_assign_covers_rollover_then_overspending_then_targets():
    client = _assign_client(rta=120_000)
    client.get_accounts.return_value = [_card("Card Alpha", -50_000)]
    client.get_transactions.return_value = [_spend("Card Alpha", "2015-06-03", -20_000)]
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 200_000),
        _cat("Card Alpha", "Credit Card Payments", None),
        _cat("Trip", "Infrequent", None, balance=-20_000),
        _cat("Dining", "Wants", 0, balance=-10_000),
    ]
    assign_funds(client, "b1", apply=True)
    calls = [(c.args[2], c.kwargs["budgeted"])
             for c in client.update_month_category.call_args_list]
    assert calls == [("Card Alpha", 30_000), ("Trip", 20_000),
                     ("Dining", 10_000), ("Rent", 60_000)]


def test_assign_overspending_and_target_are_not_double_funded():
    client = _assign_client(rta=200_000)
    categories = [
        _cat("Groceries", "Needs", 100_000, budgeted=20_000, balance=-30_000),
        _cat("Dining", "Wants", 10_000, balance=-40_000),
    ]
    client.get_categories.return_value = categories
    assign_funds(client, "b1", apply=True)
    calls = [(c.args[2], c.kwargs["budgeted"])
             for c in client.update_month_category.call_args_list]
    assert calls == [("Groceries", 120_000), ("Dining", 40_000)]
    assert categories[0]["goal_under_funded"] == 100_000
    assert categories[0]["budgeted"] == 20_000


def test_assign_partial_overspending_exhausts_rta_before_targets():
    client = _assign_client(rta=15_000)
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 100_000),
        _cat("Trip", "Infrequent", None, budgeted=5_000, balance=-20_000),
        _cat("Dining", "Wants", None, balance=-10_000),
    ]
    assign_funds(client, "b1", apply=True)
    calls = [(c.args[2], c.kwargs["budgeted"])
             for c in client.update_month_category.call_args_list]
    assert calls == [("Trip", 20_000)]


def test_assign_overspending_dry_run_and_nonpositive_rta():
    for rta in (50_000, 0, -10_000):
        client = _assign_client(rta=rta)
        client.get_categories.return_value = [
            _cat("Trip", "Infrequent", None, balance=-20_000),
        ]
        assign_funds(client, "b1", apply=False)
        client.update_month_category.assert_not_called()
        if rta <= 0:
            assign_funds(client, "b1", apply=True)
            client.update_month_category.assert_not_called()


# --- date helpers ----------------------------------------------------------


def test_last_day_of_previous_month():
    assert last_day_of_previous_month(date(2015, 6, 5)) == date(2015, 5, 31)
    assert last_day_of_previous_month(date(2015, 1, 10)) == date(2014, 12, 31)
    assert last_day_of_previous_month(date(2015, 3, 1)) == date(2015, 2, 28)


# --- phantom assign: forward-plan discretionary money ----------------------


def _inflow(payee, d, amount):
    return {"id": f"{payee}-{d}", "payee_name": payee, "date": d, "amount": amount,
            "category_name": "Inflow: Ready to Assign"}


def _want(id_, avg_ignored=None, budgeted=0, activity=0):
    return {"id": id_, "name": id_, "group_name": "Wants",
            "goal_under_funded": None, "budgeted": budgeted, "activity": activity,
            "balance": budgeted + activity}


def test_detect_biweekly_income_projects_remaining_paydays_this_month():
    txns = [
        _inflow("ACME PAYROLL", "2015-05-22", 24_000),
        _inflow("ACME PAYROLL", "2015-06-05", 24_000),
        _inflow("ACME PAYROLL", "2015-06-19", 24_000),
        _inflow("ACME PAYROLL", "2015-07-03", 25_200),  # a raise: latest amount wins
    ]
    paydays = detect_biweekly_income(txns, today=date(2015, 7, 5))
    assert paydays == [(date(2015, 7, 17), 25_200), (date(2015, 7, 31), 25_200)]


def test_detect_biweekly_income_ignores_monthly_one_off_and_outflow_payees():
    txns = [
        # monthly cadence — not a biweekly paycheque
        _inflow("LANDLORD REFUND", "2015-04-01", 500_000),
        _inflow("LANDLORD REFUND", "2015-05-01", 500_000),
        _inflow("LANDLORD REFUND", "2015-06-01", 500_000),
        # one-off
        _inflow("TAX REFUND", "2015-06-15", 1_000_000),
        # biweekly but an outflow (e.g. a loan payment) — not income
        {"id": "o1", "payee_name": "GYM", "date": "2015-06-05", "amount": -50_000,
         "category_name": "Inflow: Ready to Assign"},
    ]
    assert detect_biweekly_income(txns, today=date(2015, 7, 5)) == []


def test_detect_biweekly_income_only_counts_ready_to_assign_inflows():
    # A biweekly-looking refund stream categorized to a spending category
    # (e.g. recurring reimbursements) is not a paycheque.
    txns = [
        {"id": f"r{i}", "payee_name": "WORK EXPENSES", "date": d, "amount": 300_000,
         "category_name": "Transport"}
        for i, d in enumerate(["2015-06-05", "2015-06-19", "2015-07-03"])
    ]
    assert detect_biweekly_income(txns, today=date(2015, 7, 5)) == []


def test_previous_months_returns_first_of_month_strings_oldest_first():
    assert previous_months(date(2015, 7, 5), 3) == [
        "2015-04-01", "2015-05-01", "2015-06-01"
    ]
    assert previous_months(date(2015, 2, 10), 3) == [
        "2014-11-01", "2014-12-01", "2015-01-01"
    ]


def test_average_monthly_spending_averages_spending_per_category():
    months = [
        {"categories": [{"id": "din", "activity": -300_000}]},
        {"categories": [{"id": "din", "activity": -150_000}]},
        {"categories": [{"id": "din", "activity": 60_000}]},  # net refund month = no spend
    ]
    avg = average_monthly_spending(months)
    assert avg["din"] == 150_000  # (300k + 150k + 0) / 3


def test_plan_phantom_funds_average_spend_when_pot_is_big_enough():
    cats = [
        _want("Dining", budgeted=100_000),  # avg 300k, 100k already assigned
        _want("Fun"),                        # avg 100k
    ]
    avg = {"Dining": 300_000, "Fun": 100_000}

    plan = plan_phantom_assignments(500_000, cats, avg)

    assert [(c["id"], amt) for c, amt in plan] == [("Dining", 200_000), ("Fun", 100_000)]


def test_plan_phantom_scales_down_proportionally_when_pot_is_short():
    cats = [_want("Dining"), _want("Fun")]
    avg = {"Dining": 200_000, "Fun": 100_000}

    plan = plan_phantom_assignments(150_000, cats, avg)  # half of the 300k needed

    assert [(c["id"], amt) for c, amt in plan] == [("Dining", 100_000), ("Fun", 50_000)]


def test_plan_phantom_covers_overspending_beyond_the_average():
    # Already spent more than the historical average — the plan must cover the
    # real spending, not the average.
    cats = [_want("Dining", activity=-350_000)]
    avg = {"Dining": 200_000}

    plan = plan_phantom_assignments(1_000_000, cats, avg)

    assert [(c["id"], amt) for c, amt in plan] == [("Dining", 350_000)]


def test_plan_phantom_skips_categories_already_funded_past_their_plan():
    cats = [_want("Dining", budgeted=400_000, activity=-100_000)]
    avg = {"Dining": 300_000}

    assert plan_phantom_assignments(1_000_000, cats, avg) == []


# --- phantom_assign driver -------------------------------------------------


def _phantom_client():
    client = MagicMock()

    def get_month(budget_id, month="current"):
        if month == "current":
            return {"month": "2015-07-01", "to_be_budgeted": 50_000}
        return {"month": month, "categories": [{"id": "fun", "activity": -100_000}]}

    client.get_month.side_effect = get_month
    client.get_accounts.return_value = []
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 100_000, budgeted=0),
        _want("fun"),
    ]
    client.get_transactions.return_value = [
        _inflow("ACME PAYROLL", "2015-05-22", 1_000_000),
        _inflow("ACME PAYROLL", "2015-06-05", 1_000_000),
        _inflow("ACME PAYROLL", "2015-06-19", 1_000_000),
        _inflow("ACME PAYROLL", "2015-07-03", 1_000_000),
    ]
    return client


def test_phantom_assign_dry_run_does_not_apply():
    client = _phantom_client()
    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=False)
    client.update_month_category.assert_not_called()


def test_phantom_assign_apply_funds_commitments_and_wants():
    # The whole month gets laid out at once: Rent's target fully funded, and
    # Fun sized by its average. Wants pot = 50k RTA + 2 paydays x 1000k
    # - 100k Rent = 1950k, so Fun's 100k average is fully affordable.
    client = _phantom_client()
    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls == {"Rent": 100_000, "fun": 100_000}


def test_phantom_assign_funds_commitments_fully_even_when_rta_is_empty():
    # Commitments are phantom-funded too — they are never capped by the cash
    # actually on hand; that is what makes -RTA equal the income still to come.
    client = _phantom_client()

    def get_month(budget_id, month="current"):
        if month == "current":
            return {"month": "2015-07-01", "to_be_budgeted": 0}
        return {"month": month, "categories": [{"id": "fun", "activity": -100_000}]}

    client.get_month.side_effect = get_month
    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls["Rent"] == 100_000


def test_phantom_assign_covers_card_debt_in_full():
    client = _phantom_client()
    client.get_accounts.return_value = [_card("Card Alpha", -30_000)]
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 100_000, budgeted=0),
        _cat("Card Alpha", "Credit Card Payments", None, budgeted=0, balance=0),
        _want("fun"),
    ]

    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls["Card Alpha"] == 30_000


def test_phantom_assign_treats_suffixed_savings_group_as_committed():
    # "Savings (Reserved)" is the same committed group as "Savings" — its
    # targets are phantom-funded in full, not left for the Wants pot.
    client = _phantom_client()
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 100_000, budgeted=0),
        _cat("Vacation", "Savings (Reserved)", 40_000, budgeted=0),
        _want("fun"),
    ]

    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls["Vacation"] == 40_000


def test_phantom_assign_funds_commitments_only_when_income_falls_short():
    # Expected income doesn't cover Bills — no Wants pot at all. Commitments
    # are still phantom-funded in full, and the driver must not crash on the
    # empty Wants plan.
    client = _phantom_client()
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 5_000_000, budgeted=0),  # dwarfs the 2 paydays
        _want("fun"),
    ]

    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)

    calls = {c.args[2]: c.kwargs["budgeted"] for c in client.update_month_category.call_args_list}
    assert calls == {"Rent": 5_000_000}


def test_phantom_assign_never_touches_infrequent():
    client = _phantom_client()
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 100_000, budgeted=0),
        _cat("Annual gift", "Infrequent", 80_000, budgeted=0),
        _want("fun"),
    ]

    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)

    funded = [c.args[2] for c in client.update_month_category.call_args_list]
    assert "Annual gift" not in funded


def test_phantom_covers_overspending_with_negative_rta_without_double_funding(capsys):
    client = _phantom_client()
    original_get_month = client.get_month.side_effect

    def get_month(budget_id, month="current"):
        result = original_get_month(budget_id, month)
        if month == "current":
            result["to_be_budgeted"] = -50_100
        return result

    client.get_month.side_effect = get_month
    client.get_accounts.return_value = [_card("Card Alpha", -70_000)]
    client.get_transactions.return_value.append(_spend("Card Alpha", "2015-07-04", -40_000))
    fun = _cat("fun", "Wants", None, budgeted=20_000, balance=-40_000)
    fun["activity"] = -60_000
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 1_800_000, balance=-20_000),
        _cat("Card Alpha", "Credit Card Payments", None),
        _cat("Trip", "Infrequent", None, balance=-30_000),
        fun,
    ]
    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)
    calls = [(c.args[2], c.kwargs["budgeted"])
             for c in client.update_month_category.call_args_list]
    # $30 rollover, $90 overspending, $1780 remaining Bills target leave
    # $49.90 for Wants. Fun needs just $40 more after its overspending is covered.
    assert calls == [("Card Alpha", 30_000), ("Rent", 1_800_000),
                     ("Trip", 30_000), ("fun", 100_000)]
    assert fun["budgeted"] == 20_000
    assert "current overspending:" in capsys.readouterr().out


def test_phantom_covers_wants_overspending_even_when_commitments_exceed_income(capsys):
    client = _phantom_client()
    fun = _cat("fun", "Wants", None, balance=-40_000)
    fun["activity"] = -40_000
    client.get_categories.return_value = [
        _cat("Rent", "Bills", 5_000_000), fun,
        _cat("Trip", "Infrequent", None, balance=-30_000),
    ]
    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=True)
    calls = [(c.args[2], c.kwargs["budgeted"])
             for c in client.update_month_category.call_args_list]
    assert calls == [("fun", 40_000), ("Trip", 30_000), ("Rent", 5_000_000)]
    assert "expected income doesn't cover commitments" in capsys.readouterr().out


def test_phantom_overspending_dry_run_does_not_write():
    client = _phantom_client()
    client.get_categories.return_value = [
        _cat("Trip", "Infrequent", None, balance=-30_000),
    ]
    phantom_assign(client, "b1", today=date(2015, 7, 5), apply=False)
    client.update_month_category.assert_not_called()


def test_deleted_transaction_does_not_erase_real_rollover_debt():
    card = _card("Card Alpha", -100_000)
    category = _cat("Card Alpha", "Credit Card Payments", None)
    deleted = {**_spend("Card Alpha", "2015-07-04", -100_000), "deleted": True}
    assert plan_cc_debt_coverage([card], [category], [], [deleted]) == [(category, 100_000)]


def test_rollover_inputs_excludes_deleted_and_other_months():
    from ynab_categorizer.budgeting import _rollover_inputs
    client = _assign_client()
    valid = _spend("Card Alpha", "2015-06-30", -1_000)
    client.get_transactions.return_value = [
        valid,
        _spend("Card Alpha", "2015-07-01", -2_000),
        _spend("Card Alpha", "2015-05-31", -3_000),
        {**_spend("Card Alpha", "2015-06-10", -4_000), "deleted": True},
    ]
    _, transactions = _rollover_inputs(client, "b", {"month": "2015-06-01"})
    assert transactions == [valid]


def test_hidden_categories_cover_overspending_without_funding_targets():
    from ynab_categorizer.budgeting import plan_overspending
    hidden = {**_cat("Hidden", "Needs", 50_000, balance=-10_000), "hidden": True}
    spending, adjusted = plan_overspending([hidden])
    assert spending == [(hidden, 10_000)]
    assert plan_assignments(100_000, adjusted, ["Needs"]) == []
    wants = {**hidden, "group_name": "Wants"}
    assert plan_phantom_assignments(100_000, [wants], {"Hidden": 50_000}) == []


def test_wants_largest_remainder_conserves_dollar_in_whole_cents():
    categories = [_want(str(i)) for i in range(3)]
    plan = plan_phantom_assignments(1_000, categories, {str(i): 1_000 for i in range(3)})
    assert [n for _, n in plan] == [340, 330, 330]
    assert sum(n for _, n in plan) == 1_000


def test_wants_rounding_never_exceeds_need_or_pot():
    categories = [_want("a"), _want("b")]
    plan = plan_phantom_assignments(35, categories, {"a": 19, "b": 29})
    assert [n for _, n in plan] == [10, 20]
    assert sum(n for _, n in plan) <= 35
    assert plan_phantom_assignments(9, categories, {"a": 100, "b": 100}) == []


def test_wants_respects_currency_precision():
    categories = [_want("a"), _want("b")]
    assert [n for _, n in plan_phantom_assignments(3_000, categories,
        {"a": 5_000, "b": 5_000}, {"currency_decimal_digits": 0})] == [2_000, 1_000]
    assert [n for _, n in plan_phantom_assignments(3, categories,
        {"a": 5, "b": 5}, {"currency_decimal_digits": 3})] == [2, 1]
    with pytest.raises(ValueError):
        plan_phantom_assignments(100, categories, {}, {"currency_decimal_digits": 4})
