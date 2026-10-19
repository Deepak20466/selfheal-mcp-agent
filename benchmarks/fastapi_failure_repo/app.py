"""Intentionally faulty FastAPI app used by the isolated benchmark harness."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Selfheal benchmark target")

ROOT = Path("/srv/app/files").resolve()
WIDGETS = {1: "gear"}
PROFILE = {"name": "Ada"}
USER = {"name": "Lin", "is_admin": False}
ORDERS: dict[str, dict[str, str]] = {}
CHARGES: list[dict[str, str]] = []
STOCK = {"sku": 5}
LEADERBOARD = [{"name": "Ada", "score": 5}, {"name": "Lin", "score": 10}]
CATALOG = ["apple", "banana", "carrot"]
DEPENDENCY_READY = False


def reset_state() -> None:
    ORDERS.clear()
    CHARGES.clear()
    PROFILE["name"] = "Ada"
    USER.update({"name": "Lin", "is_admin": False})
    STOCK["sku"] = 5


@app.post("/scenario/01")
def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {"name": payload.get("name")}


@app.get("/scenario/02")
def paginate(page: int = 1, limit: int = 2) -> dict[str, list[int]]:
    items = list(range(1, 7))
    start = page * limit
    return {"items": items[start : start + limit]}


@app.get("/scenario/03/{widget_id}")
def get_widget(widget_id: int) -> dict[str, str]:
    return {"name": WIDGETS[widget_id]}


@app.get("/scenario/04")
def average(values: list[float] | None = Query(default=None)) -> dict[str, float]:
    numbers = values or []
    return {"average": sum(numbers) / len(numbers)}


@app.get("/scenario/05")
def next_event() -> dict[str, str]:
    return {"at": datetime(2030, 1, 1, 12, 0, 0).isoformat()}


@app.get("/scenario/06")
def total() -> dict[str, str]:
    return {"total": str(0.1 + 0.2)}


@app.patch("/scenario/07")
def patch_profile(payload: dict[str, Any]) -> dict[str, str | None]:
    PROFILE["name"] = payload.get("name")
    return PROFILE


@app.post("/scenario/08")
def create_widget() -> dict[str, str]:
    return {"id": "new-widget"}


@app.post("/scenario/09/{order_id}")
def create_order(order_id: str) -> JSONResponse:
    ORDERS[order_id] = {"id": order_id}
    return JSONResponse(ORDERS[order_id], status_code=201)


@app.get("/scenario/10")
def private_data(x_api_key: str | None = None) -> dict[str, str]:
    return {"secret": "private"}


@app.patch("/scenario/11")
def patch_user(payload: dict[str, Any]) -> dict[str, Any]:
    USER.update(payload)
    return USER


@app.get("/scenario/12")
def bounded_page(limit: int = 10) -> dict[str, list[int]]:
    return {"items": list(range(limit))}


@app.get("/scenario/13")
def safe_error() -> JSONResponse:
    try:
        raise RuntimeError("private database password")
    except Exception as exc:  # noqa: BLE001 - intentionally faulty fixture
        return JSONResponse({"detail": str(exc)}, status_code=500)


@app.get("/scenario/14")
def resolve_file(filename: str) -> dict[str, str]:
    return {"path": str((ROOT / filename).resolve())}


@app.delete("/scenario/15/{widget_id}")
def delete_widget(widget_id: int) -> dict[str, bool]:
    return {"deleted": True}


@app.post("/scenario/16/{quantity}")
def reserve_inventory(quantity: int) -> dict[str, int]:
    STOCK["sku"] -= quantity
    return {"remaining": STOCK["sku"]}


@app.get("/scenario/17")
def leaderboard() -> list[dict[str, int | str]]:
    return LEADERBOARD


@app.post("/scenario/18/{key}")
def charge_payment(key: str) -> dict[str, int]:
    CHARGES.append({"key": key})
    return {"charged": len(CHARGES)}


@app.get("/scenario/19")
def search_catalog(q: str = "") -> list[str]:
    return [item for item in CATALOG if q.lower() in item.lower()]


@app.get("/scenario/20")
def readiness() -> dict[str, str]:
    return {"status": "ready"}
