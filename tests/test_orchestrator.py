"""Tests for the orchestrator workflow.

These test the high-level behaviour: given a set of transactions,
the orchestrator coordinates the client and categorizer correctly.
"""

import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from ynab_categorizer.orchestrator import Orchestrator
from ynab_categorizer.backends import BackendError


@pytest.fixture(autouse=True)
def private_test_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _make_orchestrator(
    unapproved, categories=None, past=None, budgets=None, enricher=None,
    amazon_budget_name="My Budget",
):
    client = MagicMock()
    client.get_budgets.return_value = budgets or [{"id": "b1", "name": "My Budget"}]
    client.get_categories.return_value = categories or [
        {"id": "c1", "name": "Dining Out", "group_name": "Lifestyle"},
    ]
    for txn in (past or []) + unapproved:
        txn.setdefault('account_id', 'checking')
    client.get_accounts.return_value = [{'id': 'checking', 'on_budget': True}]
    client.get_transactions.return_value = list({t['id']: t for t in (past or []) + unapproved}.values())
    state = {t["id"]: dict(t) for t in unapproved}
    client.get_transaction.side_effect = lambda budget, txn_id: dict(state[txn_id])
    def update(budget, txn_id, **fields):
        state[txn_id].update(fields)
        return dict(state[txn_id])
    client.update_transaction.side_effect = update

    categorizer = MagicMock()
    # Default: the second-pass guess can't resolve, so deferred txns end up skipped
    # unless a test overrides this. Keeps tests that don't care about pass 2 clean.
    categorizer.guess.return_value = {"action": "unclear", "raw": ""}
    orch = Orchestrator(
        client, categorizer, enricher=enricher, amazon_budget_name=amazon_budget_name
    )
    return orch, client, categorizer


def test_no_unapproved_transactions_exits_early(capsys):
    orch, client, categorizer = _make_orchestrator(unapproved=[])
    orch.run()

    output = capsys.readouterr().out
    assert "all caught up" in output.lower()
    categorizer.suggest.assert_not_called()


def test_confident_categorization_auto_approves():
    txn = {"id": "t1", "payee_name": "Example Cafe", "amount": -4500, "date": "2014-03-10"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])

    categorizer.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_first_pass_never_asks_the_user_interactively(monkeypatch):
    """An unsure first pass must defer to Opus, not block on input()."""
    txn = {"id": "t1", "payee_name": "Example Cafe", "amount": -4500, "date": "2014-03-10"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])

    categorizer.suggest.return_value = {"action": "ask", "question": "Are you travelling?"}
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    def boom(_prompt):
        raise AssertionError("the run must not prompt the user mid-pass")

    monkeypatch.setattr("builtins.input", boom)

    orch.run()  # must not raise

    categorizer.guess.assert_called_once()  # the unsure txn went to the second pass
    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_group_prefixed_category_name_resolves_and_approves():
    """LLM returns 'Wants: 🪴 Hobbies' — should resolve to the category and approve."""
    txn = {"id": "t1", "payee_name": "Example Hobby Shop", "amount": -6000, "date": "2014-03-10"}
    categories = [
        {"id": "c-hobbies", "name": "🪴 Hobbies", "group_name": "Wants"},
        {"id": "c-dining", "name": "🍽️ Dining out", "group_name": "Wants"},
    ]
    orch, client, categorizer = _make_orchestrator(unapproved=[txn], categories=categories)

    categorizer.suggest.return_value = {
        "action": "categorize",
        "category_name": "Wants: 🪴 Hobbies",
    }

    orch.run()

    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c-hobbies", approved=True
    )


def test_emoji_category_name_without_group_prefix_resolves():
    """LLM returns '🛒 Groceries' (no group prefix) — should still resolve."""
    txn = {"id": "t1", "payee_name": "Example Grocer", "amount": -7000, "date": "2014-03-13"}
    categories = [
        {"id": "c-groceries", "name": "🛒 Groceries", "group_name": "Needs"},
    ]
    orch, client, categorizer = _make_orchestrator(unapproved=[txn], categories=categories)

    categorizer.suggest.return_value = {
        "action": "categorize",
        "category_name": "🛒 Groceries",
    }

    orch.run()

    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c-groceries", approved=True
    )


