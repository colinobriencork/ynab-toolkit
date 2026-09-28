"""Shared target intent for allocating money and releasing unused funding."""

from calendar import monthrange
from datetime import date


def in_group(category, name):
    group = category.get('group_name') or ''
    return group == name or group.startswith(name + ' (')


def resolve_categories(categories, selectors):
    result = {}
    for selector in selectors:
        matches = [c for c in categories if selector in (
            c['id'], c['name'], f"{c.get('group_name', '')}: {c['name']}")]
        if len(matches) != 1:
            raise ValueError(f'Policy category must match exactly once: {selector}')
        result[selector] = matches[0]['id']
    return result


def accumulating(category, options):
    groups = ['Savings', 'Infrequent', *options.get('rebalance_protected_groups', [])]
    return (any(in_group(category, g) for g in groups)
            or options.get('group_roles', {}).get(category.get('group_name')) in {'saving', 'repayment', 'exclude'}
            or category['id'] in options.get('_accumulating_ids', set()))


def regular_spending_target(category):
    return (category.get('goal_type') == 'NEED'
            and category.get('goal_cadence') in (1, 2)
            and (category.get('goal_cadence_frequency') or 1) == 1)


def target_funding(category, month, options=None):
    """Return target intent and new funding needed, in integer milliunits.

    Ordinary spending counts carryover and this month's activity. Accumulating
    and multi-month goals retain YNAB's period-aware installment calculation.
    """
    options = options or {}
    balance = category.get('balance') or 0
    activity = category.get('activity') or 0
    target = category.get('goal_target') or 0
    needed = max(0, category.get('goal_under_funded') or 0)
    rule, requirement, funded = 'ynab_target', target, (category.get('budgeted') or 0)
    if accumulating(category, options):
        rule = 'accumulate'
    elif regular_spending_target(category) and options.get('spending_target_mode', 'refill') == 'refill':
        rule = 'refill_spending'
        first = date.fromisoformat(month)
        if category.get('goal_cadence') == 2 and target:
            weekday = category.get('goal_day')
            if type(weekday) is not int or weekday not in range(7):
                raise ValueError('Weekly spending target needs a valid goal_day')
            occurrences = sum((date(first.year, first.month, d).weekday() + 1) % 7 == weekday
                              for d in range(1, monthrange(first.year, first.month)[1] + 1))
            requirement *= occurrences
        funded = balance - activity
        needed = max(0, requirement - funded)
    elif category.get('goal_type'):
        rule = 'periodic_or_accumulating_target'
    snoozed = category.get('goal_snoozed_at') or ''
    if snoozed[:7] == month[:7]:
        rule, needed = 'snoozed', 0
    return {'rule': rule, 'target': target, 'period_requirement': requirement,
            'available': balance, 'activity': activity,
            'funding_including_carry': balance - activity,
            'assigned': category.get('budgeted') or 0,
            'additional_needed': needed}


def target_report(categories):
    return [{'id': c['id'], 'name': c['name'], **c['_target_funding']}
            for c in categories if c.get('goal_type') and '_target_funding' in c]
