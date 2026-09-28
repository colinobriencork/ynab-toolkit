"""Move unused current-month category funding to recorded overspending."""

from calendar import monthrange
from collections import defaultdict
from datetime import date

from .budget_workflow import CC_GROUP, execute_plan, snapshot
from .budgeting import _in_group, combine_assignments
from .target_policy import accumulating, regular_spending_target, resolve_categories
from .funding_holds import funding_categories


def _resolve(categories, selectors):
    return resolve_categories(categories, selectors)


def scheduled_categories(scheduled, accounts, month):
    """Protect the entire category if an outgoing scheduled item is still due."""
    account_map = {a['id']: a for a in accounts}
    first = date.fromisoformat(month)
    end = first.replace(day=monthrange(first.year, first.month)[1]).isoformat()
    protected = set()
    for txn in scheduled:
        if txn.get('deleted') or not account_map.get(txn['account_id'], {}).get('on_budget'):
            continue
        if txn['date_next'] > end:
            continue
        for part in txn.get('subtransactions') or [txn]:
            if part.get('deleted') or part['amount'] >= 0:
                continue
            cid = part.get('category_id')
            if cid:
                protected.add(cid)
            elif not part.get('transfer_account_id'):
                raise ValueError('Categorize the scheduled outflow due this month before rebalancing')
    return protected


def plan_rebalance(state, transactions, scheduled, options=None):
    options = dict(options or {})
    categories = state['categories']
    options['_accumulating_ids'] = set(_resolve(categories, options.get('accumulate_categories', [])).values())
    protected = set(_resolve(categories, options.get('rebalance_protected_categories', [])).values())
    last = set(_resolve(categories, options.get('rebalance_last_categories', [])).values())
    keep = options.get('rebalance_keep', {})
    minimums = {cid: keep[key] for key, cid in _resolve(categories, keep).items()}
    scheduled_ids = scheduled_categories(scheduled, state['accounts'], state['month']['month'])
    digits = options.get('currency_decimal_digits', 2)
    if type(digits) is not int or digits not in range(4):
        raise ValueError('currency_decimal_digits must be 0, 1, 2, or 3')
    step = 10 ** (3 - digits)
    accounts = {a['id']: a for a in state['accounts']}
    credit_spending = defaultdict(int)
    month = state['month']['month'][:7]
    for txn in transactions:
        account = accounts.get(txn['account_id'], {})
        if (txn.get('deleted') or txn['date'][:7] != month
                or not account.get('on_budget') or account.get('type') != 'creditCard'):
            continue
        for part in txn.get('subtransactions') or [txn]:
            if not part.get('deleted'):
                credit_spending[part.get('category_id')] -= part['amount']

    donors, recipients, review = [], [], []
    for c in categories:
        if c.get('deleted') or c.get('internal') or c.get('group_name') in {CC_GROUP, 'Internal Master Category'}:
            continue
        balance = c.get('balance') or 0
        if balance < 0:
            credit = min(-balance, max(0, credit_spending[c['id']]))
            cash = -balance - credit
            for kind, amount in [('cash', cash), ('credit', credit)]:
                amount = amount // step * step
                if amount:
                    recipients.append({'category': c, 'kind': kind, 'need': amount,
                                       'last': c['id'] in last})
            continue
        if balance == 0:
            continue
        reason = None
        if c.get('hidden'):
            reason = 'hidden category'
        elif c['id'] in protected or accumulating(c, options):
            reason = 'protected category/group'
        elif c['id'] in scheduled_ids:
            reason = 'scheduled expense still due this month'
        elif c.get('goal_type') and not regular_spending_target(c):
            reason = 'balance-building or longer-term target'
        floor = minimums.get(c['id'], 0)
        available = 0 if reason else max(0, balance - floor) // step * step
        row = {'id': c['id'], 'name': c['name'], 'balance': balance,
               'keep': balance if reason else floor, 'eligible': available,
               'released': 0, 'reason': reason or ('minimum retained' if not available else 'available above minimum')}
        review.append(row)
        if available:
            donors.append((c, row))

    # Discretionary money first, then needs, then bills. Stable ordering makes
    # repeat previews understandable; recipients prioritize cash and largest gaps.
    order = ['Wants', 'Needs', 'Bills']
    donors.sort(key=lambda item: (next((i for i, g in enumerate(order) if _in_group(item[0], g)), 3), item[0]['name']))
    recipients.sort(key=lambda r: (r['kind'] != 'cash', r['last'], -r['need'], r['category']['name']))
    total_needed = sum(r['need'] for r in recipients)
    remaining = min(total_needed, sum(row['eligible'] for _, row in donors))
    plan, moves = [], []
    donor_index = 0
    for recipient in recipients:
        recipient['funded'] = 0
        needed = recipient['need']
        while needed and remaining:
            donor, row = donors[donor_index]
            available = row['eligible'] - row['released']
            amount = min(available, needed, remaining)
            plan.extend([(donor, -amount), (recipient['category'], amount)])
            row['released'] += amount
            recipient['funded'] += amount
            needed -= amount
            remaining -= amount
            moves.append({'source_id': donor['id'], 'source': donor['name'],
                          'destination_id': recipient['category']['id'],
                          'destination': recipient['category']['name'], 'amount': amount,
                          'kind': recipient['kind']})
            if row['released'] == row['eligible']:
                donor_index += 1
    # Release every donor before funding recipients; interrupted receiving writes
    # leave the released money in Ready to Assign and in the recovery journal.
    plan = sorted(combine_assignments(plan), key=lambda pair: pair[1] >= 0)
    assert sum(amount for _, amount in plan) == 0
    for row in review:
        row['after'] = row['balance'] - row['released']
    destinations = [{'id': r['category']['id'], 'name': r['category']['name'],
                     'kind': r['kind'], 'shortfall': r['need'], 'funded': r['funded'],
                     'remaining': r['need'] - r['funded'], 'last': r['last']} for r in recipients]
    return plan, {'donors': review, 'destinations': destinations, 'moves': moves,
                  'total_moved': sum(m['amount'] for m in moves),
                  'remaining_overspending': sum(r['remaining'] for r in destinations)}


