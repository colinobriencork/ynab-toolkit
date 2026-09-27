"""Read-only spending analysis, with explicit category roles and data-quality flags."""

from collections import defaultdict
from datetime import date


VALID_ROLES = {'spending', 'saving', 'repayment', 'exclude'}


def spending_report(client, budget_id, start, end, options=None):
    options = options or {}
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError('Start date must be on or before end date')
    roles = options.get('group_roles', {})
    if any(role not in VALID_ROLES for role in roles.values()):
        raise ValueError('Group roles must be spending, saving, repayment, or exclude')
    cats = {c['id']: c for c in client.get_categories(budget_id)}
    # Include hidden historical categories; visibility must not change actual spending.
    month = first.replace(day=1)
    while month <= last:
        for c in client.get_month(budget_id, month.isoformat()).get('categories', []):
            cats.setdefault(c['id'], {**c, 'group_name': c.get('category_group_name', 'Unknown')})
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    rows = defaultdict(lambda: {'outflow': 0, 'inflow': 0, 'count': 0})
    income = 0
    transfer_out = transfer_in = 0
    flags = []
    payee_categories = defaultdict(set)
    for txn in client.get_transactions(budget_id, since_date=start):
        if txn.get('deleted') or not start <= txn['date'] <= end:
            continue
        if txn.get('approved') is False:
            flags.append({'transaction_id': txn['id'], 'reason': 'unapproved'})
        for part in txn.get('subtransactions') or [txn]:
            if part.get('deleted'):
                continue
            amount = part['amount']
            if part.get('transfer_account_id') and not part.get('category_id'):
                transfer_out += max(0, -amount)
                transfer_in += max(0, amount)
                continue
            cat = cats.get(part.get('category_id'), {})
            name = cat.get('name', part.get('category_name') or 'Uncategorized')
            group = cat.get('group_name', 'Unknown')
            if group == 'Internal Master Category' and name == 'Inflow: Ready to Assign':
                income += amount
                continue
            if group in {'Credit Card Payments', 'Internal Master Category'}:
                continue
            role = roles.get(group, 'spending')
            if role == 'exclude':
                continue
            key = (role, group, name)
            rows[key]['outflow'] += max(0, -amount)
            rows[key]['inflow'] += max(0, amount)
            rows[key]['count'] += 1
            payee = part.get('payee_name') or txn.get('payee_name') or 'Unknown'
            payee_categories[payee].add(name)
            if not cat:
                flags.append({'transaction_id': txn['id'], 'reason': 'unknown or uncategorized category'})
    categories = [dict(role=role, group=group, category=name, **values,
                       net=values['outflow']-values['inflow'])
                  for (role, group, name), values in rows.items()]
    categories.sort(key=lambda c: c['net'], reverse=True)
    totals = {role: sum(c['net'] for c in categories if c['role'] == role)
              for role in ('spending', 'saving', 'repayment')}
    result = {'budget_id': budget_id, 'start': start, 'end': end,
              'income': income, 'totals': totals, 'categories': categories,
              'transfers': {'outflow': transfer_out, 'inflow': transfer_in},
              'flags': flags, 'mixed_category_payees': {p: sorted(v) for p, v in payee_categories.items() if len(v)>1}}
    print(f'Spending report: {start} through {end} (net of category inflows)')
    print(f'Income: {income / 1000:.2f}')
    for role, amount in totals.items():
        print(f'{role.title()}: {amount / 1000:.2f}')
    for row in categories:
        print(f"  {row['role']}: {row['group']} / {row['category']}: {row['net']/1000:.2f}")
    print('Uncategorized account transfers excluded; categorized transfers follow group_roles. Category inflows may be refunds or reimbursements.')
    print('Configure group_roles to distinguish savings and repayments from consumption.')
    print(f"Review: {len(flags)} data flags; {len(result['mixed_category_payees'])} merchants use multiple categories (not necessarily errors).")
    return result
