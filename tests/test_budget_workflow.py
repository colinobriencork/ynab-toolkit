from copy import deepcopy
from datetime import date
import json

import pytest

from ynab_categorizer.budget_workflow import snapshot, execute_plan
from ynab_categorizer.budgeting import detect_biweekly_income, plan_phantom_assignments, projected_income


class Budget:
    def __init__(self):
        self.month = {"month": "2015-09-01", "to_be_budgeted": 100_000}
        self.cats = [
            dict(id="food", name="Food", group_name="Needs", budgeted=0, balance=-10_000),
            dict(id="a", name="Card Alpha", group_name="Credit Card Payments", budgeted=0, balance=120_000),
            dict(id="v", name="Card Beta", group_name="Credit Card Payments", budgeted=0, balance=70_000),
        ]
        self.accounts = [dict(id="a", name="Card Alpha", type="creditCard", balance=-100_000),
                         dict(id="v", name="Card Beta", type="creditCard", balance=-100_000)]
        self.calls = 0
        self.fail_at = None

    def get_month(self, *_):
        return deepcopy(self.month)

    def get_categories(self, *_):
        return deepcopy(self.cats)

    def get_accounts(self, *_):
        return deepcopy(self.accounts)

    def update_month_category(self, budget_id, month, category_id, budgeted):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("network failure")
        c = next(c for c in self.cats if c["id"] == category_id)
        delta = budgeted - c["budgeted"]
        c["budgeted"] = budgeted
        c["balance"] += delta
        self.month["to_be_budgeted"] -= delta
        if category_id == "food":
            self.cats[2]["balance"] += delta  # YNAB moves covered credit spending.


def test_reconcile_uses_actual_card_balances_after_spending(tmp_path):
    client = Budget()
    before = snapshot(client, "b")
    result = execute_plan(client, "b", before, [(before["categories"][0], 10_000)], True,
                          options={"report_path": str(tmp_path / "run.json")})
    assert result["status"] == "applied"
    assert [c["balance"] for c in client.cats] == [0, 100_000, 100_000]
    assert client.month["to_be_budgeted"] == 90_000
    assert result["checks"]["rta_matches"]
    assert all(c["surplus"] == 0 for c in result["checks"]["cards"])
    journal = json.loads((tmp_path / "run.json").read_text())
    assert [op["status"] for op in journal["operations"]] == ["completed"] * 3
    assert journal["operations"][1]["before"] == 0
    assert journal["operations"][1]["after"] == -20_000


def test_preview_explicit_report_records_plan_without_api_writes(tmp_path):
    client = Budget()
    path = tmp_path / "run.json"
    result = execute_plan(client, "b", snapshot(client, "b"), [], options={"report_path": str(path)})
    assert result["status"] == "preview"
    assert client.calls == 0
    assert json.loads(path.read_text())["status"] == "preview"


def test_partial_failure_records_uncertain_write_and_stops(tmp_path):
    client = Budget()
    client.fail_at = 3
    state = snapshot(client, "b")
    path = tmp_path / "run.json"
    result = execute_plan(client, "b", state, [(state["categories"][0], 10_000)], True,
                          options={"report_path": str(path)})
    assert result["status"] == "partial_failure"
    assert len(result["completed"]) == 2
    assert client.calls == 3
    assert client.month["to_be_budgeted"] == 110_000  # released funding remains in RTA
    journal = json.loads(path.read_text())
    assert journal["operations"][-1]["status"] == "writing"
    assert result["failed"]["write"]["id"] == "v"


def test_concurrent_change_stops_before_writing(tmp_path):
    client = Budget()
    state = snapshot(client, "b")
    client.month["to_be_budgeted"] -= 100
    result = execute_plan(client, "b", state, [], True, options={"report_path": str(tmp_path / "run.json")})
    assert result["status"] == "stale"
    assert client.calls == 0


