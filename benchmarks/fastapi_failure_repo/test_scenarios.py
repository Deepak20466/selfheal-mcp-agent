"""Twenty contract tests for the independent, intentionally faulty API."""

from __future__ import annotations

from app import app, reset_state
from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)


def setup_function() -> None:
    reset_state()


def test_01_required_request_field_is_validated() -> None:
    assert client.post("/scenario/01", json={}).status_code == 422


def test_02_page_one_starts_at_first_item() -> None:
    assert client.get("/scenario/02?page=1&limit=2").json()["items"] == [1, 2]


def test_03_missing_widget_returns_not_found() -> None:
    assert client.get("/scenario/03/99").status_code == 404


def test_04_empty_average_is_a_client_error() -> None:
    assert client.get("/scenario/04").status_code == 422


def test_05_event_timestamp_is_timezone_aware() -> None:
    assert client.get("/scenario/05").json()["at"].endswith("+00:00")


def test_06_money_uses_decimal_precision() -> None:
    assert client.get("/scenario/06").json()["total"] == "0.30"


def test_07_omitted_patch_field_preserves_existing_value() -> None:
    assert client.patch("/scenario/07", json={}).json()["name"] == "Ada"


def test_08_create_returns_201() -> None:
    assert client.post("/scenario/08").status_code == 201


def test_09_duplicate_order_is_conflict() -> None:
    client.post("/scenario/09/order-1")
    assert client.post("/scenario/09/order-1").status_code == 409


def test_10_private_route_requires_api_key() -> None:
    assert client.get("/scenario/10").status_code == 401


def test_11_mass_assignment_cannot_grant_admin() -> None:
    result = client.patch("/scenario/11", json={"name": "Lin", "is_admin": True})
    assert result.json()["is_admin"] is False


def test_12_page_size_is_capped() -> None:
    assert len(client.get("/scenario/12?limit=101").json()["items"]) <= 100


def test_13_internal_exception_is_not_returned() -> None:
    assert "private database password" not in client.get("/scenario/13").text


def test_14_path_traversal_is_rejected() -> None:
    assert client.get("/scenario/14?filename=..%2Fsecret.txt").status_code == 403


def test_15_delete_returns_no_content() -> None:
    response = client.delete("/scenario/15/1")
    assert response.status_code == 204
    assert response.content == b""


def test_16_inventory_cannot_be_reserved_below_zero() -> None:
    assert client.post("/scenario/16/10").status_code == 409


def test_17_leaderboard_is_sorted_descending() -> None:
    assert client.get("/scenario/17").json()[0]["name"] == "Lin"


def test_18_payment_retry_is_idempotent() -> None:
    first = client.post("/scenario/18/key-1").json()
    second = client.post("/scenario/18/key-1").json()
    assert first == second == {"charged": 1}


def test_19_empty_search_query_is_rejected() -> None:
    assert client.get("/scenario/19").status_code == 422


def test_20_readiness_reports_unavailable_dependency() -> None:
    assert client.get("/scenario/20").status_code == 503
