"""Historical category corrections and guarded recovery from run journals."""

import json
from datetime import date
from pathlib import Path

from .audit import write_report
from .categorizer import resolve_category


def _month_categories(client, budget_id, month):
    data = client.get_month(budget_id, month)
    return data, {c['id']: c for c in data['categories']}


def _current_value(client, op):
    if op['kind'] == 'transaction':
        txn = client.get_transaction(op['budget_id'], op['transaction_id'])
        if txn.get('deleted'):
            raise ValueError('Transaction was deleted')
        return {key: txn.get(key) for key in op['before']}
    if op['kind'] == 'category':
        _, cats = _month_categories(client, op['budget_id'], op['month'])
        return cats[op['category_id']]['budgeted']
    raise ValueError('Unsupported journal operation')


def _write(client, op):
    if op['kind'] == 'transaction':
        return client.update_transaction(op['budget_id'], op['transaction_id'], **op['after'])
    return client.update_month_category(op['budget_id'], op['month'], op['category_id'], budgeted=op['after'])


def _execute(client, report, apply, report_path):
    """Recheck each intended field immediately before writing, journaling progress."""
    report['status'] = 'preview'
    if not apply:
        if report_path:
            report['report_path'] = write_report(report, report_path)
        return report
    path = write_report(report, report_path)
    report['report_path'] = path
    report['status'] = 'applying'
    try:
        for op in report['operations']:
            if op['status'] == 'completed':
                continue
            if op.get('guard'):
                current_txn = client.get_transaction(op['budget_id'], op['transaction_id'])
                if any(current_txn.get(k) != v for k, v in op['guard'].items()):
                    raise ValueError('Transaction details changed since preview')
            if _current_value(client, op) != op['before']:
                raise ValueError('Budget changed since preview; remaining changes stopped')
            op['status'] = 'writing'
            write_report(report, path)
            _write(client, op)
            op['status'] = 'completed'
            write_report(report, path)
        report['status'] = 'completed'
    except Exception as exc:
        # A response can be lost after YNAB accepted a write. Do not assume rollback.
        for op in report['operations']:
            if op['status'] == 'writing':
                op['status'] = 'uncertain'
        report['status'] = 'incomplete'
        report['error'] = type(exc).__name__
        print('Run incomplete. Inspect the journal and current YNAB state before retrying.')
    write_report(report, path)
    return report


