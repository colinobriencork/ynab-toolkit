"""Tests for the YNAB API client.

These test the client's behaviour against mocked HTTP responses —
verifying it calls the right endpoints, passes the right params,
and transforms the responses correctly.
"""

import httpx
import respx
import pytest

from ynab_categorizer.client import YNABClient, BASE_URL


@pytest.fixture
def client():
    return YNABClient("fake-token")


@respx.mock
def test_get_budgets_returns_budget_list(client):
    respx.get(f"{BASE_URL}/budgets").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "budgets": [
                        {"id": "budget-1", "name": "My Budget"},
                        {"id": "budget-2", "name": "Other Budget"},
                    ]
                }
            },
        )
    )
    budgets = client.get_budgets()
    assert len(budgets) == 2
    assert budgets[0]["name"] == "My Budget"


@respx.mock
def test_get_categories_flattens_groups_and_excludes_hidden(client):
    respx.get(f"{BASE_URL}/budgets/b1/categories").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "category_groups": [
                        {
                            "name": "Bills",
                            "categories": [
                                {"id": "c1", "name": "Rent", "hidden": False, "deleted": False},
                                {"id": "c2", "name": "Old Bill", "hidden": True, "deleted": False},
                            ],
                        },
                        {
                            "name": "Fun",
                            "categories": [
                                {"id": "c3", "name": "Dining Out", "hidden": False, "deleted": False},
                                {"id": "c4", "name": "Removed", "hidden": False, "deleted": True},
                            ],
                        },
                    ]
                }
            },
        )
    )
    cats = client.get_categories("b1")
    names = [c["name"] for c in cats]
    assert names == ["Rent", "Dining Out"]
    assert cats[0]["group_name"] == "Bills"
    assert cats[1]["group_name"] == "Fun"


@respx.mock
def test_get_unapproved_transactions(client):
    route = respx.get(f"{BASE_URL}/budgets/b1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "t1", "payee_name": "Example Cafe", "amount": -5000, "approved": False},
                    ]
                }
            },
        )
    )
    txns = client.get_unapproved_transactions("b1")
    assert len(txns) == 1
    assert txns[0]["payee_name"] == "Example Cafe"
    assert route.calls[0].request.url.params["type"] == "unapproved"


@respx.mock
def test_get_transactions_with_since_date(client):
    route = respx.get(f"{BASE_URL}/budgets/b1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )
    client.get_transactions("b1", since_date="2014-01-01")
    assert route.calls[0].request.url.params["since_date"] == "2014-01-01"


@respx.mock
def test_update_transaction_sends_fields(client):
    route = respx.put(f"{BASE_URL}/budgets/b1/transactions/t1").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transaction": {"id": "t1", "category_id": "c1", "approved": True}}},
        )
    )
    result = client.update_transaction("b1", "t1", category_id="c1", approved=True)
    assert result["category_id"] == "c1"
    assert result["approved"] is True


@respx.mock
def test_get_month_returns_month_detail(client):
    respx.get(f"{BASE_URL}/budgets/b1/months/current").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "month": {
                        "month": "2015-06-01",
                        "to_be_budgeted": 42000,
                        "categories": [{"id": "c1", "name": "Rent"}],
                    }
                }
            },
        )
    )
    month = client.get_month("b1")
    assert month["to_be_budgeted"] == 42000
    assert month["month"] == "2015-06-01"


@respx.mock
def test_update_month_category_patches_budgeted(client):
    route = respx.patch(
        f"{BASE_URL}/budgets/b1/months/2015-06-01/categories/c1"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"category": {"id": "c1", "budgeted": 18000}}}
        )
    )
    result = client.update_month_category("b1", "2015-06-01", "c1", budgeted=18000)
    assert result["budgeted"] == 18000
    import json
    body = json.loads(route.calls[0].request.content)
    assert body == {"category": {"budgeted": 18000}}


@respx.mock
def test_get_accounts_returns_accounts(client):
    respx.get(f"{BASE_URL}/budgets/b1/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "accounts": [
                        {"id": "a1", "name": "Chequing", "type": "checking"},
                        {"id": "cc1", "name": "Card Alpha", "type": "creditCard"},
                    ]
                }
            },
        )
    )
    accounts = client.get_accounts("b1")
    assert {a["type"] for a in accounts} == {"checking", "creditCard"}


@respx.mock
def test_get_scheduled_transactions(client):
    respx.get(f"{BASE_URL}/budgets/b1/scheduled_transactions").mock(
        return_value=httpx.Response(200, json={'data': {'scheduled_transactions': [
            {'id': 's1', 'date_next': '2015-09-30', 'amount': -3_000}]}}))
    assert client.get_scheduled_transactions('b1')[0]['id'] == 's1'


@respx.mock
def test_client_raises_on_http_error(client):
    respx.get(f"{BASE_URL}/budgets").mock(
        return_value=httpx.Response(401, json={"error": {"detail": "Unauthorized"}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.get_budgets()


@respx.mock
def test_client_retries_transient_timeout(client):
    route = respx.put(f"{BASE_URL}/budgets/b1/transactions/t1").mock(
        side_effect=[
            httpx.ReadTimeout("timed out"),
            httpx.Response(
                200,
                json={"data": {"transaction": {"id": "t1", "approved": True}}},
            ),
        ]
    )
    result = client.update_transaction("b1", "t1", approved=True)
    assert result["approved"] is True
    assert route.call_count == 2


@respx.mock
def test_client_gives_up_after_exhausting_retries(client):
    respx.put(f"{BASE_URL}/budgets/b1/transactions/t1").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    with pytest.raises(httpx.ReadTimeout):
        client.update_transaction("b1", "t1", approved=True)


@respx.mock
def test_get_transaction_for_guarded_edit(client):
    respx.get(f"{BASE_URL}/budgets/b1/transactions/t1").mock(
        return_value=httpx.Response(200, json={'data': {'transaction': {'id': 't1', 'category_id': 'food'}}})
    )
    assert client.get_transaction('b1', 't1')['category_id'] == 'food'
