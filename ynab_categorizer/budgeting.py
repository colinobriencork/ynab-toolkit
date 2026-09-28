"""Month-end budgeting helpers.

Two chores that aren't categorization but share the YNAB client:

1. assign_funds — pour Ready-to-Assign money into credit-card debt first
   (covering only debt that rolled over unfunded from last month — gaps from
   this month's own overspending are covered automatically when the overspent
   categories get funded), then current spending-category overspending, then
   into category targets in priority order (Bills, Needs, Savings, Wants), funding each to the
   amount YNAB says it's still underfunded this month.

2. phantom_assign — forward-plan discretionary money: project the biweekly
   paydays still to come this month, subtract what Bills/Needs/Savings and card
   debt still require, and assign the rest to Wants sized by recent average
   spending. Assigns money not yet received, so Ready-to-Assign goes negative in
   YNAB until the paydays land — that is the point.

Planning is pure (and unit-tested); the drivers default to a dry run and only
touch YNAB when called with apply=True.
"""

from calendar import monthrange
from datetime import date, timedelta
from statistics import median

from .budget_workflow import snapshot, execute_plan, check_snapshot, policy_checks, reserve_card_surplus
from .funding_holds import funding_categories
from .target_policy import target_report

# Funded in this order; a group is fully funded before the next gets anything.
# "Infrequent" is deliberately excluded — those are funded by hand. Budgets may
# decorate a group name with a parenthetical (e.g. "Savings (Reserved)");
# matching ignores that suffix so every budget's groups line up.
DEFAULT_PRIORITY_GROUPS = ["Bills", "Needs", "Savings", "Wants"]

# YNAB's built-in group holding one payment category per card, named after it.
CC_PAYMENTS_GROUP = "Credit Card Payments"

# The group phantom_assign plans for; everything in DEFAULT_PRIORITY_GROUPS
# before it is treated as committed money.
DISCRETIONARY_GROUP = "Wants"

# YNAB's category name for income inflows — how a paycheque is recognized.
INCOME_CATEGORY = "Inflow: Ready to Assign"
# An inflow cadence this many days apart (median) reads as biweekly pay.
BIWEEKLY_GAP_RANGE = (11, 17)
PAYDAY_LOOKBACK_DAYS = 120
AVG_SPEND_MONTHS = 3


# --- auto-assign ---------------------------------------------------------


def _in_group(cat: dict, group: str) -> bool:
    """Whether ``cat`` belongs to ``group``, ignoring a parenthetical suffix."""
    name = cat.get("group_name") or ""
    return name == group or name.startswith(group + " (")


def plan_assignments(to_be_budgeted: int, categories: list[dict], priority_groups):
    """Decide how much of ``to_be_budgeted`` to add to each category.

    Walks the groups in ``priority_groups`` order, and within each the categories
    as given, assigning each its ``goal_under_funded`` amount (capped by what's
    left). Categories with no underfunding are skipped. Returns a list of
    ``(category, amount)`` for everything that gets funded.
    """
    remaining = to_be_budgeted
    plan = []
    for group in priority_groups:
        for cat in categories:
            if remaining <= 0:
                return plan
            if cat.get("hidden") or not _in_group(cat, group):
                continue
            needed = cat.get("goal_under_funded") or 0
            if needed <= 0:
                continue
            amount = min(needed, remaining)
            plan.append((cat, amount))
            remaining -= amount
    return plan


