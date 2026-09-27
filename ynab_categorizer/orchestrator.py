"""Orchestrator: coordinates the categorization workflow.

This is the core loop that ties together the YNAB client, the LLM categorizer,
and user interaction. Each step is a method that can be overridden or extended
without rewriting the whole flow.
"""

import re

import httpx

from .client import YNABClient
from .categorizer import Categorizer, resolve_category


# Merchants where the payee name alone doesn't tell you what was bought.
# "contains" matches substring (e.g. "amazon" matches "Amazon Prime", "Amazon Channels")
# "exact" matches the full payee name (e.g. "uber" matches only "Uber", not "Uber Eats")
MANUAL_REVIEW_MERCHANTS = [
    ("amazon", "contains"),
    ("uber", "exact"),
]

# A bare "Amazon" / "Amazon.ca" payee names nothing bought — as opposed to
# "Amazon Prime Video", which does. Only the bare form is truly opaque.
_BARE_AMAZON_RE = re.compile(r"^amazon(?:\.(?:ca|com))?$", re.IGNORECASE)
# Interac e-transfers: "E-TRANSFER ***TEST1", "SEND E-TFR ***TEST2", etc. A bare
# person-to-person transfer with no memo carries no signal about its purpose.
_ETRANSFER_RE = re.compile(r"\be[-\s]?(?:transfer|tfr)\b", re.IGNORECASE)


def _needs_manual_review(payee_name: str) -> bool:
    payee_lower = payee_name.lower()
    for merchant, match_type in MANUAL_REVIEW_MERCHANTS:
        if match_type == "exact" and payee_lower == merchant:
            return True
        if match_type == "contains" and merchant in payee_lower:
            return True
    return False


def _is_bare_amazon(payee_name: str) -> bool:
    return bool(_BARE_AMAZON_RE.match(payee_name.strip()))


def _is_etransfer(payee_name: str) -> bool:
    return bool(_ETRANSFER_RE.search(payee_name))


def select_budget(client, selector: str | None = None) -> dict:
    """Prompt for (or auto-pick) a budget and return its dict."""
    budgets = client.get_budgets()
    if not budgets:
        raise SystemExit("No budgets found.")

    if selector is not None:
        matches = [b for b in budgets if b["id"] == selector]
        if not matches:
            matches = [b for b in budgets if b["name"].casefold() == selector.casefold()]
        if len(matches) != 1:
            raise SystemExit(f"Budget selector must match exactly one budget: {selector}")
        budget = matches[0]
    elif len(budgets) == 1:
        budget = budgets[0]
    else:
        print("Select a budget:")
        for i, b in enumerate(budgets):
            print(f"  {i + 1}. {b['name']}")
        try:
            choice = int(input("> ")) - 1
        except (ValueError, EOFError):
            raise SystemExit("Enter a budget number from the list.") from None
        if not 0 <= choice < len(budgets):
            raise SystemExit("Budget number is outside the list.")
        budget = budgets[choice]

    print(f"Using budget: {budget['name']}\n")
    return budget


