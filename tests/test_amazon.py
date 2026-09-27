"""Tests for the Amazon order enricher.

These test the behaviour of matching a YNAB transaction to an Amazon order
and summarising its items. Matching logic is pure and uses lightweight fake
order objects — no network, no amazon-orders dependency.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from ynab_categorizer.amazon import (
    AmazonEnricher,
    AmazonLoginRequired,
    _orders_url,
    _stop_paging,
    match_order,
    nearby_orders,
    parse_orders,
    summarize_candidate,
    summarize_items,
)


def _order(placed: str, total: float, titles):
    return SimpleNamespace(
        order_number=f"order-{placed}-{total}",
        order_placed_date=date.fromisoformat(placed),
        grand_total=total,
        items=[SimpleNamespace(title=t) for t in titles],
    )


def _txn(amount_milliunits: int, date_str: str):
    return {"id": "t1", "payee_name": "Amazon", "amount": amount_milliunits, "date": date_str}


# --- match_order ---------------------------------------------------------


def test_single_order_matching_date_and_amount_is_returned():
    orders = [_order("2014-03-10", 11.25, ["USB-C cable"])]
    txn = _txn(-11250, "2014-03-10")

    assert match_order(txn, orders) is orders[0]


def test_match_within_date_tolerance():
    """Charge often posts a couple of days after the order is placed."""
    orders = [_order("2014-03-08", 11.25, ["USB-C cable"])]
    txn = _txn(-11250, "2014-03-10")  # 2 days later

    assert match_order(txn, orders) is orders[0]


def test_no_match_when_date_outside_tolerance_returns_none():
    orders = [_order("2014-03-01", 11.25, ["USB-C cable"])]
    txn = _txn(-11250, "2014-03-10")  # 9 days later

    assert match_order(txn, orders) is None


def test_no_match_when_amount_differs_returns_none():
    orders = [_order("2014-03-10", 4.25, ["USB-C cable"])]
    txn = _txn(-11250, "2014-03-10")

    assert match_order(txn, orders) is None


def test_ambiguous_two_orders_same_date_and_amount_returns_none():
    orders = [
        _order("2014-03-10", 11.25, ["USB-C cable"]),
        _order("2014-03-10", 11.25, ["Dish soap"]),
    ]
    txn = _txn(-11250, "2014-03-10")

    assert match_order(txn, orders) is None


def test_empty_order_history_returns_none():
    assert match_order(_txn(-11250, "2014-03-10"), []) is None


# --- summarize_items -----------------------------------------------------


def test_summarize_joins_item_titles():
    order = _order("2014-03-10", 11.25, ["USB-C cable", "Dish soap", "Phone case"])

    summary = summarize_items(order)

    assert "USB-C cable" in summary
    assert "Dish soap" in summary
    assert "Phone case" in summary


# --- AmazonEnricher ------------------------------------------------------


def test_enrich_returns_item_summary_for_matched_order():
    orders = [_order("2014-03-10", 11.25, ["USB-C cable"])]
    enricher = AmazonEnricher(fetch_orders=lambda: orders)

    summary = enricher.enrich(_txn(-11250, "2014-03-10"))

    assert "USB-C cable" in summary


def test_enrich_returns_none_when_no_confident_match():
    orders = [_order("2014-03-10", 16.50, ["Something else"])]
    enricher = AmazonEnricher(fetch_orders=lambda: orders)

    assert enricher.enrich(_txn(-11250, "2014-03-10")) is None


# --- candidate orders for the second pass --------------------------------


def test_nearby_orders_returns_orders_in_date_window_ignoring_amount():
    """Even when the amount doesn't match, nearby orders are useful context."""
    orders = [
        _order("2015-05-13", 8.40, ["Socks"]),   # 1 day off — included
        _order("2015-05-25", 5.00, ["Pen"]),       # 11 days off — excluded
        _order("2015-04-01", 2.75, ["Old thing"]), # far away — excluded
    ]
    txn = _txn(-700, "2015-05-14")  # amount intentionally matches nothing

    near = nearby_orders(txn, orders)

    titles = [i.title for o in near for i in o.items]
    assert "Socks" in titles
    assert "Pen" not in titles
    assert "Old thing" not in titles


def test_summarize_candidate_includes_date_total_and_items():
    order = _order("2015-05-13", 8.40, ["Socks", "Belt"])
    line = summarize_candidate(order)
    assert "2015-05-13" in line
    assert "$8.40" in line
    assert "Socks" in line and "Belt" in line


