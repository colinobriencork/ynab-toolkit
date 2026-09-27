from unittest.mock import MagicMock

from ynab_categorizer.reporting import spending_report


def test_report_separates_roles_splits_transfers_and_inflows():
    client = MagicMock()
    client.get_categories.return_value = [
        {'id': 'food', 'name': 'Food', 'group_name': 'Needs'},
        {'id': 'save', 'name': 'Retirement', 'group_name': 'Investments'},
        {'id': 'income', 'name': 'Inflow: Ready to Assign', 'group_name': 'Internal Master Category'},
    ]
    client.get_month.return_value = {'categories': []}
    client.get_transactions.return_value = [
        {'id': 'pay', 'date': '2015-06-01', 'category_id': 'income', 'amount': 1000000},
        {'id': 'split', 'date': '2015-06-02', 'amount': -150000, 'payee_name': 'Shop',
         'subtransactions': [{'category_id': 'food', 'amount': -100000}, {'category_id': 'save', 'amount': -50000}]},
        {'id': 'refund', 'date': '2015-06-03', 'category_id': 'food', 'amount': 20000},
        {'id': 'transfer', 'date': '2015-06-04', 'amount': -500000, 'transfer_account_id': 'card'},
        {'id': 'future', 'date': '2015-07-01', 'category_id': 'food', 'amount': -999999},
    ]
    result = spending_report(client, 'b', '2015-06-01', '2015-06-30', {'group_roles': {'Investments': 'saving'}})
    assert result['totals'] == {'spending': 80000, 'saving': 50000, 'repayment': 0}
    assert result['income'] == 1000000
    assert result['transfers']['outflow'] == 500000
    assert result['mixed_category_payees']['Shop'] == ['Food', 'Retirement']


def test_categorized_tracking_transfer_counts_as_saving():
    client = MagicMock()
    client.get_categories.return_value = [{'id': 'save', 'name': 'Retirement', 'group_name': 'Investments'}]
    client.get_month.return_value = {'categories': []}
    client.get_transactions.return_value = [
        {'id': 'out', 'date': '2015-06-01', 'amount': -100000, 'category_id': 'save', 'transfer_account_id': 'tracking'},
        {'id': 'in', 'date': '2015-06-01', 'amount': 100000, 'category_id': None, 'transfer_account_id': 'checking'},
    ]
    report = spending_report(client, 'b', '2015-06-01', '2015-06-30', {'group_roles': {'Investments': 'saving'}})
    assert report['totals']['saving'] == 100000
    assert report['totals']['spending'] == 0