class Orchestrator:
    def __init__(
        self,
        client: YNABClient,
        categorizer: Categorizer,
        enricher=None,
        amazon_budget_name=None,
        *, dry_run=False, budget_selector=None, merchant_rules=None, report_path=None,
    ):
        self.dry_run = dry_run
        self.budget_selector = budget_selector
        self.merchant_rules = {k.strip().casefold(): v for k, v in (merchant_rules or {}).items()}
        self.report_path = report_path
        self._report = {"kind": "categorization", "dry_run": dry_run, "operations": [], "results": []}
        self.client = client
        self.categorizer = categorizer
        self.enricher = enricher
        self.amazon_budget_name = amazon_budget_name
        self._amazon_enabled = False
        self._amazon_failed = False
        self._results = []
        self._deferred = []
        self._unapproved = []

    def run(self):
        self._results = []
        self._deferred = []
        self._report = {"kind": "categorization", "dry_run": self.dry_run, "operations": [], "results": []}
        self._amazon_failed = False
        try:
            budget = self.select_budget()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise SystemExit(
                    "YNAB API returned 401 Unauthorized. Your token is likely invalid.\n"
                    "Make sure you used a Personal Access Token (not an OAuth client ID/secret).\n"
                    "Run: pdm run setup"
                )
            raise
        budget_id = budget["id"]
        self._report["budget_id"] = budget_id
        self._save_report()

        # Amazon orders belong to one account, so only enrich on the matching budget.
        self._amazon_enabled = bool(self.enricher) and (
            self.amazon_budget_name is not None and budget["name"] == self.amazon_budget_name
        )

        if self.enricher and not self.amazon_budget_name:
            print("Amazon enrichment disabled: configure amazon_budget_name to bind orders to a budget.")

        categories = self.client.get_categories(budget_id)
        past_transactions = self.client.get_transactions(budget_id)
        unapproved = self.client.get_unapproved_transactions(budget_id)
        self._unapproved = unapproved

        if not unapproved:
            self.on_no_transactions()
            return self._finish_report()

        self.on_start(unapproved)

        for txn in unapproved:
            try:
                self.process_transaction(txn, budget_id, categories, past_transactions)
            except Exception as exc:
                # One transaction failing (e.g. a network error that survived
                # retries) shouldn't discard the work already applied — log it
                # and move on.
                payee = txn.get("payee_name") or "Unknown"
                print(f"\n  ! skipped {payee}: {type(exc).__name__}")
                self._record(txn, "failed", type(exc).__name__)

        self.second_pass(self._deferred, budget_id, categories, past_transactions)
        report = self._finish_report()
        self.on_complete()
        return report

    def select_budget(self) -> dict:
        return select_budget(self.client, self.budget_selector)

    def process_transaction(
        self,
        txn: dict,
        budget_id: str,
        categories: list[dict],
        past_transactions: list[dict],
    ):
        amount = txn["amount"] / -1000
        payee = txn.get("payee_name") or "Unknown"
        print(f"  {payee} | ${amount:.2f} | {txn['date']}", end="")

        if txn.get("deleted") or txn.get("transfer_account_id") or txn.get("subtransactions"):
            reason = "deleted, transfer, or split transaction requires manual review"
            print(" -> NEEDS YOUR INPUT")
            self._record(txn, "needs_input", reason)
            return

        rule = self.merchant_rules.get(payee.strip().casefold())
        if rule is not None:
            if not self.apply_categorization(txn, budget_id, categories, rule, result_label="rule"):
                self._record(txn, "needs_input", "merchant rule category missing or ambiguous")
            return

        if _is_etransfer(payee):
            # No merchant, no memo — the first pass can't help. Defer so the second
            # pass surfaces it for the user rather than auto-approving a guess.
            self._defer(txn)
            return

        if _needs_manual_review(payee):
            items = self._enrich(payee, txn)
            if items:
                # Order details recovered: attach them and let the LLM categorize.
                txn = {**txn, "amazon_items": items}
                print(f" (items: {items})")
            else:
                # No confident order match — still hand the second pass the nearby
                # orders so it can reason over them, rather than guess blind.
                self._defer(self._with_amazon_candidates(payee, txn))
                return

        result = self.categorizer.suggest(txn, categories, past_transactions)

        if result["action"] == "categorize" and self.apply_categorization(
            txn, budget_id, categories, result["category_name"]
        ):
            return
        # Unsure (a question), unclear, or a name that didn't resolve — never stop to
        # ask; hand it to the smarter second pass to reason out automatically.
        self._defer(txn)

    def _defer(self, txn: dict):
        print(" -> deferred for second-pass guess")
        self._deferred.append(txn)

    def second_pass(
        self,
        deferred: list[dict],
        budget_id: str,
        categories: list[dict],
        past_transactions: list[dict],
    ):
        """Have the smarter model make an educated guess on the leftovers."""
        if not deferred:
            return

        print(f"\n--- Second pass: {len(deferred)} uncertain, asking a smarter model ---")
        for txn in deferred:
            payee = txn.get("payee_name") or "Unknown"
            amount = txn["amount"] / -1000
            print(f"  {payee} | ${amount:.2f} | {txn['date']}", end="")

            # Some charges carry no signal to guess from — surface them directly
            # rather than letting the model invent a plausible-but-wrong category.
            no_guess = self._unguessable(txn)
            if no_guess:
                print(" -> NEEDS YOUR INPUT")
                self._record(txn, "needs_input", no_guess)
                continue

            # Give the guesser the surrounding charges from this batch too, so it can
            # read a charge in the context of the ones it sits beside.
            context = list({t["id"]: t for t in past_transactions + self._unapproved
                            if t.get("id") and t["id"] != txn.get("id") and not t.get("deleted")}.values())
            try:
                result = self.categorizer.guess(txn, categories, context)
                if result["action"] == "categorize" and self.apply_categorization(
                    txn, budget_id, categories, result["category_name"], result_label="guessed"
                ):
                    continue
            except Exception as e:
                # One slow/failed research call (e.g. a timeout) must not sink the
                # whole run — surface that transaction and move on.
                print(" -> NEEDS YOUR INPUT")
                self._record(txn, "failed", f"second-pass failed: {type(e).__name__}")
                continue

            # Opus genuinely couldn't infer it — surface it to the user rather than
            # blind-guessing or silently dropping it.
            reason = self._uncertain_reason(result)
            print(" -> NEEDS YOUR INPUT")
            self._record(txn, "needs_input", reason)

    def _unguessable(self, txn: dict) -> str | None:
        """A reason to surface a charge instead of guessing, or None if it's fair game.

        Some charges have no signal a model could reason from — guessing them only
        invents plausible-but-wrong categories (and, worse, seeds the history that
        justifies the next wrong guess). Surface those for the user instead.
        """
        payee = txn.get("payee_name") or ""
        if _is_etransfer(payee):
            return (
                "bare Interac e-transfer with no memo — categorize by who it was "
                "sent to/from"
            )
        if (
            _is_bare_amazon(payee)
            and not txn.get("amazon_items")
            and not txn.get("amazon_candidates")
        ):
            if self._amazon_failed:
                # The session dropped mid-run, so nothing could be enriched.
                return (
                    "Amazon session dropped this run (run: pdm run amazon-login), "
                    "so no order details; categorize manually"
                )
            # Session was fine — the order just wasn't found (likely older than the
            # order-history scrape window).
            return (
                "couldn't find a matching Amazon order (it may predate the "
                "order-history scrape window); categorize manually"
            )
        return None

    @staticmethod
    def _uncertain_reason(result: dict) -> str:
        if result.get("action") == "uncertain":
            return result.get("reason") or "could not determine a category"
        if result.get("action") == "categorize":
            return (
                f"suggested '{result['category_name']}', "
                "which isn't one of your categories"
            )
        return "could not determine a category"

    def _enrich(self, payee: str, txn: dict) -> str | None:
        """Recover item details for an opaque merchant, or None to defer it."""
        if self._amazon_enabled and "amazon" in payee.lower():
            try:
                return self.enricher.enrich(txn)
            except Exception as e:
                # e.g. expired Amazon session — disable for the rest of the run and
                # let the affected charges fall through to the second pass.
                print(f"\n  (Amazon enrichment unavailable: {e})")
                self._amazon_enabled = False
                self._amazon_failed = True
        return None

    def _with_amazon_candidates(self, payee: str, txn: dict) -> dict:
        """Attach nearby Amazon orders to a charge that couldn't be matched exactly."""
        if not (self._amazon_enabled and self.enricher and "amazon" in payee.lower()):
            return txn
        try:
            cands = self.enricher.candidates(txn)
        except Exception:
            # Best-effort context only — a failure here just means a blind guess.
            return txn
        return {**txn, "amazon_candidates": cands} if cands else txn

    def apply_categorization(
        self,
        txn: dict,
        budget_id: str,
        categories: list[dict],
        category_name: str,
        result_label: str = "approved",
    ) -> bool:
        """Apply a category and approve. Returns False if the name didn't resolve."""
        payee = txn.get("payee_name") or "Unknown"
        cat = resolve_category(category_name, categories)

        if not cat:
            return False

        operation = {
            "kind": "transaction", "budget_id": budget_id, "transaction_id": txn["id"],
            "before": {"category_id": txn.get("category_id"), "approved": txn.get("approved", False)},
            "after": {"category_id": cat["id"], "approved": True},
            "status": "planned", "evidence": {"source": result_label, "selected_category": category_name},
            "date": txn.get("date"), "payee": payee, "amount": txn.get("amount"),
        }
        self._report["operations"].append(operation)
        self._save_report()
        if not self.dry_run:
            live = self.client.get_transaction(budget_id, txn["id"])
            fields = ("category_id", "approved", "amount", "date", "account_id", "payee_id", "memo", "transfer_account_id", "subtransactions", "deleted")
            if any(live.get(key, False if key == "approved" else None) != txn.get(key, False if key == "approved" else None) for key in fields):
                operation["status"] = "stale"
                self._record(txn, "failed", "transaction changed since the run started")
                return True
            operation["status"] = "writing"
            self._save_report()
            try:
                self.client.update_transaction(
                    budget_id, txn["id"], category_id=cat["id"], approved=True
                )
            except Exception:
                operation["status"] = "uncertain"
                self._save_report()
                raise
            operation["status"] = "completed"
            self._save_report()
            try:
                actual = self.client.get_transaction(budget_id, txn["id"])
                verified = actual.get("category_id") == cat["id"] and actual.get("approved") is True
            except Exception:
                verified = False
            operation["verification"] = "passed" if verified else "failed"
            if not verified:
                self._record(txn, "verification_failed", "could not verify applied transaction category and approval")
                return True
        print(f" -> {cat['name']}" + (" (preview)" if self.dry_run else ""))
        self._record(txn, "planned" if self.dry_run else result_label, cat["name"])
        return True

    def _finish_report(self):
        failures = [r for r in self._report["results"] if r["status"] in {"failed", "verification_failed"}]
        needs_input = sum(r["status"] == "needs_input" for r in self._report["results"])
        self._report["failures"] = failures
        self._report["needs_input"] = needs_input
        self._report["status"] = "incomplete" if failures or needs_input else ("preview" if self.dry_run else "completed")
        self._save_report()
        return self._report

    def _save_report(self):
        if self.dry_run and self.report_path is None:
            return
        from .audit import write_report
        self.report_path = write_report(self._report, self.report_path)

    def _record(self, txn, status, reason):
        self._results.append((status, txn.get("payee_name") or "Unknown", reason))
        self._report["results"].append({
            "transaction_id": txn.get("id"), "date": txn.get("date"),
            "status": status, "reason": reason,
        })
        self._save_report()

    def on_no_transactions(self):
        print("No unapproved transactions. You're all caught up!")

    def on_start(self, transactions: list[dict]):
        print(f"Found {len(transactions)} unapproved transaction(s).\n")

    def on_complete(self):
        approved = [r for r in self._results if r[0] in {"approved", "rule"}]
        guessed = [r for r in self._results if r[0] == "guessed"]
        needs_input = [r for r in self._results if r[0] == "needs_input"]
        failed = sum(r[0] in {"failed", "verification_failed"} for r in self._results)
        planned = sum(r[0] == "planned" for r in self._results)
        print(
            f"\n--- Summary: {len(approved)} approved, "
            f"{len(guessed)} guessed, {len(needs_input)} need your input, "
            f"{failed} failed, {planned} planned ---"
        )
        if self.report_path:
            print(f"\n  Run report: {self.report_path}")
        if guessed:
            print("\n  Guessed by the second pass (approved — worth a glance):")
            for _, payee, cat in guessed:
                print(f"    - {payee} -> {cat}")
        if needs_input:
            print("\n  Needs your input (couldn't infer — set these in YNAB):")
            for _, payee, reason in needs_input:
                print(f"    - {payee} ({reason})")