def test_enricher_candidates_lists_nearby_orders_when_no_exact_match():
    orders = [
        _order("2015-05-13", 8.40, ["Socks"]),
        _order("2015-01-01", 2.75, ["Old thing"]),
    ]
    enricher = AmazonEnricher(fetch_orders=lambda: orders)

    cands = enricher.candidates(_txn(-700, "2015-05-14"))

    assert any("Socks" in c for c in cands)
    assert all("Old thing" not in c for c in cands)


# --- HTML parsing --------------------------------------------------------

# Minimal synthetic markup mirroring amazon.ca's order-history DOM (no real data).
_ORDERS_HTML = """
<div class="order-card">
  <div class="order-header">Order placed May 12, 2015 Total $6.75 Order # 111-222</div>
  <a class="a-link-normal" href="/dp/EXAMPLE001?ref=x">Example notebook</a>
  <a class="a-link-normal" href="/gp/product/EXAMPLE002?ref=y">Example pencil set</a>
  <a class="a-link-normal" href="/your-orders/details?orderID=111">View order details</a>
</div>
<div class="order-card">
  <div class="order-header">Subscription charged on April 30, 2015 Total $11.25 Order # 333-444</div>
  <a class="a-link-normal" href="/dp/EXAMPLE003?ref=z">Example subscription</a>
</div>
"""


def test_parse_orders_extracts_date_total_and_items():
    orders = parse_orders(_ORDERS_HTML)
    assert len(orders) == 2

    o = orders[0]
    assert o.order_placed_date == date(2015, 5, 12)
    assert o.grand_total == 6.75
    titles = [i.title for i in o.items]
    assert titles == ["Example notebook", "Example pencil set"]


def test_parse_orders_ignores_non_product_links():
    orders = parse_orders(_ORDERS_HTML)
    titles = [i.title for o in orders for i in o.items]
    assert "View order details" not in titles


def test_parse_orders_handles_subscription_total():
    orders = parse_orders(_ORDERS_HTML)
    sub = orders[1]
    assert sub.order_placed_date == date(2015, 4, 30)
    assert sub.grand_total == 11.25


def test_parse_orders_empty_html_returns_empty_list():
    assert parse_orders("<html><body>nothing here</body></html>") == []


# --- pagination ----------------------------------------------------------


def test_orders_url_builds_paginated_year_url():
    url = _orders_url("amazon.ca", 2015, 10)
    assert "amazon.ca" in url
    assert "orderFilter=year-2015" in url
    assert "startIndex=10" in url


def test_stop_paging_when_page_empty():
    assert _stop_paging([], cutoff=date(2015, 1, 1)) is True


def test_stop_paging_when_oldest_order_past_cutoff():
    older = [SimpleNamespace(order_placed_date=date(2014, 12, 1))]
    assert _stop_paging(older, cutoff=date(2015, 1, 1)) is True


def test_keep_paging_while_orders_within_window():
    recent = [
        SimpleNamespace(order_placed_date=date(2015, 5, 1)),
        SimpleNamespace(order_placed_date=date(2015, 4, 20)),
    ]
    assert _stop_paging(recent, cutoff=date(2015, 1, 1)) is False


# --- auto re-login when the session has expired ---------------------------


def test_enrich_logs_back_in_and_retries_when_session_expired():
    orders = [_order("2014-03-10", 11.25, ["USB-C cable"])]
    calls = {"fetch": 0, "login": 0}

    def fetch():
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise AmazonLoginRequired("session expired")
        return orders

    def login():
        calls["login"] += 1
        return True

    enricher = AmazonEnricher(fetch_orders=fetch, login=login)
    summary = enricher.enrich(_txn(-11250, "2014-03-10"))

    assert "USB-C cable" in summary
    assert calls == {"fetch": 2, "login": 1}


def test_enrich_raises_login_required_when_relogin_fails():
    def fetch():
        raise AmazonLoginRequired("session expired")

    enricher = AmazonEnricher(fetch_orders=fetch, login=lambda: False)

    with pytest.raises(AmazonLoginRequired):
        enricher.enrich(_txn(-11250, "2014-03-10"))


def test_no_login_attempted_while_session_is_fine():
    calls = {"login": 0}

    def login():
        calls["login"] += 1
        return True

    orders = [_order("2014-03-10", 11.25, ["USB-C cable"])]
    enricher = AmazonEnricher(fetch_orders=lambda: orders, login=login)
    enricher.enrich(_txn(-11250, "2014-03-10"))

    assert calls["login"] == 0


def test_enrich_fetches_order_history_only_once():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [_order("2014-03-10", 11.25, ["USB-C cable"])]

    enricher = AmazonEnricher(fetch_orders=fetch)
    enricher.enrich(_txn(-11250, "2014-03-10"))
    enricher.enrich(_txn(-11250, "2014-03-10"))

    assert calls["n"] == 1
