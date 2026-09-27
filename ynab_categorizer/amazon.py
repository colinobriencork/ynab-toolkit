"""Amazon order enrichment.

YNAB sees Amazon charges as opaque payees ("AMZN Mktp US") with no hint of what
was bought. This module matches a YNAB transaction to an Amazon order by date and
amount, then summarises the purchased items so the categorizer has something to
work with — instead of deferring the transaction.

Amazon actively blocks plain-HTTP scrapers with a JavaScript/WAF challenge, so
order data is fetched by driving a real headless Chromium via Playwright against a
persistent, already-logged-in browser profile. The pure pieces (parsing, matching,
pagination) are separated out so they're testable without a browser. Fetching is
injectable so tests never launch one.
"""

import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup

MAX_SUMMARY_LEN = 200
NEARBY_ORDER_DAYS = 7       # window for candidate orders when no exact match
LOOKBACK_DAYS = 90          # how far back to match charges against orders
PAGE_SIZE = 10              # Amazon shows 10 orders per page
MAX_PAGES = 12              # safety cap per year (~120 orders)
PROFILE_DIR = Path.home() / ".config" / "ynab-categorizer" / "amazon-profile"

_MONTH_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+\d{1,2},\s+\d{4}"
)
_MONEY = re.compile(r"\$[\d,]+\.\d{2}")
_PRODUCT_HREF = ("/dp/", "/gp/product/", "/product/")


class AmazonLoginRequired(RuntimeError):
    """Raised when the persistent profile isn't logged in (run the login command)."""


# --- pure logic (unit-tested, no browser) --------------------------------


def match_order(txn, orders, tolerance_days=3, amount_tol=0.01):
    """Return the single Amazon order matching this YNAB transaction, or None.

    A match requires the order's grand total to equal the charge (within
    ``amount_tol`` dollars) and to have been placed within ``tolerance_days`` of
    the transaction date. If zero or more than one order matches, returns None so
    the caller can fall back to the second pass rather than guess.
    """
    amount = abs(txn["amount"]) / 1000  # YNAB milliunits -> dollars
    txn_date = date.fromisoformat(txn["date"])

    candidates = [
        o
        for o in orders
        if abs(o.grand_total - amount) <= amount_tol
        and abs((o.order_placed_date - txn_date).days) <= tolerance_days
    ]

    return candidates[0] if len(candidates) == 1 else None


def summarize_items(order) -> str:
    """Join an order's item titles into a short, single-line description."""
    titles = [item.title for item in order.items if item.title]
    summary = "; ".join(titles)
    if len(summary) > MAX_SUMMARY_LEN:
        summary = summary[: MAX_SUMMARY_LEN - 1].rstrip() + "…"
    return summary


def nearby_orders(txn, orders, window_days=NEARBY_ORDER_DAYS) -> list:
    """Orders placed within ``window_days`` of the charge, ignoring amount.

    When match_order can't find a single confident match, these are the plausible
    orders behind the charge — handed to the second pass so it can reason about a
    near-miss (a tax difference, a partial shipment, or two orders charged together)
    instead of guessing blind.
    """
    txn_date = date.fromisoformat(txn["date"])
    near = [
        o for o in orders
        if abs((o.order_placed_date - txn_date).days) <= window_days
    ]
    near.sort(key=lambda o: o.order_placed_date)
    return near


def summarize_candidate(order) -> str:
    """One-line 'date: $total — items' summary of a candidate order."""
    return (
        f"{order.order_placed_date.isoformat()}: "
        f"${order.grand_total:.2f} — {summarize_items(order)}"
    )


def parse_orders(html: str) -> list:
    """Parse an Amazon order-history page into order objects.

    Each returned object has ``order_placed_date`` (date), ``grand_total`` (float)
    and ``items`` (list of objects with a ``title``) — the shape match_order and
    summarize_items expect.
    """
    soup = BeautifulSoup(html, "html.parser")
    orders = []
    for card in soup.select(".order-card"):
        header = card.select_one(".order-header")
        htext = header.get_text(" ", strip=True) if header else ""
        md = _MONTH_DATE.search(htext)
        mt = _MONEY.search(htext)
        if not (md and mt):
            continue
        placed = datetime.strptime(md.group(0), "%B %d, %Y").date()
        total = float(mt.group(0)[1:].replace(",", ""))

        items = []
        for a in card.select("a.a-link-normal"):
            href = a.get("href", "") or ""
            title = a.get_text(" ", strip=True)
            if title and any(p in href for p in _PRODUCT_HREF):
                items.append(SimpleNamespace(title=title))

        orders.append(
            SimpleNamespace(order_placed_date=placed, grand_total=total, items=items)
        )
    return orders


def _orders_url(domain: str, year: int, start_index: int) -> str:
    return (
        f"https://www.{domain}/gp/css/order-history"
        f"?orderFilter=year-{year}&startIndex={start_index}"
    )