def correct_transaction(client, budget_id, transaction_id, category, apply=False, report_path=None):
    """Move one unsplit expense and matching assignment within its original month."""
    txn = client.get_transaction(budget_id, transaction_id)
    if txn.get('deleted') or txn.get('transfer_account_id') or txn.get('subtransactions') or txn['amount'] >= 0:
        raise ValueError('Correction requires a non-transfer, unsplit expense')
    target = resolve_category(category, client.get_categories(budget_id))
    if not target:
        raise ValueError('Destination category is missing or ambiguous')
    month = txn['date'][:7] + '-01'
    before, cats = _month_categories(client, budget_id, month)
    source_id = txn.get('category_id')
    if source_id not in cats or target['id'] not in cats:
        raise ValueError('Both categories must exist in the transaction month')
    if source_id == target['id']:
        raise ValueError('Transaction already uses the requested category')
    for cat in (cats[source_id], cats[target['id']]):
        if cat.get('internal') or cat.get('category_group_name') in {'Internal Master Category', 'Credit Card Payments'}:
            raise ValueError('Correction must use spending categories')
    amount = -txn['amount']
    operations = [{
        'kind': 'transaction', 'budget_id': budget_id, 'transaction_id': transaction_id,
        'before': {'category_id': source_id}, 'after': {'category_id': target['id']}, 'status': 'planned',
        'guard': {key: txn.get(key) for key in ('amount', 'date', 'account_id', 'subtransactions', 'transfer_account_id', 'deleted')},
    }]
    for cid, delta in [(source_id, -amount), (target['id'], amount)]:
        operations.append({'kind': 'category', 'budget_id': budget_id, 'month': month,
                           'category_id': cid, 'before': cats[cid]['budgeted'],
                           'after': cats[cid]['budgeted'] + delta, 'status': 'planned'})
    report = {'command': 'correct', 'budget_id': budget_id, 'operations': operations}
    print(f"{'Applying' if apply else 'Preview'}: move {amount / 1000:.2f} and {month} assignment to {target['name']}")
    if apply and any(_current_value(client, op) != op['before'] for op in operations):
        raise ValueError('Budget changed while planning correction; no writes applied')
    report = _execute(client, report, apply, report_path)
    if apply and report['status'] == 'completed':
        try:
            after, updated = _month_categories(client, budget_id, month)
            report['checks'] = {
                'transaction_category': client.get_transaction(budget_id, transaction_id)['category_id'] == target['id'],
                'rta_unchanged': after['to_be_budgeted'] == before['to_be_budgeted'],
                'available_unchanged': all(updated[cid]['balance'] == cats[cid]['balance'] for cid in (source_id, target['id'])),
            }
            # Historical recategorization can redistribute card funding even
            # when the spending categories and RTA remain unchanged.
            from .budget_workflow import snapshot, reconciliation_plan
            state = snapshot(client, budget_id)
            assigned = {c['id']: c['budgeted'] for c in state['categories']}
            for donor, receiver, transfer in reconciliation_plan(state):
                for cat, delta in ((donor, -transfer), (receiver, transfer)):
                    cid = cat['id']
                    report['operations'].append({
                        'kind': 'category', 'budget_id': budget_id,
                        'month': state['month']['month'], 'category_id': cid,
                        'before': assigned[cid], 'after': assigned[cid] + delta,
                        'status': 'planned', 'stage': 'reconciliation',
                    })
                    assigned[cid] += delta
            if any(op['status'] == 'planned' for op in report['operations']):
                report = _execute(client, report, True, report['report_path'])
            final = snapshot(client, budget_id)
            final_assigned = {c['id']: c['budgeted'] for c in final['categories']}
            report['checks']['reconciliation_rta_unchanged'] = final['month']['to_be_budgeted'] == state['month']['to_be_budgeted']
            report['checks']['reconciliation_assignments'] = all(final_assigned.get(cid) == n for cid, n in assigned.items())
            from .budgeting import budget_check
            report['current_budget_check'] = budget_check(client, budget_id, today=date.today())
            if not all(report['checks'].values()):
                report['status'] = 'verification_failed'
        except Exception as exc:
            report['status'] = 'verification_failed'
            report['error'] = type(exc).__name__
        write_report(report, report['report_path'])
    print(f"Result: {report['status']}")
    return report


def restore_run(client, path, apply=False, report_path=None):
    """Reverse completed operations only if all affected fields still match the run."""
    original = json.loads(Path(path).expanduser().read_text())
    ops = original.get('operations')
    if not isinstance(ops, list) or not ops:
        raise ValueError('Journal contains no restorable operations')
    if any(op.get('status') in {'writing', 'uncertain'} for op in ops):
        raise ValueError('Journal has uncertain writes; inspect YNAB before restoring')
    reverse = []
    expected = {}
    # Validate the complete reversal before writing any field. A field may have
    # several journal entries (e.g. initial funding then card reconciliation).
    for op in reversed(ops):
        if op.get('status') != 'completed':
            continue
        key = (op['kind'], op['budget_id'], op.get('month'), op.get('category_id'), op.get('transaction_id'))
        current = expected[key] if key in expected else _current_value(client, op)
        if current != op['after']:
            raise ValueError('Current values differ from this run; refusing to overwrite later edits')
        expected[key] = op['before']
        reverse.append({**op, 'before': op['after'], 'after': op['before'], 'status': 'planned'})
    if not reverse:
        raise ValueError('Journal has no completed changes')
    print(f"{'Restoring' if apply else 'Preview restore'}: {len(reverse)} changes")
    report = _execute(client, {'command': 'restore', 'source_report': str(path), 'operations': reverse}, apply, report_path)
    if apply and report['status'] == 'completed':
        try:
            checks = []
            # Verify final expected values, rather than intermediate duplicate writes.
            for key, value in expected.items():
                op = next(o for o in reverse if (o['kind'], o['budget_id'], o.get('month'), o.get('category_id'), o.get('transaction_id')) == key)
                checks.append(_current_value(client, op) == value)
            report['verified'] = all(checks)
            if not report['verified']:
                report['status'] = 'verification_failed'
        except Exception as exc:
            report['status'] = 'verification_failed'
            report['error'] = type(exc).__name__
        write_report(report, report['report_path'])
    print(f"Result: {report['status']}")
    return report