def plan_cc_debt_coverage(
    accounts: list[dict],
    categories: list[dict],
    prev_month_categories: list[dict],
    month_transactions: list[dict],
):
    """Cover only card debt that rolled over unfunded from last month.

    The rollover is what the card owed at the end of last month beyond what its
    payment category held then. Gaps opened by the current month's own credit
    overspending are deliberately left alone: assigning to the overspent
    spending categories moves that money to the payment category automatically,
    so topping it up here as well would double-fund the card. Anything already
    assigned to the payment category this month counts toward the rollover, so
    re-runs don't cover the same debt twice. Returns ``(category, amount)``.
    """
    pay_cats = {
        c["name"]: c for c in categories if c.get("group_name") == CC_PAYMENTS_GROUP
    }
    prev_balances = {c["id"]: c.get("balance") or 0 for c in prev_month_categories}
    month_activity: dict = {}
    for t in month_transactions:
        if t.get("deleted"):
            continue
        acct_id = t.get("account_id")
        month_activity[acct_id] = month_activity.get(acct_id, 0) + t["amount"]

    plan = []
    for acct in accounts:
        if acct.get("type") != "creditCard" or acct.get("deleted"):
            continue
        cat = next((c for c in categories if c["id"] == acct.get("credit_card_payment_category_id")), None) or pay_cats.get(acct.get("name"))
        if not cat:
            continue
        # Card balance is negative when money is owed; backing out this month's
        # activity gives what it owed when the month rolled over.
        prev_debt = -((acct.get("balance") or 0) - month_activity.get(acct["id"], 0))
        rollover = prev_debt - prev_balances.get(cat["id"], 0)
        rollover -= cat.get("budgeted") or 0
        # Never assign past the card's actual gap (refunds may have shrunk it).
        shortfall = -(acct.get("balance") or 0) - (cat.get("balance") or 0)
        amount = min(rollover, shortfall)
        if amount > 0:
            plan.append((cat, amount))
    return plan


def _rollover_inputs(client, budget_id: str, month: dict):
    """Last month's category balances and this month's transactions — what
    plan_cc_debt_coverage needs to separate rolled-over debt from new spending."""
    first = date.fromisoformat(month["month"])
    prev_month = last_day_of_previous_month(first).replace(day=1).isoformat()
    prev_categories = client.get_month(budget_id, prev_month).get("categories") or []
    month_txns = client.get_transactions(budget_id, since_date=month["month"])
    month_end = first.replace(day=monthrange(first.year, first.month)[1]).isoformat()
    month_txns = [t for t in month_txns if not t.get("deleted")
                  and month["month"] <= t.get("date", "") <= month_end]
    return prev_categories, month_txns


def plan_overspending(categories: list[dict], available: int | None = None):
    """Cover negative spending balances; None means phantom-fund in full.

    Return the plan plus category copies reflecting its assignments, so later
    target and Wants planning count these dollars as already funded.
    """
    plan, adjusted = [], []
    remaining = max(0, available) if available is not None else None
    for cat in categories:
        updated = dict(cat)
        if cat.get("group_name") not in {CC_PAYMENTS_GROUP, "Internal Master Category"}:
            needed = max(0, -(cat.get("balance") or 0))
            amount = needed if remaining is None else min(needed, remaining)
            if amount:
                plan.append((cat, amount))
                if remaining is not None:
                    remaining -= amount
                updated["budgeted"] = (cat.get("budgeted") or 0) + amount
                updated["balance"] = (cat.get("balance") or 0) + amount
                updated["goal_under_funded"] = max(
                    0, (cat.get("goal_under_funded") or 0) - amount
                )
        adjusted.append(updated)
    return plan, adjusted


def combine_assignments(plan):
    """Sum additions per category, retaining the first (original) snapshot."""
    combined = {}
    for cat, amount in plan:
        previous = combined.get(cat["id"], (cat, 0))
        combined[cat["id"]] = (previous[0], previous[1] + amount)
    return list(combined.values())


