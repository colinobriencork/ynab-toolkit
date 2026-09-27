"""Read-only snapshots and auditable, non-atomic budget application."""
from copy import deepcopy

from .audit import write_report

CC_GROUP = "Credit Card Payments"


def snapshot(client, budget_id):
    month = client.get_month(budget_id)
    visible = client.get_categories(budget_id)
    accounts = client.get_accounts(budget_id)
    metadata = {c["id"]: c for c in visible}
    group_names = {c.get("category_group_id"): c.get("group_name") for c in visible
                   if c.get("category_group_id")}
    card_ids = {a.get("credit_card_payment_category_id") for a in accounts
                if a.get("type") == "creditCard"}
    categories = []
    for c in month.get("categories", visible):
        if c.get("deleted"):
            continue
        merged = {**metadata.get(c["id"], {}), **c}
        merged["group_name"] = (CC_GROUP if c["id"] in card_ids else
                                merged.get("group_name") or group_names.get(c.get("category_group_id"), ""))
        categories.append(merged)
    return deepcopy({"month": month, "categories": categories, "accounts": accounts})


def card_positions(state):
    cats = {c["name"]: c for c in state["categories"] if c.get("group_name") == CC_GROUP}
    by_id = {c["id"]: c for c in state["categories"]}
    positions = []
    for a in state["accounts"]:
        if a.get("type") != "creditCard" or a.get("deleted"):
            continue
        c = by_id.get(a.get("credit_card_payment_category_id")) or cats.get(a.get("name"))
        if c:
            owed = max(0, -(a.get("balance") or 0))
            positions.append((c, (c.get("balance") or 0) - owed))
    return positions


def reconciliation_plan(state):
    """Transfer only actual card surplus; never invent new payment money."""
    positions = card_positions(state)
    donors = [[c, n] for c, n in positions if n > 0]
    moves = []
    for receiver, gap in positions:
        needed = max(0, -gap)
        for donor in donors:
            amount = min(needed, donor[1])
            if amount:
                moves.append((donor[0], receiver, amount))
                donor[1] -= amount
                needed -= amount
    return moves


def reserve_card_surplus(plan, state):
    """Avoid buying rollover coverage already held by another payment category."""
    surplus = sum(max(0, n) for _, n in card_positions(state))
    result = []
    for c, amount in plan:
        covered = min(amount, surplus)
        surplus -= covered
        if amount > covered:
            result.append((c, amount - covered))
    return result


def check_snapshot(state, expected_income=0):
    return {
        "ready_to_assign": state["month"]["to_be_budgeted"],
        "expected_income": expected_income,
        "forecast_remaining": state["month"]["to_be_budgeted"] + expected_income,
        "targets": [{"id": c["id"], "name": c["name"], "amount": c["goal_under_funded"]}
                    for c in state["categories"] if (c.get("goal_under_funded") or 0) > 0],
        "funding_status": ("uncovered" if state["month"]["to_be_budgeted"] + expected_income < 0 else
                           "depends_on_expected_income" if state["month"]["to_be_budgeted"] < 0 else "cash_backed"),
        "overspending": [{"id": c["id"], "name": c["name"], "amount": -c["balance"]}
                         for c in state["categories"]
                         if c.get("group_name") not in {CC_GROUP, "Internal Master Category"}
                         and (c.get("balance") or 0) < 0],
        "cards": [{"id": c["id"], "name": c["name"], "surplus": gap}
                  for c, gap in card_positions(state)],
    }