def rebalance(client, budget_id, apply=False, options=None, today=None):
    options = dict(options or {})
    today = today or date.today()
    before = snapshot(client, budget_id)
    month = before['month']['month']
    if month != today.replace(day=1).isoformat():
        raise ValueError('Rebalance only supports the current month')
    if before['month']['to_be_budgeted'] < 0:
        raise ValueError('Resolve negative Ready to Assign before redistributing category balances')
    transactions = client.get_transactions(budget_id, since_date=month)
    scheduled = client.get_scheduled_transactions(budget_id)
    plan, analysis = plan_rebalance(before, transactions, scheduled, options)
    first = date.fromisoformat(month)
    next_month = date(first.year + (first.month == 12), first.month % 12 + 1, 1).isoformat()
    future = client.get_month(budget_id, next_month)
    if future['month'] != next_month:
        raise ValueError('Next-month snapshot does not match the requested month')
    metadata = {c['id']: c for c in before['categories']}
    future_categories = [{**metadata.get(c['id'], {}), **c} for c in future['categories']]
    original = funding_categories(future_categories, budget_id, next_month, options=options)
    released = {r['id']: r['released'] for r in analysis['donors'] if r['released']}
    adjusted = [{**c, 'balance': c['balance'] - released.get(c['id'], 0)} for c in future_categories]
    revised = {c['id']: c for c in funding_categories(adjusted, budget_id, next_month, options=options)}
    effects = [{'id': c['id'], 'name': c['name'],
                'new_funding_before': c['goal_under_funded'],
                'new_funding_after': revised[c['id']]['goal_under_funded'],
                'increase': revised[c['id']]['goal_under_funded'] - c['goal_under_funded']}
               for c in original if c['id'] in released]
    analysis['next_month'] = next_month
    analysis['next_month_target_effects'] = effects
    analysis['next_month_target_funding_increase'] = sum(r['increase'] for r in effects)
    print(f"{'APPLYING' if apply else 'PREVIEW'} — rebalance {month[:7]}")
    print('Use at month end after recording spending; minimums must cover expenses not scheduled in YNAB.')
    for row in analysis['donors']:
        print(f"  {row['name']}: available ${row['balance']/1000:.2f}, "
              f"release ${row['released']/1000:.2f}, left ${row['after']/1000:.2f} ({row['reason']})")
    for move in analysis['moves']:
        print(f"  ${move['amount']/1000:.2f}: {move['source']} -> {move['destination']} ({move['kind']})")
    print(f"Total moved: ${analysis['total_moved']/1000:.2f}; "
          f"remaining overspending: ${analysis['remaining_overspending']/1000:.2f}")
    print('Ready to Assign is preserved. Savings and protected balances are not donors.')
    print(f"Next month's target top-ups increase by ${analysis['next_month_target_funding_increase']/1000:.2f} "
          'if these transfers are applied; next-month planning uses the resulting carryover.')
    options['month'] = month
    options['minimum_balances'] = {r['id']: r['keep'] for r in analysis['donors'] if r['released']}
    options['hold_rebalanced_funding'] = True
    options['report_metadata'] = {'rebalance': analysis}

    def guard(category_id):
        due = scheduled_categories(client.get_scheduled_transactions(budget_id), before['accounts'], month)
        if category_id in due:
            raise RuntimeError('A scheduled expense now needs the donor balance')

    options['donor_guard'] = guard
    result = execute_plan(client, budget_id, before, plan, apply, options=options)
    result['rebalance'] = analysis
    return result