def assign_funds(
    client, budget_id: str, priority_groups=DEFAULT_PRIORITY_GROUPS, apply: bool = False, options=None
):
    """Fund rollover debt, current overspending, then targets. Dry run
    unless apply=True."""
    options = options or {}
    before = snapshot(client, budget_id, options.get("month", "current"))
    month, categories, accounts = before["month"], before["categories"], before["accounts"]
    rta = month["to_be_budgeted"]
    prev_categories, month_txns = _rollover_inputs(client, budget_id, month)

    priority_groups = options.get("priority_groups", priority_groups)
    # Card debt comes off the top: whatever RTA it consumes, targets never see.
    plan = []
    remaining = rta
    for cat, needed in reserve_card_surplus(plan_cc_debt_coverage(
        accounts, categories, prev_categories, month_txns
    ), before):
        if remaining <= 0:
            break
        amount = min(needed, remaining)
        plan.append((cat, amount))
        remaining -= amount
    overspending_plan, target_categories = plan_overspending(categories, remaining)
    remaining -= sum(amount for _, amount in overspending_plan)
    plan += overspending_plan
    target_categories = funding_categories(target_categories, budget_id, month["month"],
                                          options.get("refill_rebalanced", False), options)
    plan += plan_assignments(remaining, target_categories, priority_groups)
    plan = combine_assignments(plan)
    options = {**options, 'report_metadata': {'target_funding': target_report(target_categories)}}

    if not plan:
        print(f"Ready to Assign: ${rta / 1000:.2f} — " +
              ("no cash available for additional funding." if rta <= 0 else "no target funding needed."))
    print(f"{'APPLYING' if apply else 'DRY RUN'} — funding {len(plan)} categories:")
    return execute_plan(client, budget_id, before, plan, apply, options=options)


# --- date helpers ----------------------------------------------------------


def last_day_of_previous_month(today: date) -> date:
    """The last calendar day of the month before ``today``."""
    last_month = today.replace(day=1) - timedelta(days=1)
    last_day = monthrange(last_month.year, last_month.month)[1]
    return date(last_month.year, last_month.month, last_day)


# --- phantom assign: forward-plan discretionary money ----------------------


def detect_biweekly_income(transactions: list[dict], today: date, month: date | None = None):
    """Project this month's remaining paydays from the paycheque record.

    A payee whose Ready-to-Assign inflows land a median of ~14 days apart is a
    biweekly paycheque; its future paydays are projected 14 days at a time from
    the last one received. Missed paydays invalidate the pattern; unusually
    large latest payments use the recent median to avoid projecting bonuses.
    Returns date-sorted ``(date, amount)`` pairs after ``today``,
    within the selected month. Payroll freshness is always checked as of today.
    """
    by_payee = {}
    for t in transactions:
        if t.get("deleted") or date.fromisoformat(t["date"]) > today:
            continue
        if (t.get("amount") or 0) <= 0:
            continue
        if t.get("category_name") != INCOME_CATEGORY or not t.get("payee_name"):
            continue
        by_payee.setdefault(t["payee_name"], []).append(t)

    month_start = (month or today).replace(day=1)
    month_end = date(month_start.year, month_start.month,
                     monthrange(month_start.year, month_start.month)[1])
    lo, hi = BIWEEKLY_GAP_RANGE
    paydays = []
    for txns in by_payee.values():
        if len(txns) < 3:
            continue
        txns.sort(key=lambda t: t["date"])
        dates = [date.fromisoformat(t["date"]) for t in txns]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        if not lo <= median(gaps) <= hi:
            continue
        if (today - dates[-1]).days > hi:
            continue  # Missing an expected payday invalidates extrapolation.
        baseline = int(median(t["amount"] for t in txns[-4:]))
        latest = txns[-1]["amount"]
        amount = latest if latest <= baseline * 1.2 else baseline
        nxt = dates[-1] + timedelta(days=14)
        while nxt <= month_end:
            if nxt > today and nxt >= month_start:
                paydays.append((nxt, amount))
            nxt += timedelta(days=14)
    return sorted(paydays)


