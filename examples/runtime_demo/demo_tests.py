"""Regression tests used by the runtime demo's repair job, not default CI discovery."""

from __future__ import annotations

from fastapi.testclient import TestClient

from examples.runtime_demo.main import app, product_page, profile_response


def test_nullable_database_name_uses_a_fallback() -> None:
    assert profile_response(1)["display_name"] == "Guest"


def test_page_beyond_last_page_returns_an_empty_result() -> None:
    assert product_page(3, 3) == {"items": [], "next_cursor": None}


def test_malformed_quantity_is_reported_as_client_input_error() -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/orders/1/total?quantity=many")
    assert response.status_code == 422
