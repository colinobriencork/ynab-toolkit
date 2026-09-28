from copy import deepcopy
from datetime import date
import json

import pytest

from ynab_categorizer.budget_workflow import snapshot, execute_plan
from ynab_categorizer.budgeting import assign_funds, phantom_assign, plan_phantom_assignments
from ynab_categorizer.funding_holds import funding_categories, held_categories
from ynab_categorizer.rebalance import plan_rebalance, rebalance
from ynab_categorizer.target_policy import target_funding


class Budget:
    def __init__(self):
        self.month = {'month': '2015-09-01', 'to_be_budgeted': 0}
        self.cats = [
            dict(id='spare', name='Monthly service', group_name='Bills', budgeted=70_000,
                 activity=-20_000, balance=50_000, goal_type='NEED', goal_cadence=1,
                 goal_under_funded=0, goal_target=70_000),
            dict(id='save', name='Reserve', group_name='Savings (Reserved)', budgeted=0,
                 activity=0, balance=200_000),
            dict(id='food', name='Food', group_name='Needs', budgeted=0,
                 activity=-30_000, balance=-30_000),
            dict(id='cash', name='Utilities', group_name='Bills', budgeted=0,
                 activity=-10_000, balance=-10_000),
            dict(id='card', name='Card', group_name='Credit Card Payments', budgeted=0,
                 activity=0, balance=30_000),
        ]
        self.accounts = [dict(id='checking', name='Checking', type='checking', on_budget=True, balance=260_000),
                         dict(id='card', name='Card', type='creditCard', on_budget=True, balance=-60_000)]
        self.transactions = [dict(id='expense', account_id='card', category_id='food',
                                  amount=-30_000, date='2015-09-10')]
        self.scheduled = []
        self.writes = []
        self.fail_at = None

    def get_month(self, budget, month='current'):
        if month != 'current' and month != self.month['month']:
            return {'month': month, 'to_be_budgeted': 0, 'categories': [
                {**deepcopy(c), 'budgeted': 0, 'activity': 0, 'balance': max(0, c['balance']),
                 'goal_under_funded': c.get('goal_target', 0)} for c in self.cats]}
        return {**deepcopy(self.month), 'categories': deepcopy(self.cats)}

    def get_categories(self, budget):
        return deepcopy(self.cats)

    def get_accounts(self, budget):
        return deepcopy(self.accounts)

    def get_transactions(self, *args, **kwargs):
        return deepcopy(self.transactions)

    def get_scheduled_transactions(self, *args):
        return deepcopy(self.scheduled)

    def update_month_category(self, budget, month, category_id, budgeted):
        self.writes.append((month, category_id, budgeted))
        if self.fail_at == len(self.writes):
            raise RuntimeError('unavailable')
        c = next(c for c in self.cats if c['id'] == category_id)
        delta = budgeted - c['budgeted']
        c['budgeted'] = budgeted
        if category_id == 'food':
            self.cats[-1]['balance'] += min(max(0, -c['balance']), max(0, delta))
        c['balance'] += delta
        self.month['to_be_budgeted'] -= delta


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))


def test_protections_floors_cash_priority_and_no_double_funding():
    client = Budget()
    plan, report = plan_rebalance(snapshot(client, 'b'), client.transactions, [],
                                 {'rebalance_keep': {'Monthly service': 25_000}})
    assert [(c['id'], n) for c, n in plan] == [('spare', -25_000), ('cash', 10_000), ('food', 15_000)]
    assert report['total_moved'] == 25_000
    assert report['remaining_overspending'] == 15_000
    assert next(r for r in report['donors'] if r['id'] == 'save')['released'] == 0
    assert sum(n for _, n in plan) == 0


@pytest.mark.parametrize('fields,options', [
    ({'hidden': True}, {}),
    ({'goal_type': 'TBD'}, {}),
    ({'goal_type': 'MF'}, {}),
    ({'goal_cadence': 13}, {}),
    ({'goal_cadence': 1, 'goal_cadence_frequency': 2}, {}),
    ({'group_name': 'Infrequent'}, {}),
    ({}, {'rebalance_protected_categories': ['spare']}),
    ({}, {'rebalance_protected_groups': ['Bills']}),
    ({}, {'group_roles': {'Bills': 'saving'}}),
])
def test_protected_and_long_term_donors_are_never_used(fields, options):
    client = Budget()
    client.cats[0].update(fields)
    plan, report = plan_rebalance(snapshot(client, 'b'), client.transactions, [], options)
    assert plan == []
    assert report['total_moved'] == 0


def test_scheduled_split_expense_protects_whole_category():
    client = Budget()
    client.scheduled = [dict(account_id='checking', date_next='2015-09-30', amount=-3_000,
                             subtransactions=[dict(category_id='spare', amount=-3_000)])]
    assert plan_rebalance(snapshot(client, 'b'), client.transactions, client.scheduled)[0] == []
    client.scheduled[0]['date_next'] = '2015-10-01'
    assert plan_rebalance(snapshot(client, 'b'), client.transactions, client.scheduled)[0]