def previous_months(today: date, n: int) -> list[str]:
    """The first-of-month strings for the ``n`` months before this one, oldest first."""
    months = []
    d = today.replace(day=1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
        months.append(d.isoformat())
    return list(reversed(months))


def average_monthly_spending(months: list[dict]) -> dict:
    """Average spending per category id across month snapshots, in milliunits.

    A month where a category netted an inflow (refunds exceeded spending) counts
    as zero spend, not negative — refunds shouldn't shrink the plan below what
    the other months say.
    """
    totals = {}
    for month in months:
        for c in month.get("categories", []):
            spent = max(0, -(c.get("activity") or 0))
            totals[c["id"]] = totals.get(c["id"], 0) + spent
    return {cid: total // max(1, len(months)) for cid, total in totals.items()}


def plan_phantom_assignments(pot: int, categories: list[dict], avg_spend: dict, options=None):
    """Size each Wants category's month at max(average spend, already spent),
    top up whatever isn't budgeted yet, and scale everything down
    proportionally if ``pot`` can't cover it. Returns ``(category, amount)``.
    """
    options = options or {}
    mode = options.get("wants_mode", "refill")
    if mode not in {"refill", "accumulate"}:
        raise ValueError("wants_mode must be refill or accumulate")
    needs = []
    for cat in categories:
        if cat.get("hidden") or cat.get("_rebalance_hold") or not _in_group(cat, options.get("discretionary_group", DISCRETIONARY_GROUP)):
            continue
        spent = max(0, -(cat.get("activity") or 0))
        overrides = options.get("wants_overrides", {})
        policy = cat.get('_target_funding')
        if (policy and cat.get('goal_type') and (cat.get('goal_target') or 0) > 0
                and cat['id'] not in overrides and cat['name'] not in overrides):
            needed = policy['additional_needed']
            if needed > 0:
                needs.append((cat, needed))
            continue
        allowance = overrides.get(cat["id"], overrides.get(cat["name"], avg_spend.get(cat["id"], 0)))
        month_plan = max(allowance, spent)
        funding = cat.get("budgeted") or 0
        if mode == "refill":
            funding = max(funding, (cat.get("balance") or 0) - (cat.get("activity") or 0))
        needed = month_plan - funding
        if needed > 0:
            needs.append((cat, needed))

    digits = options.get("currency_decimal_digits", 2)
    if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 3:
        raise ValueError("currency_decimal_digits must be an integer from 0 to 3")
    step = 10 ** (3 - digits)
    # Allocate whole currency units. Flooring each need prevents rounding above
    # a category's remaining requirement; largest remainders preserve the pot.
    units = [(cat, needed // step) for cat, needed in needs if needed >= step]
    total_units = sum(n for _, n in units)
    pot_units = max(0, pot) // step
    if total_units <= pot_units:
        return [(cat, n * step) for cat, n in units]
    amounts = [n * pot_units // total_units for _, n in units]
    remaining = pot_units - sum(amounts)
    order = sorted(range(len(units)),
                   key=lambda i: -(units[i][1] * pot_units % total_units))
    for i in order[:remaining]:
        amounts[i] += 1
    return [(cat, amount * step) for (cat, _), amount in zip(units, amounts) if amount]


def phantom_assign(client, budget_id: str, today: date, apply: bool = False, options=None):
    """Lay out the whole month from *expected* money. Dry run unless apply=True.

    Funds rollover debt, overspending, and committed targets in full, then sizes Wants
    from what the projected paydays leave over. Ready-to-Assign goes negative in
    YNAB by design. Unused forecast money and uncovered commitments are
    possible; actual balances are verified separately after applying.
    """
    options = options or {}
    before = snapshot(client, budget_id, options.get("month", "current"))
    month, categories, accounts = before["month"], before["categories"], before["accounts"]
    since = (today - timedelta(days=PAYDAY_LOOKBACK_DAYS)).isoformat()
    transactions = client.get_transactions(budget_id, since_date=since)
    rta = month["to_be_budgeted"]

    paydays = projected_income(transactions, today, options)
    future_income = sum(a for _, a in paydays)

    # Commitments are phantom-funded in full — never capped by cash on hand.
    prev_categories, month_txns = _rollover_inputs(client, budget_id, month)
    cc_plan = reserve_card_surplus(plan_cc_debt_coverage(accounts, categories, prev_categories, month_txns), before)
    cc_debt = sum(amt for _, amt in cc_plan)
    overspending_plan, adjusted_categories = plan_overspending(categories)
    adjusted_categories = funding_categories(adjusted_categories, budget_id, month["month"],
                                            options.get("refill_rebalanced", False), options)
    overspending = sum(amt for _, amt in overspending_plan)
    committed_groups = [g for g in options.get("priority_groups", DEFAULT_PRIORITY_GROUPS)
                        if g != options.get("discretionary_group", DISCRETIONARY_GROUP)]
    committed = sum(
        c.get("goal_under_funded") or 0
        for c in adjusted_categories
        if not c.get("hidden") and any(_in_group(c, g) for g in committed_groups)
    )
    committed_plan = plan_assignments(committed, adjusted_categories, committed_groups)

    pot = rta + future_income - cc_debt - overspending - committed

    print(f"Planning month: {month['month'][:7]} (balances as of {today})")
    if month["month"] > today.replace(day=1).isoformat():
        print("Provisional: remaining spending and month-end target rollover can change this plan.")
    print(f"Ready to Assign now:             ${rta / 1000:>10.2f}")
    for d, amount in paydays:
        print(f"+ expected payday {d}:     ${amount / 1000:>10.2f}")
    print(f"- current overspending:         ${overspending / 1000:>10.2f}")
    print(f"- Bills/Needs/Savings still due: ${committed / 1000:>10.2f}")
    print(f"- card debt to cover:            ${cc_debt / 1000:>10.2f}")
    print(f"= left for Wants:                ${pot / 1000:>10.2f}\n")

    if not paydays:
        print("(no future income projected — commitments may still exceed available cash)")
    if pot < 0:
        print("Warning: expected income doesn't cover commitments — "
              f"short ${-pot / 1000:.2f}. Funding commitments only.")

    wants_plan = []
    if pot > 0:
        avg = average_monthly_spending(
            [client.get_month(budget_id, m)
             for m in previous_months(today, AVG_SPEND_MONTHS)]
        )
        wants_plan = plan_phantom_assignments(pot, adjusted_categories, avg, options)


    plan = combine_assignments(cc_plan + overspending_plan + committed_plan + wants_plan)
    options = {**options, 'report_metadata': {'target_funding': target_report(adjusted_categories)}}
    print(f"{'APPLYING' if apply else 'DRY RUN'} — funding {len(plan)} categories:")
    print(f"Projected Ready to Assign: ${(rta - sum(n for _, n in plan)) / 1000:.2f}")
    return execute_plan(client, budget_id, before, plan, apply, future_income, options=options)


def projected_income(transactions, today, options=None):
    options = options or {}
    month = date.fromisoformat(options["month"]) if options.get("month") else today.replace(day=1)
    if "income_schedule" not in options:
        return detect_biweekly_income(transactions, today, month)
    result = []
    for payment in options["income_schedule"]:
        day = date.fromisoformat(payment["date"])
        amount = payment["amount"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("income_schedule amounts must be nonnegative integer milliunits")
        if today < day and (day.year, day.month) == (month.year, month.month):
            result.append((day, amount))
    return sorted(result)


def budget_check(client, budget_id, today=None, options=None):
    """Read-only actual balances and expected-income coverage."""
    today = today or date.today()
    options = options or {}
    state = snapshot(client, budget_id, options.get("month", "current"))
    txns = client.get_transactions(budget_id, since_date=(today - timedelta(days=PAYDAY_LOOKBACK_DAYS)).isoformat())
    checks = policy_checks(state, budget_id, sum(n for _, n in projected_income(txns, today, options)), options)
    checks["month"] = state["month"]["month"]
    print(f"Budget month: {checks['month'][:7]}")
    print(f"Ready to Assign: ${checks['ready_to_assign'] / 1000:.2f}")
    print(f"After expected income: ${checks['forecast_remaining'] / 1000:.2f}")
    print(f"Funding status: {checks['funding_status'].replace('_', ' ')}")
    print(f"Overspending: {len(checks['overspending'])} categories; underfunded targets: {len(checks['targets'])}")
    for c in checks["targets"]:
        print(f"Target still needed: {c['name']} ${c['amount'] / 1000:.2f}")
    for c in checks["overspending"]:
        print(f"Overspent: {c['name']} ${c['amount'] / 1000:.2f}")
    for c in checks["cards"]:
        print(f"Card funding difference: {c['name']} ${c['surplus'] / 1000:+.2f}")
    return checks