def test_bare_amazon_without_order_details_is_surfaced_not_guessed(capsys):
    """A bare 'Amazon' charge with no item data is opaque — surface it for manual
    categorization rather than blind-guessing (which used to default to Groceries)."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -11250, "date": "2014-03-01"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    categorizer.suggest.assert_not_called()  # not run through the first (Haiku) pass
    categorizer.guess.assert_not_called()  # and NOT blind-guessed either
    client.update_transaction.assert_not_called()
    out = capsys.readouterr().out
    assert "need your input" in out.lower()
    # The session is fine here (nothing failed) — don't blame it / tell them to log in.
    assert "amazon-login" not in out


def test_descriptive_amazon_charge_is_still_guessed():
    """'Amazon Prime Video' names what it is, so the guesser may still categorize it."""
    txn = {
        "id": "t1", "payee_name": "Amazon Prime Video", "amount": -1500,
        "date": "2015-06-18",
    }
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    categorizer.guess.assert_called_once()
    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_amazon_txn_is_enriched_and_categorized_when_order_matches():
    """With an enricher that resolves the order, Amazon txns get categorized."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -11250, "date": "2014-03-01"}
    enricher = MagicMock()
    enricher.enrich.return_value = "USB-C cable; Dish soap"
    orch, client, categorizer = _make_orchestrator(unapproved=[txn], enricher=enricher)
    categorizer.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    # The categorizer was given the items, and the transaction was approved.
    categorizer.suggest.assert_called_once()
    passed_txn = categorizer.suggest.call_args[0][0]
    assert passed_txn["amazon_items"] == "USB-C cable; Dish soap"
    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_amazon_enrichment_runs_only_on_matching_budget():
    """Amazon orders belong to one person — enrich only when the selected budget matches."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -11250, "date": "2014-03-01"}
    enricher = MagicMock()
    enricher.enrich.return_value = "USB-C cable"
    orch, client, categorizer = _make_orchestrator(
        unapproved=[txn],
        budgets=[{"id": "b1", "name": "Primary Budget"}],
        enricher=enricher,
        amazon_budget_name="Primary Budget",
    )
    categorizer.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    enricher.enrich.assert_called_once()


def test_amazon_enrichment_skipped_on_non_matching_budget():
    """On a different (e.g. shared) budget, the enricher must not be consulted."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -11250, "date": "2014-03-01"}
    enricher = MagicMock()
    enricher.enrich.return_value = "USB-C cable"
    orch, client, categorizer = _make_orchestrator(
        unapproved=[txn],
        budgets=[{"id": "b2", "name": "Shared Budget"}],
        enricher=enricher,
        amazon_budget_name="Primary Budget",
    )

    orch.run()

    enricher.enrich.assert_not_called()


def test_amazon_enrichment_error_is_surfaced_not_blind_guessed(capsys):
    """If the Amazon fetch fails (e.g. expired session), a bare Amazon charge has no
    item data, so surface it for manual review — don't crash and don't blind-guess."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -11250, "date": "2014-03-01"}
    enricher = MagicMock()
    enricher.enrich.side_effect = RuntimeError("session expired")
    orch, client, categorizer = _make_orchestrator(
        unapproved=[txn],
        budgets=[{"id": "b1", "name": "Primary Budget"}],
        enricher=enricher,
        amazon_budget_name="Primary Budget",
    )
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()  # must not raise

    categorizer.guess.assert_not_called()  # no item data -> surfaced, not guessed
    client.update_transaction.assert_not_called()
    out = capsys.readouterr().out
    assert "need your input" in out.lower()
    # The session genuinely dropped this run, so pointing at amazon-login is right.
    assert "amazon-login" in out


def test_unmatched_amazon_charge_is_deferred_then_guessed():
    """Enricher finds no order — fall back to the second-pass guess, not manual."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -11250, "date": "2014-03-01"}
    enricher = MagicMock()
    enricher.enrich.return_value = None
    orch, client, categorizer = _make_orchestrator(unapproved=[txn], enricher=enricher)
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    enricher.enrich.assert_called_once()
    categorizer.suggest.assert_not_called()
    categorizer.guess.assert_called_once()
    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_unmatched_amazon_passes_order_candidates_to_second_pass():
    """No single order matched, but the second pass should still get the nearby
    orders to reason over — not be left blind."""
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -2250, "date": "2015-05-14"}
    enricher = MagicMock()
    enricher.enrich.return_value = None  # no confident single match
    enricher.candidates.return_value = ["2015-05-13: $8.40 — Socks"]
    orch, client, categorizer = _make_orchestrator(unapproved=[txn], enricher=enricher)
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    passed_txn = categorizer.guess.call_args[0][0]
    assert passed_txn.get("amazon_candidates") == ["2015-05-13: $8.40 — Socks"]