def test_repeated_reconciliation_is_idempotent(tmp_path):
    client = Budget()
    state = snapshot(client, "b")
    execute_plan(client, "b", state, [(state["categories"][0], 10_000)], True,
                 options={"report_path": str(tmp_path / "one.json")})
    calls = client.calls
    result = execute_plan(client, "b", snapshot(client, "b"), [], True,
                          options={"report_path": str(tmp_path / "two.json")})
    assert result["status"] == "applied"
    assert client.calls == calls


def payroll(days, amounts):
    return [dict(date=d, amount=n, payee_name="Employer", category_name="Inflow: Ready to Assign")
            for d, n in zip(days, amounts)]


def test_stale_payroll_is_not_projected():
    txns = payroll(["2015-06-05", "2015-06-19", "2015-07-03"], [24_000] * 3)
    assert detect_biweekly_income(txns, date(2015, 9, 7)) == []


def test_bonus_does_not_double_forecast():
    txns = payroll(["2015-08-07", "2015-08-21", "2015-09-04"], [24_000, 24_000, 48_000])
    assert detect_biweekly_income(txns, date(2015, 9, 7)) == [(date(2015, 9, 18), 24_000)]


def test_explicit_income_schedule_replaces_detection():
    assert projected_income([], date(2015, 9, 7), {"income_schedule": [
        {"date": "2015-09-07", "amount": 10}, {"date": "2015-09-30", "amount": 20},
        {"date": "2015-10-01", "amount": 30}]}) == [(date(2015, 9, 30), 20)]


def test_wants_can_refill_or_accumulate_with_override():
    c = dict(id="crafts", name="Crafts", group_name="Fun", budgeted=0, activity=-100_000, balance=400_000)
    options = {"discretionary_group": "Fun"}
    assert plan_phantom_assignments(1_000_000, [c], {"crafts": 300_000}, options) == []
    assert plan_phantom_assignments(1_000_000, [c], {"crafts": 300_000}, {**options, "wants_mode": "accumulate"}) == [(c, 300_000)]
    assert plan_phantom_assignments(1_000_000, [c], {}, {**options, "wants_overrides": {"Crafts": 600_000}}) == [(c, 100_000)]


def test_snapshot_includes_hidden_categories_and_matches_cards_by_id():
    client = Budget()
    client.accounts[0]["credit_card_payment_category_id"] = "a"
    client.cats[1]["name"] = "Renamed payment category"
    full = deepcopy(client.cats)
    full.append(dict(id="hidden", name="Hidden expense", hidden=True, budgeted=0, balance=-5_000))
    client.month["categories"] = full
    client.cats = client.cats[:1]
    state = snapshot(client, "b")
    from ynab_categorizer.budget_workflow import check_snapshot
    checks = check_snapshot(state)
    assert any(c["id"] == "hidden" for c in checks["overspending"])
    assert any(c["id"] == "a" and c["surplus"] == 20_000 for c in checks["cards"])


def test_intervening_assignment_edit_is_not_overwritten(tmp_path):
    client = Budget()
    before = snapshot(client, "b")
    original_get_month = client.get_month
    count = 0

    def get_month(*args):
        nonlocal count
        count += 1
        if count == 2:  # first pre-write read, after the initial stale check
            client.cats[0]["budgeted"] = 9_000
        return original_get_month(*args)

    client.get_month = get_month
    result = execute_plan(client, "b", before, [(before["categories"][0], 10_000)], True,
                          options={"report_path": str(tmp_path / "run.json")})
    assert result["status"] == "failed"
    assert client.calls == 0
    assert client.cats[0]["budgeted"] == 9_000


def test_errors_do_not_copy_sensitive_response_details(tmp_path):
    client = Budget()
    def fail(*args, **kwargs):
        raise ValueError("Bearer secret-token response payload")
    client.update_month_category = fail
    state = snapshot(client, "b")
    path = tmp_path / "run.json"
    result = execute_plan(client, "b", state, [(state["categories"][0], 10_000)], True,
                          options={"report_path": str(path)})
    assert result["failed"]["error"] == "ValueError"
    assert "secret-token" not in path.read_text()
