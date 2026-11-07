"""Runtime-error demo with three intentionally seeded production-style bugs."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from sentinel.client import SentinelClient
from sentinel.middleware import SentinelMiddleware

PROFILES: dict[int, dict[str, str | None]] = {
    1: {"id": "1", "display_name": None},  # nullable database column
}
PRODUCTS = [{"id": str(index), "name": f"Product {index}"} for index in range(1, 6)]
ORDERS = {1: {"id": 1, "unit_price": 12.5}}


def profile_response(profile_id: int) -> dict[str, str]:
    """Format a database record whose display_name column is nullable."""
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    # Seeded bug: nullable values reach a string method without a fallback.
    display_name = profile["display_name"].strip()  # type: ignore[union-attr]
    return {"id": profile["id"] or "", "display_name": display_name}


def product_page(page: int, page_size: int) -> dict[str, Any]:
    start = (page - 1) * page_size
    rows = PRODUCTS[start : start + page_size]
    # Seeded bug: a valid page beyond the last page has no last row.
    next_cursor = rows[-1]["id"]
    return {"items": rows, "next_cursor": next_cursor}


def order_total(order_id: int, raw_quantity: str) -> dict[str, float | int]:
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    # Seeded bug: malformed user input raises ValueError and becomes HTTP 500.
    quantity = int(raw_quantity)
    return {"order_id": order_id, "total": float(order["unit_price"]) * quantity}


def create_app(sentinel_client: SentinelClient | None = None) -> FastAPI:
    app = FastAPI(title="Selfheal runtime demo")
    app.add_middleware(
        SentinelMiddleware,
        sentinel_client=sentinel_client,
        in_app_markers=("examples/runtime_demo",),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/profiles/{profile_id}")
    async def get_profile(profile_id: int) -> dict[str, str]:
        return profile_response(profile_id)

    @app.get("/products")
    async def get_products(
        page: int = Query(default=1), page_size: int = Query(default=3, ge=1, le=100)
    ) -> dict[str, Any]:
        return product_page(page, page_size)

    @app.get("/orders/{order_id}/total")
    async def get_order_total(
        order_id: int, quantity: str = Query(default="1")
    ) -> dict[str, float | int]:
        return order_total(order_id, quantity)

    return app


app = create_app()
