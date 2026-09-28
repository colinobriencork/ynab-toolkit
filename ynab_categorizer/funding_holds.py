"""Keep month-end releases from being immediately refilled by the planners."""

import hashlib
import json
import os
from pathlib import Path

from .audit import write_report
from .target_policy import resolve_categories, target_funding


def _path(budget_id, month):
    key = hashlib.sha256(f"{budget_id}:{month}".encode()).hexdigest()
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "ynab-categorizer" / "funding-holds" / f"{key}.json"


def held_categories(budget_id, month):
    path = _path(budget_id, month)
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    if data.get("budget_id") != budget_id or data.get("month") != month:
        raise ValueError("Funding hold does not match budget and month")
    ids = data["category_ids"]
    if not isinstance(ids, list) or not all(isinstance(cid, str) for cid in ids):
        raise ValueError("Invalid funding hold categories")
    return set(ids)


def hold_category(budget_id, month, category_id):
    # Persist before the API write: a timeout can leave its outcome uncertain.
    ids = held_categories(budget_id, month) | {category_id}
    write_report({"budget_id": budget_id, "month": month, "category_ids": sorted(ids)},
                 _path(budget_id, month))


def funding_categories(categories, budget_id, month, refill=False, options=None):
    """Respect monthly spending caps, protected accumulation, and prior releases.

    Available minus activity is this month's funding including carryover. Using
    it instead of Assigned avoids counting money twice, before or after rollover.
    Only monthly NEED targets can be interpreted as a monthly spending allowance.
    """
    options = dict(options or {})
    held = set() if refill else held_categories(budget_id, month)
    options['_accumulating_ids'] = set(resolve_categories(categories, options.get('accumulate_categories', [])).values())
    result = []
    for original in categories:
        c = dict(original)
        c['_target_funding'] = target_funding(c, month, options)
        c['goal_under_funded'] = c['_target_funding']['additional_needed']
        if c['id'] in held:
            c.update(goal_under_funded=0, _rebalance_hold=True)
            c['_target_funding'].update(rule='released_this_month', additional_needed=0)
        result.append(c)
    return result