def test_uncategorized_scheduled_expense_fails_closed():
    client = Budget()
    with pytest.raises(ValueError, match='scheduled outflow'):
        plan_rebalance(snapshot(client, 'b'), [], [dict(account_id='checking',
                       date_next='2015-09-30', amount=-3_000)])


@pytest.mark.parametrize('options', [
    {'rebalance_protected_categories': ['typo']}, {'rebalance_keep': {'typo': 3}},
    {'rebalance_last_categories': ['typo']},
])
def test_unknown_policy_categories_fail_closed(options):
    client = Budget()
    with pytest.raises(ValueError, match='exactly once'):
        plan_rebalance(snapshot(client, 'b'), [], [], options)


def test_refunds_splits_tracking_and_last_priority():
    client = Budget()
    client.cats[0]['balance'] = 15_000
    client.cats.insert(2, dict(id='other', name='Other', group_name='Needs', balance=-10_000, budgeted=0))
    client.transactions.extend([
        dict(id='refund', date='2015-09-11', account_id='card', category_id='food', amount=5_000),
        dict(id='split', date='2015-09-11', account_id='card', amount=-10_000,
             subtransactions=[dict(category_id='other', amount=-10_000)]),
        dict(id='tracking', date='2015-09-11', account_id='unknown', category_id='cash', amount=-50_000),
    ])
    plan, report = plan_rebalance(snapshot(client, 'b'), client.transactions, [],
                                 {'rebalance_last_categories': ['food']})
    # The refund leaves Food with 25 credit + 5 cash overspending. Cash comes first,
    # even for a deferred reimbursement category. No cash is misclassified as credit.
    assert [(r['id'], r['kind'], r['funded']) for r in report['destinations']] == [
        ('cash', 'cash', 10_000), ('food', 'cash', 5_000),
        ('other', 'credit', 0), ('food', 'credit', 0)]


def test_currency_precision_does_not_overdraw_floor():
    client = Budget()
    client.cats[0]['balance'] = 10_019
    plan, report = plan_rebalance(snapshot(client, 'b'), client.transactions, [], {'rebalance_keep': {'spare': 10}})
    assert report['total_moved'] == 10_000
    assert next(r for r in report['donors'] if r['id'] == 'spare')['after'] == 19


def test_live_workflow_preview_apply_verification_repeat_and_holds(tmp_path):
    client = Budget()
    options = {'report_path': tmp_path / 'report.json'}
    preview = rebalance(client, 'b', options=options, today=date(2015, 9, 30))
    assert preview['status'] == 'preview'
    assert client.writes == []
    assert held_categories('b', '2015-09-01') == set()
    assert json.loads(options['report_path'].read_text())['rebalance']['total_moved'] == 40_000
    effect = preview['rebalance']['next_month_target_effects'][0]
    assert effect['new_funding_before'] == 20_000
    assert effect['new_funding_after'] == 60_000
    assert effect['increase'] == 40_000
    result = rebalance(client, 'b', apply=True, options=options, today=date(2015, 9, 30))
    assert result['status'] == 'applied'
    assert [c['balance'] for c in client.cats] == [10_000, 200_000, 0, 0, 60_000]
    assert client.month['to_be_budgeted'] == 0
    assert result['checks']['cards'][0]['surplus'] == 0
    assert result['checks']['minimum_balances_preserved']
    assert held_categories('b', '2015-09-01') == {'spare'}
    assert held_categories('b', '2015-10-01') == set()
    count = len(client.writes)
    rebalance(client, 'b', apply=True, options=options, today=date(2015, 9, 30))
    assert len(client.writes) == count


def test_failed_receiving_write_leaves_released_cash_and_recovery_journal(tmp_path):
    client = Budget()
    client.fail_at = 2
    path = tmp_path / 'report.json'
    result = rebalance(client, 'b', apply=True, options={'report_path': path}, today=date(2015, 9, 30))
    assert result['status'] == 'partial_failure'
    assert client.month['to_be_budgeted'] == 40_000
    journal = json.loads(path.read_text())
    assert [op['status'] for op in journal['operations']] == ['completed', 'writing']
    assert journal['rebalance']['total_moved'] == 40_000


