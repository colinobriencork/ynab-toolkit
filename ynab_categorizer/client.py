"""YNAB API client."""

import time

import httpx

BASE_URL = "https://api.ynab.com/v1"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0


class YNABClient:
    def __init__(self, token: str, timeout: float = DEFAULT_TIMEOUT):
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue a request, retrying transient network errors with backoff.

        Transient failures (read/connect timeouts, dropped connections) are
        retried so a single slow response can't abort a whole run. HTTP status
        errors are not retried — they're raised by the callers via
        ``raise_for_status``.
        """
        last_exc: httpx.TransportError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
        raise last_exc

    def get_budgets(self) -> list[dict]:
        resp = self._request("GET", "/budgets")
        resp.raise_for_status()
        return resp.json()["data"]["budgets"]

    def get_categories(self, budget_id: str) -> list[dict]:
        resp = self._request("GET", f"/budgets/{budget_id}/categories")
        resp.raise_for_status()
        groups = resp.json()["data"]["category_groups"]
        categories = []
        for group in groups:
            for cat in group["categories"]:
                if not cat["deleted"] and not cat["hidden"]:
                    cat["group_name"] = group["name"]
                    categories.append(cat)
        return categories

    def get_unapproved_transactions(self, budget_id: str) -> list[dict]:
        resp = self._request("GET",
            f"/budgets/{budget_id}/transactions", params={"type": "unapproved"}
        )
        resp.raise_for_status()
        return resp.json()["data"]["transactions"]

    def get_transactions(
        self, budget_id: str, since_date: str | None = None
    ) -> list[dict]:
        params = {}
        if since_date:
            params["since_date"] = since_date
        resp = self._request("GET",
            f"/budgets/{budget_id}/transactions", params=params
        )
        resp.raise_for_status()
        return resp.json()["data"]["transactions"]

    def update_transaction(
        self, budget_id: str, transaction_id: str, **fields
    ) -> dict:
        resp = self._request("PUT",
            f"/budgets/{budget_id}/transactions/{transaction_id}",
            json={"transaction": fields},
        )
        resp.raise_for_status()
        return resp.json()["data"]["transaction"]

    def get_transaction(self, budget_id: str, transaction_id: str) -> dict:
        resp = self._request("GET", f"/budgets/{budget_id}/transactions/{transaction_id}")
        resp.raise_for_status()
        return resp.json()["data"]["transaction"]

    def get_accounts(self, budget_id: str) -> list[dict]:
        resp = self._request("GET", f"/budgets/{budget_id}/accounts")
        resp.raise_for_status()
        return resp.json()["data"]["accounts"]

    def get_month(self, budget_id: str, month: str = "current") -> dict:
        """A month's detail, including to_be_budgeted (Ready to Assign)."""
        resp = self._request("GET", f"/budgets/{budget_id}/months/{month}")
        resp.raise_for_status()
        return resp.json()["data"]["month"]

    def update_month_category(
        self, budget_id: str, month: str, category_id: str, budgeted: int
    ) -> dict:
        """Set a category's budgeted (assigned) amount, in milliunits, for a month."""
        resp = self._request("PATCH",
            f"/budgets/{budget_id}/months/{month}/categories/{category_id}",
            json={"category": {"budgeted": budgeted}},
        )
        resp.raise_for_status()
        return resp.json()["data"]["category"]