def _stop_paging(orders_on_page: list, cutoff: date) -> bool:
    """Stop when a page is empty or its oldest order predates the match window."""
    if not orders_on_page:
        return True
    return min(o.order_placed_date for o in orders_on_page) < cutoff


# --- browser I/O (not unit-tested) ---------------------------------------


def _domain() -> str:
    return os.environ.get("AMAZON_DOMAIN", "amazon.com")


def _on_orders(page) -> bool:
    try:
        url = page.url
        return bool(page.query_selector_all(".order-card")) and "/ap/" not in url
    except Exception:
        return False


def _fetch_orders_via_playwright():
    """Scrape recent order history headlessly using the logged-in profile."""
    from playwright.sync_api import sync_playwright

    domain = _domain()
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    years = sorted({today.year, cutoff.year}, reverse=True)

    orders = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for year in years:
                for i in range(MAX_PAGES):
                    page.goto(
                        _orders_url(domain, year, i * PAGE_SIZE),
                        wait_until="domcontentloaded",
                    )
                    page.wait_for_timeout(2000)
                    if "/ap/" in page.url or "/ax/" in page.url or "signin" in page.url:
                        raise AmazonLoginRequired(
                            "Amazon session expired. Run: pdm run amazon-login"
                        )
                    page_orders = parse_orders(page.content())
                    orders.extend(page_orders)
                    if _stop_paging(page_orders, cutoff):
                        break
        finally:
            ctx.close()
    return orders


def _try_click(page, *selectors) -> bool:
    """Click the first selector that's present (used to dismiss the passkey upsell)."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=1500)
                return True
        except Exception:
            continue
    return False


def interactive_login(timeout: int = 300) -> bool:
    """One-time headful login that persists the session for headless fetches.

    Auto-fills email + (password with the authenticator code appended, which Amazon
    accepts in one shot), auto-enters an authenticator field if shown, and auto-
    dismisses the "set up a passkey" upsell. A human only needs to step in for a
    genuine passkey/captcha challenge in the open window.
    """
    import pyotp
    from playwright.sync_api import sync_playwright

    user = os.environ["AMAZON_USERNAME"]
    pw = os.environ["AMAZON_PASSWORD"]
    otp_secret = os.environ.get("AMAZON_OTP_SECRET_KEY", "")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 1000}
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(_orders_url(_domain(), date.today().year, 0),
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            if not _on_orders(page):
                if page.locator("#ap_email").count():
                    page.fill("#ap_email", user)
                    _try_click(page, "#continue")
                    page.wait_for_timeout(1500)
                if page.locator("#ap_password").count():
                    code = pyotp.TOTP(otp_secret).now() if otp_secret else ""
                    page.fill("#ap_password", pw + code)  # password+OTP in one shot
                    _try_click(page, "#signInSubmit")
                    page.wait_for_timeout(3000)

            deadline = time.time() + timeout
            while not _on_orders(page) and time.time() < deadline:
                try:
                    if page.locator("#auth-mfa-otpcode").count() and otp_secret:
                        page.fill("#auth-mfa-otpcode", pyotp.TOTP(otp_secret).now())
                        _try_click(page, "#auth-signin-button")
                        page.wait_for_timeout(2500)
                    # Auto-dismiss the passkey upsell / continue prompts.
                    _try_click(
                        page,
                        "text=Not now", "text=Skip for now", "text=Maybe later",
                        "text=No thanks", "text=Cancel", "#signInSubmit",
                        "input[type=submit]",
                    )
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            ok = _on_orders(page)
            print("Login succeeded — session saved." if ok else
                  "Could not reach orders. Complete any challenge in the window, then retry.")
            return ok
        finally:
            ctx.close()


class AmazonEnricher:
    """Looks up the items behind an Amazon charge.

    The order history is fetched once (lazily) and reused for every transaction in
    a run. If the saved session has expired, the login browser is opened
    automatically (it signs in by itself; a human only intervenes on a genuine
    challenge) and the fetch retried — so an expired session heals mid-run instead
    of dumping every Amazon charge on the user. Pass ``fetch_orders`` / ``login``
    to inject in tests.
    """

    def __init__(self, fetch_orders=None, login=None):
        self._fetch_orders = fetch_orders or _fetch_orders_via_playwright
        self._login = login or interactive_login
        self._orders = None

    def enrich(self, txn) -> str | None:
        """Return a description of the items on the matched order, or None."""
        order = match_order(txn, self._all_orders())
        return summarize_items(order) if order else None

    def candidates(self, txn) -> list[str]:
        """Summaries of orders near the charge date, for when enrich() found no match."""
        return [summarize_candidate(o) for o in nearby_orders(txn, self._all_orders())]

    def _all_orders(self):
        if self._orders is None:
            try:
                self._orders = self._fetch_orders()
            except AmazonLoginRequired:
                print("\n  (Amazon session expired — opening a browser to sign back in)")
                if not self._login():
                    raise
                self._orders = self._fetch_orders()
        return self._orders
