import json
from copy import deepcopy

import pytest

from ynab_categorizer.audit import write_report
from ynab_categorizer.operations import correct_transaction, restore_run


class Client:
    def __init__(self):
        self.txn = {'id': 't', 'date': '2015-06-19', 'amount': -17000,
                    'category_id': 'dining', 'approved': True}
        self.cats = {
            'dining': {'id': 'dining', 'name': 'Dining', 'group_name': 'Wants', 'budgeted': 43000, 'balance': 26000},
            'clothing': {'id': 'clothing', 'name': 'Clothing', 'group_name': 'Needs', 'budgeted': 0, 'balance': 0},
        }
        self.rta = 100000
        self.writes = []

    def get_transaction(self, *args):
        return deepcopy(self.txn)

    def get_categories(self, *args):
        return deepcopy(list(self.cats.values()))

    def get_month(self, *args):
        return {'month': '2015-06-01', 'to_be_budgeted': self.rta, 'categories': self.get_categories()}

    def get_accounts(self, *args):
        return []

    def update_transaction(self, budget, txn, **fields):
        self.writes.append(('transaction', fields))
        if 'category_id' in fields:
            self.cats[self.txn['category_id']]['balance'] -= self.txn['amount']
            self.cats[fields['category_id']]['balance'] += self.txn['amount']
        self.txn.update(fields)
        return deepcopy(self.txn)

    def update_month_category(self, budget, month, cid, budgeted):
        self.writes.append((cid, budgeted))
        delta = budgeted - self.cats[cid]['budgeted']
        self.cats[cid]['budgeted'] = budgeted
        self.cats[cid]['balance'] += delta
        self.rta -= delta
        return deepcopy(self.cats[cid])


def test_correction_preserves_balances_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr('ynab_categorizer.budgeting.budget_check', lambda *a, **kw: {'ok': True})
    client = Client()
    before = deepcopy(client.cats)
    path = tmp_path / 'run.json'
    result = correct_transaction(client, 'b', 't', 'Clothing', apply=True, report_path=path)
    assert result['status'] == 'completed'
    assert all(result['checks'].values())
    assert client.txn['category_id'] == 'clothing'
    assert client.txn['approved'] is True
    result = restore_run(client, path, apply=True, report_path=tmp_path / 'restore.json')
    assert result['verified']
    assert client.cats == before
    assert client.txn['category_id'] == 'dining'


def test_correction_preview_does_not_write():
    client = Client()
    result = correct_transaction(client, 'b', 't', 'Clothing')
    assert result['status'] == 'preview'
    assert not client.writes


def test_restore_refuses_later_edits_before_any_write(tmp_path):
    client = Client()
    op = {'kind': 'category', 'budget_id': 'b', 'month': '2015-06-01', 'category_id': 'clothing',
          'before': 0, 'after': 17000, 'status': 'completed'}
    path = write_report({'operations': [op]}, tmp_path / 'run.json')
    with pytest.raises(ValueError, match='later edits'):
        restore_run(client, path, apply=True)
    assert not client.writes


def test_failed_write_is_journaled_uncertain(tmp_path):
    client = Client()
    def fail(*args, **kwargs):
        raise TimeoutError('response lost')
    client.update_transaction = fail
    path = tmp_path / 'run.json'
    result = correct_transaction(client, 'b', 't', 'Clothing', apply=True, report_path=path)
    assert result['status'] == 'incomplete'
    assert json.loads(path.read_text())['operations'][0]['status'] == 'uncertain'
    with pytest.raises(ValueError, match='uncertain'):
        restore_run(client, path)


def test_private_journal_permissions(tmp_path):
    path = tmp_path / 'report.json'
    write_report({'operations': []}, path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_correction_reconciles_displaced_card_funding_in_same_journal(monkeypatch, tmp_path):
    monkeypatch.setattr('ynab_categorizer.budgeting.budget_check', lambda *a, **kw: {})
    client = Client()
    for cid in ('card_a', 'card_b'):
        client.cats[cid] = {'id': cid, 'name': cid, 'group_name': 'Credit Card Payments',
                            'budgeted': 100000, 'balance': 100000}
    client.get_accounts = lambda *a: [
        {'id': cid, 'name': cid, 'type': 'creditCard', 'balance': -100000,
         'credit_card_payment_category_id': cid} for cid in ('card_a', 'card_b')
    ]
    update = client.update_transaction
    def recategorize(*args, **kwargs):
        result = update(*args, **kwargs)
        client.cats['card_a']['balance'] += 6000
        client.cats['card_b']['balance'] -= 6000
        return result
    client.update_transaction = recategorize
    result = correct_transaction(client, 'b', 't', 'Clothing', apply=True, report_path=tmp_path / 'run.json')
    assert result['status'] == 'completed'
    assert client.cats['card_a']['balance'] == client.cats['card_b']['balance'] == 100000
    assert len(result['operations']) == 5
    assert client.rta == 100000


def test_correction_stops_if_amount_changed_before_write(tmp_path):
    client = Client()
    get_txn = client.get_transaction
    reads = 0
    def changed(*args):
        nonlocal reads
        reads += 1
        result = get_txn(*args)
        if reads >= 3:
            result['amount'] = -999000
        return result
    client.get_transaction = changed
    result = correct_transaction(client, 'b', 't', 'Clothing', apply=True, report_path=tmp_path / 'run.json')
    assert result['status'] == 'incomplete'
    assert not client.writes