def execute_plan(client, budget_id, before, plan, apply=False, expected_income=0, options=None):
    """Record every successful write and stop on first failure; no blind rollback.

    Card redistribution is deliberately computed only after spending assignments
    have reached YNAB. Dry runs display current-state possibilities, not promises.
    """
    options = options or {}
    result = {"status": "preview", "before": before, "after": None,
              "planned": [{"id": c["id"], "name": c["name"], "delta": n} for c, n in plan],
              "completed": [], "failed": None, "checks": None}
    for c, amount in plan:
        print(f"  {amount / 1000:+10.2f} -> {c.get('group_name', '')}: {c['name']}")
    if not apply:
        moves = reconciliation_plan(before)
        for donor, receiver, amount in moves:
            print(f"  Possible card transfer: ${amount / 1000:.2f} {donor['name']} -> {receiver['name']} (rechecked after funding)")
        print("(dry run — re-run with --apply to assign; card transfers depend on refreshed balances)")
        result["checks"] = check_snapshot(before, expected_income)
        result["projected_rta"] = before["month"]["to_be_budgeted"] - sum(n for _, n in plan)
        if options.get("report_path"):
            result["report_path"] = write_report({"kind": "budget_assignment", "budget_id": budget_id,
                "status": "preview", "operations": [], "planned": result["planned"],
                "checks": result["checks"], "projected_rta": result["projected_rta"]}, options["report_path"])
        return result

    options = options or {}
    journal = {"kind": "budget_assignment", "budget_id": budget_id, "operations": [], "status": "applying"}
    report_path = options.get("report_path")
    current_write = None

    def save():
        nonlocal report_path
        report_path = write_report(journal, report_path)
        result["report_path"] = report_path

    def write(category_id, before_amount, after_amount, stage):
        live_month = client.get_month(budget_id)
        if live_month["month"] != month:
            raise RuntimeError("Budget month changed")
        live_categories = live_month.get("categories")
        if live_categories is None:
            live_categories = client.get_categories(budget_id)
        live = next((c for c in live_categories if c["id"] == category_id), None)
        if live is None or (live.get("budgeted") or 0) != before_amount:
            raise RuntimeError("Category assignment changed before write")
        operation = {"kind": "category", "budget_id": budget_id, "month": month,
                     "category_id": category_id, "before": before_amount,
                     "after": after_amount, "status": "writing", "stage": stage}
        journal["operations"].append(operation)
        save()
        client.update_month_category(budget_id, month, category_id, budgeted=after_amount)
        operation["status"] = "completed"
        save()

    try:
        # Reject a stale preview before performing any writes.
        fresh = snapshot(client, budget_id)
        if fresh != before:
            result["status"] = "stale"
            print("Budget changed while planning; no changes applied. Run again.")
            return result
        month = before["month"]["month"]
        for c, amount in plan:
            if not amount:
                continue
            current_write = {"id": c["id"], "budgeted": (c.get("budgeted") or 0) + amount,
                             "delta": amount, "stage": "funding"}
            write(c["id"], c.get("budgeted") or 0, current_write["budgeted"], "funding")
            result["completed"].append(current_write)
        fresh = snapshot(client, budget_id)
        if fresh["month"]["month"] != month:
            raise RuntimeError("Month changed during application; reconciliation stopped")
        assigned = {c["id"]: c.get("budgeted") or 0 for c in fresh["categories"]}
        for donor, receiver, amount in reconciliation_plan(fresh):
            # Release first: a failed receiving write leaves money safely in RTA.
            for c, delta in ((donor, -amount), (receiver, amount)):
                current_write = {"id": c["id"], "budgeted": assigned[c["id"]] + delta,
                                 "delta": delta, "stage": "reconciliation"}
                write(c["id"], assigned[c["id"]], current_write["budgeted"], "reconciliation")
                assigned[c["id"]] = current_write["budgeted"]
                result["completed"].append(current_write)
        current_write = None
        result["after"] = snapshot(client, budget_id)
        result["checks"] = check_snapshot(result["after"], expected_income)
        expected_rta = before["month"]["to_be_budgeted"] - sum(w["delta"] for w in result["completed"])
        result["checks"]["month_matches"] = result["after"]["month"]["month"] == month
        result["checks"]["rta_matches"] = result["checks"]["ready_to_assign"] == expected_rta
        actual = {c["id"]: c.get("budgeted") or 0 for c in result["after"]["categories"]}
        last_writes = {w["id"]: w["budgeted"] for w in result["completed"]}
        result["checks"]["assignments_match"] = all(actual.get(cid) == n for cid, n in last_writes.items())
        result["status"] = "applied" if result["checks"]["month_matches"] and result["checks"]["rta_matches"] and result["checks"]["assignments_match"] else "verification_failed"
        print(f"Verified Ready to Assign: ${result['checks']['ready_to_assign'] / 1000:.2f} ({result['status']})")
        for card in result["checks"]["cards"]:
            if card["surplus"]:
                print(f"  Card funding difference: {card['name']} ${card['surplus'] / 1000:+.2f}")
    except Exception as exc:
        result["status"] = "partial_failure" if result["completed"] else "failed"
        result["failed"] = {"write": current_write, "error": type(exc).__name__}
        print(f"Stopped: {len(result['completed'])} writes completed; {type(exc).__name__}. Rerun budget-check before retrying.")
    journal["status"] = result["status"]
    journal["checks"] = result["checks"]
    journal["failed"] = result["failed"]
    if report_path is not None:
        try:
            save()
        except OSError as exc:
            result["journal_error"] = type(exc).__name__
            print(f"Could not finalize run journal: {type(exc).__name__}")
    return result