def test_donor_spending_between_snapshot_and_write_stops_transfer(tmp_path):
    client = Budget()
    before = snapshot(client, 'b')
    original = client.get_month
    calls = 0
    def get_month(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            client.cats[0]['balance'] = 5_000
        return original(*args)
    client.get_month = get_month
    result = execute_plan(client, 'b', before, [(before['categories'][0], -20_000)], True,
                          options={'minimum_balances': {'spare': 10_000},
                                   'report_path': tmp_path / 'report.json'})
    assert result['status'] == 'failed'
    assert client.writes == []


def test_new_scheduled_expense_before_apply_stops_donor(tmp_path):
    client = Budget()
    calls = 0
    def schedule(*args):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [dict(account_id='checking', category_id='spare',
                                           date_next='2015-09-30', amount=-5_000)]
    client.get_scheduled_transactions = schedule
    result = rebalance(client, 'b', apply=True, options={'report_path': tmp_path / 'report.json'}, today=date(2015, 9, 30))
    assert result['status'] == 'failed'
    assert client.writes == []


def test_negative_rta_and_noncurrent_month_are_rejected():
    client = Budget()
    with pytest.raises(ValueError, match='current month'):
        rebalance(client, 'b', today=date(2015, 10, 1))
    client.month['to_be_budgeted'] = -1
    with pytest.raises(ValueError, match='negative Ready'):
        rebalance(client, 'b', today=date(2015, 9, 30))
    assert client.writes == []


def test_assign_and_phantom_respect_month_scoped_holds(tmp_path, monkeypatch):
    client = Budget()
    rebalance(client, 'b', apply=True, options={'report_path': tmp_path / 'apply.json'}, today=date(2015, 9, 30))
    client.month['to_be_budgeted'] = 50_000
    client.cats[0]['goal_under_funded'] = 40_000
    monkeypatch.setattr('ynab_categorizer.budgeting._rollover_inputs', lambda *a: (client.cats, []))
    assert assign_funds(client, 'b')['planned'] == []
    assert phantom_assign(client, 'b', date(2015, 9, 30))['planned'] == []
    assert assign_funds(client, 'b', options={'refill_rebalanced': True})['planned'][0]['delta'] == 40_000
    held = funding_categories([{**client.cats[0], 'group_name': 'Wants'}], 'b', '2015-09-01')
    assert plan_phantom_assignments(50_000, held, {'spare': 60_000}) == []


def spending_target(**changes):
    return dict(dict(id='service', name='Service', group_name='Bills',
                     goal_type='NEED', goal_cadence=1, goal_cadence_frequency=1,
                     goal_target=80_000, goal_under_funded=80_000,
                     budgeted=0, activity=0, balance=25_000), **changes)


def test_shared_refill_rule_counts_carryover_and_spending_once():
    beginning = target_funding(spending_target(), '2015-10-01')
    assert beginning['additional_needed'] == 55_000
    paid = spending_target(budgeted=55_000, activity=-45_000, balance=35_000)
    assert target_funding(paid, '2015-10-01')['additional_needed'] == 0
    # A release this month changes next month's need through actual carryover.
    assert target_funding(spending_target(balance=10_000), '2015-10-01')['additional_needed'] == 70_000


@pytest.mark.parametrize('fields', [
    {'group_name': 'Savings (Reserved)'}, {'group_name': 'Infrequent'},
    {'goal_type': 'MF'}, {'goal_type': 'TB'}, {'goal_type': 'TBD'},
    {'goal_cadence': 13}, {'goal_cadence': 1, 'goal_cadence_frequency': 2},
])
def test_contributions_and_longer_term_targets_keep_period_installments(fields):
    c = spending_target(**fields, goal_under_funded=12_000)
    assert target_funding(c, '2015-10-01')['additional_needed'] == 12_000


def test_weekly_target_counts_due_days_in_selected_month():
    c = spending_target(goal_cadence=2, goal_day=4, goal_target=10_000, balance=5_000)
    # Five Thursdays in October, four in November.
    assert target_funding(c, '2015-10-01')['additional_needed'] == 45_000
    assert target_funding(c, '2015-11-01')['additional_needed'] == 35_000


def test_explicit_accumulation_and_ynab_mode_are_respected():
    cats = funding_categories([spending_target()], 'b', '2015-10-01',
                              options={'accumulate_categories': ['Bills: Service']})
    assert cats[0]['goal_under_funded'] == 80_000
    assert target_funding(spending_target(), '2015-10-01',
                          {'spending_target_mode': 'ynab'})['additional_needed'] == 80_000
    assert target_funding(spending_target(group_name='Long term'), '2015-10-01',
                          {'group_roles': {'Long term': 'saving'}})['additional_needed'] == 80_000


def test_positive_discretionary_target_uses_same_remaining_need_as_bills():
    cats = funding_categories([spending_target(group_name='Wants')], 'b', '2015-10-01')
    plan = plan_phantom_assignments(100_000, cats, {'service': 200_000})
    assert plan[0][1] == 55_000


def test_snoozed_target_does_not_request_new_funding():
    c = spending_target(goal_snoozed_at='2015-10-10T00:00:00Z')
    assert target_funding(c, '2015-10-01')['additional_needed'] == 0
    assert target_funding(c, '2015-11-01')['additional_needed'] == 55_000


def test_budget_checks_and_planners_use_the_same_target_need(tmp_path):
    from ynab_categorizer.budget_workflow import policy_checks
    client = Budget()
    client.cats = [spending_target()]
    client.accounts = []
    state = snapshot(client, 'b')
    checks = policy_checks(state, 'b')
    assert checks['targets'][0]['amount'] == 55_000
    assert checks['target_funding'][0]['additional_needed'] == 55_000