def test_bare_uber_is_deferred_then_guessed_by_amount():
    """Bare 'Uber' is ambiguous on pass 1; the Opus pass guesses from the amount."""
    txn = {"id": "t1", "payee_name": "Uber", "amount": -850, "date": "2014-03-11"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    # A small Uber charge — second pass guesses dining (Uber Eats).
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    categorizer.suggest.assert_not_called()
    passed_txn = categorizer.guess.call_args[0][0]
    assert passed_txn["amount"] == -850  # the guesser gets the amount as a signal
    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_uber_eats_is_categorized_on_first_pass(capsys):
    """Uber Eats is clear — Haiku handles it directly, no second pass needed."""
    txn = {"id": "t1", "payee_name": "Uber Eats", "amount": -4500, "date": "2014-03-10"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    categorizer.suggest.assert_called_once()
    categorizer.guess.assert_not_called()


def test_amazon_variant_names_are_deferred(capsys):
    """Amazon Channels, Amazon Prime, etc. bypass the first pass too."""
    txn = {"id": "t1", "payee_name": "Amazon Prime Video", "amount": -1750, "date": "2014-03-08"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])

    orch.run()

    categorizer.suggest.assert_not_called()
    categorizer.guess.assert_called_once()


def test_unclear_first_pass_is_resolved_by_second_pass_guess():
    """An unclear Haiku result is deferred and rescued by the Opus guess."""
    txn = {"id": "t1", "payee_name": "Example Cafe", "amount": -4500, "date": "2014-03-10"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.suggest.return_value = {"action": "unclear", "raw": "dunno"}
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    categorizer.guess.assert_called_once()
    client.update_transaction.assert_called_once_with(
        "b1", "t1", category_id="c1", approved=True
    )


def test_opus_decline_is_surfaced_with_its_reason(capsys):
    """When Opus genuinely can't infer, surface it to the user with the reason."""
    txn = {"id": "t1", "payee_name": "Example Inn", "amount": -3750, "date": "2015-06-06"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.suggest.return_value = {"action": "unclear", "raw": "dunno"}
    categorizer.guess.return_value = {
        "action": "uncertain",
        "reason": "no nearby trip charges to tell lodging from dining",
    }

    orch.run()

    client.update_transaction.assert_not_called()
    output = capsys.readouterr().out
    assert "Example Inn" in output
    assert "no nearby trip charges" in output  # Opus's reason is shown


def test_unresolvable_category_after_second_pass_is_surfaced(capsys):
    """A guess naming a non-existent category is surfaced, not silently dropped."""
    txn = {"id": "t1", "payee_name": "Example Cafe", "amount": -4500, "date": "2014-03-10"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.suggest.return_value = {"action": "categorize", "category_name": "Nonexistent"}
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Still Nonexistent"}

    orch.run()

    client.update_transaction.assert_not_called()
    assert "need your input" in capsys.readouterr().out.lower()


def test_second_pass_sees_sibling_charges_as_context():
    """The Opus guess must receive other charges in the batch (incidental context)."""
    lodging = {"id": "t1", "payee_name": "Example Inn", "amount": -40000, "date": "2015-06-04"}
    incidental = {"id": "t2", "payee_name": "Example Inn", "amount": -3750, "date": "2015-06-06"}
    orch, client, categorizer = _make_orchestrator(unapproved=[lodging, incidental])

    # Lodging is categorized confidently on pass 1; the incidental is deferred.
    categorizer.suggest.side_effect = [
        {"action": "categorize", "category_name": "Dining Out"},
        {"action": "unclear", "raw": "dunno"},
    ]
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    # The guess for the incidental saw the sibling lodging charge in its context.
    context_txns = categorizer.guess.call_args[0][2]
    assert any(t.get("id") == "t1" for t in context_txns)


def test_second_pass_failure_is_surfaced_not_fatal(capsys):
    """If a guess errors (e.g. the web research times out), surface that one
    transaction and keep going — don't crash the whole run at the end."""
    txn = {"id": "t1", "payee_name": "Mystery", "amount": -1000, "date": "2014-03-12"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.suggest.return_value = {"action": "unclear", "raw": "dunno"}
    categorizer.guess.side_effect = RuntimeError("claude CLI timed out")

    orch.run()  # must not raise

    output = capsys.readouterr().out
    assert "Mystery" in output
    assert "need your input" in output.lower()


def test_inbound_etransfer_is_surfaced_not_auto_assigned(capsys):
    """A bare inbound e-transfer (no memo) must not be auto-assigned to Ready to
    Assign — it could be a reimbursement. Surface it for the user."""
    # Negative raw amount displays as an inflow ($-165.00) — money coming in.
    txn = {"id": "t1", "payee_name": "E-TRANSFER ***TEST1", "amount": 19000, "date": "2015-06-22"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.guess.return_value = {
        "action": "categorize", "category_name": "Inflow: Ready to Assign",
    }

    orch.run()

    categorizer.suggest.assert_not_called()  # deferred before the Haiku pass
    categorizer.guess.assert_not_called()  # and never guessed
    client.update_transaction.assert_not_called()
    assert "need your input" in capsys.readouterr().out.lower()


def test_outbound_etransfer_is_surfaced_not_guessed(capsys):
    """An outbound 'SEND E-TFR' with no memo is equally opaque — surface it."""
    txn = {"id": "t1", "payee_name": "SEND E-TFR ***TEST2", "amount": -23000, "date": "2015-06-29"}
    orch, client, categorizer = _make_orchestrator(unapproved=[txn])
    categorizer.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}

    orch.run()

    categorizer.guess.assert_not_called()
    client.update_transaction.assert_not_called()
    assert "need your input" in capsys.readouterr().out.lower()


def test_summary_shows_approved_guessed_and_needs_input_counts(capsys):
    txns = [
        {"id": "t1", "payee_name": "Example Cafe", "amount": -4500, "date": "2014-03-10"},
        {"id": "t2", "payee_name": "Uber", "amount": -900, "date": "2014-03-11"},
        {"id": "t3", "payee_name": "Mystery", "amount": -1000, "date": "2014-03-12"},
    ]
    orch, client, categorizer = _make_orchestrator(unapproved=txns)

    # t1: confident pass-1. t2: opaque -> deferred. t3: unclear -> deferred.
    categorizer.suggest.side_effect = [
        {"action": "categorize", "category_name": "Dining Out"},
        {"action": "unclear", "raw": "dunno"},
    ]
    # Second pass: t2 guessed successfully, t3 genuinely uncertain.
    categorizer.guess.side_effect = [
        {"action": "categorize", "category_name": "Dining Out"},
        {"action": "uncertain", "reason": "no idea what this is"},
    ]

    orch.run()

    output = capsys.readouterr().out
    assert "1 approved" in output
    assert "1 guessed" in output
    assert "1 need your input" in output


def test_dry_run_records_plan_without_writes(tmp_path):
    txn = {"id": "t1", "payee_name": "Store", "amount": -1000, "date": "2015-09-07",
           "category_id": "old", "approved": False}
    orch, client, cat = _make_orchestrator([txn])
    orch.dry_run = True
    orch.report_path = tmp_path / "preview.json"
    cat.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}
    orch.run()
    client.update_transaction.assert_not_called()
    op = json.loads(Path(orch.report_path).read_text())["operations"][0]
    assert op["status"] == "planned"
    assert op["before"] == {"category_id": "old", "approved": False}
    assert op["after"] == {"category_id": "c1", "approved": True}


@pytest.mark.parametrize("extra", [{"transfer_account_id": "a2"}, {"subtransactions": [{"id": "s1"}]}, {"deleted": True}])
def test_structured_transactions_are_not_recategorized(extra):
    txn = {"id": "t1", "payee_name": "Store", "amount": -1000, "date": "2015-09-07", **extra}
    orch, client, cat = _make_orchestrator([txn])
    orch.run()
    client.update_transaction.assert_not_called()
    cat.suggest.assert_not_called()
    cat.guess.assert_not_called()


def test_second_pass_write_failure_continues_and_is_audited(tmp_path):
    txns = [{"id": f"t{i}", "payee_name": "Store", "amount": -1000, "date": "2015-09-07"} for i in range(2)]
    orch, client, cat = _make_orchestrator(txns)
    cat.suggest.return_value = {"action": "unclear"}
    cat.guess.return_value = {"action": "categorize", "category_name": "Dining Out"}
    update = client.update_transaction.side_effect
    def fail_first(budget, txn_id, **fields):
        if txn_id == "t0":
            raise RuntimeError("secret must not be recorded")
        return update(budget, txn_id, **fields)
    client.update_transaction.side_effect = fail_first
    orch.report_path = tmp_path / "run.json"
    orch.run()
    report = json.loads((tmp_path / "run.json").read_text())
    assert client.update_transaction.call_count == 2
    assert [op["status"] for op in report["operations"]] == ["uncertain", "completed"]
    assert "secret" not in json.dumps(report)
    assert any(r["status"] == "failed" for r in report["results"])


def test_merchant_rules_precede_models_and_context_is_deduplicated():
    txn = {"id": "t1", "payee_name": "Example Merchant", "amount": -1000, "date": "2015-09-07"}
    orch, client, cat = _make_orchestrator([txn])
    orch.merchant_rules = {"example merchant": "c1"}
    orch.run()
    cat.suggest.assert_not_called()
    client.update_transaction.assert_called_once()
    orch, client, cat = _make_orchestrator([txn, dict(txn, id="t2")], past=[txn, dict(txn, id="t2")])
    cat.suggest.return_value = {"action": "unclear"}
    orch.run()
    assert [t["id"] for t in cat.guess.call_args_list[0].args[2]] == ["t2"]


def test_budget_selector_validates_before_processing(monkeypatch):
    from ynab_categorizer.orchestrator import select_budget
    client = MagicMock()
    client.get_budgets.return_value = [{"id": "a", "name": "First"}, {"id": "b", "name": "Second"}]
    assert select_budget(client, "second")["id"] == "b"
    assert select_budget(client, "a")["id"] == "a"
    with pytest.raises(SystemExit):
        select_budget(client, "missing")
    monkeypatch.setattr("builtins.input", lambda _: "0")
    with pytest.raises(SystemExit):
        select_budget(client)


def test_stale_transaction_is_not_overwritten():
    txn = {"id": "t1", "payee_name": "Store", "amount": -1000, "date": "2015-09-07"}
    orch, client, cat = _make_orchestrator([txn])
    cat.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}
    client.get_transaction.side_effect = lambda *_: {**txn, "category_id": "user-edit"}
    report = orch.run()
    client.update_transaction.assert_not_called()
    assert report["status"] == "incomplete"
    assert report["operations"][0]["status"] == "stale"
    assert len(report["failures"]) == 1


def test_readback_mismatch_is_incomplete_but_retains_completed_operation():
    txn = {"id": "t1", "payee_name": "Store", "amount": -1000, "date": "2015-09-07"}
    orch, client, cat = _make_orchestrator([txn])
    cat.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}
    client.get_transaction.side_effect = [dict(txn), dict(txn)]
    report = orch.run()
    assert report["status"] == "incomplete"
    assert report["operations"][0]["status"] == "completed"
    assert report["operations"][0]["verification"] == "failed"
    assert report["failures"][0]["status"] == "verification_failed"


def test_successful_run_returns_verified_completed_report():
    txn = {"id": "t1", "payee_name": "Store", "amount": -1000, "date": "2015-09-07"}
    orch, client, cat = _make_orchestrator([txn])
    cat.suggest.return_value = {"action": "categorize", "category_name": "Dining Out"}
    report = orch.run()
    assert report["status"] == "completed"
    assert report["failures"] == []
    assert report["operations"][0]["verification"] == "passed"


def test_amazon_requires_explicit_budget_binding(capsys):
    txn = {"id": "t1", "payee_name": "Amazon", "amount": -1000, "date": "2015-09-07"}
    enricher = MagicMock()
    orch, client, cat = _make_orchestrator([txn], enricher=enricher, amazon_budget_name=None)
    orch.run()
    enricher.enrich.assert_not_called()
    enricher.candidates.assert_not_called()
    assert "Amazon enrichment disabled" in capsys.readouterr().out


@pytest.mark.parametrize('second_pass', [False, True])
def test_backend_failure_never_writes_and_preserves_safe_diagnostic(second_pass):
    txn = {"id": "t1", "payee_name": "Example Shop", "amount": -1000, "date": "2014-03-10"}
    orch, client, cat = _make_orchestrator([txn])
    error = BackendError("codex executable not found; install it or select another backend")
    if second_pass:
        cat.suggest.return_value = {"action": "ask", "question": "What was purchased?"}
        cat.guess.side_effect = error
    else:
        cat.suggest.side_effect = error
    report = orch.run()
    client.update_transaction.assert_not_called()
    assert report['status'] == 'incomplete'
    assert 'codex executable not found' in json.dumps(report['results'])


@pytest.mark.parametrize('category', [None, 'user-choice'])
def test_approved_transaction_is_preserved_without_model_or_write(category):
    txn = {'id': 't1', 'payee_name': 'Example Shop', 'amount': -1000,
           'date': '2014-03-10', 'category_id': category, 'approved': True}
    orch, client, cat = _make_orchestrator([txn])
    orch.merchant_rules = {'example shop': 'c1'}
    report = orch.run()
    cat.suggest.assert_not_called()
    cat.guess.assert_not_called()
    client.update_transaction.assert_not_called()
    assert report['operations'] == []


@pytest.mark.parametrize('category', [None, 'imported-choice'])
def test_unapproved_transaction_is_reviewed_even_if_already_categorized(category):
    txn = {'id': 't1', 'payee_name': 'Example Shop', 'amount': -1000,
           'date': '2014-03-10', 'category_id': category, 'approved': False}
    orch, client, cat = _make_orchestrator([txn])
    cat.suggest.return_value = {'action': 'categorize', 'category_name': 'Dining Out'}
    report = orch.run()
    client.update_transaction.assert_called_once_with('b1', 't1', category_id='c1', approved=True)
    client.get_unapproved_transactions.assert_not_called()
    assert report['operations'][0]['before']['category_id'] == category
    assert report['operations'][0]['verification'] == 'passed'


def test_tracking_transfers_deleted_and_unknown_accounts_are_excluded():
    base = {'payee_name': 'Example Shop', 'amount': -1000, 'date': '2014-03-10'}
    txns = [dict(base, id='tracking', account_id='loan'),
            dict(base, id='unknown', account_id='unknown'),
            dict(base, id='transfer', transfer_account_id='other'),
            dict(base, id='deleted', deleted=True)]
    orch, client, cat = _make_orchestrator(txns)
    client.get_accounts.return_value.append({'id': 'loan', 'on_budget': False})
    report = orch.run()
    cat.suggest.assert_not_called()
    cat.guess.assert_not_called()
    client.update_transaction.assert_not_called()
    assert report['operations'] == []
