"""Reference repair version for validating the benchmark's regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
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
    if not payload.get("name"):
        raise HTTPException(status_code=422, detail="name is required")
    return {"name": payload["name"]}


@app.get("/scenario/02")
def paginate(page: int = 1, limit: int = 2) -> dict[str, list[int]]:
    items = list(range(1, 7))
    start = (page - 1) * limit
    return {"items": items[start : start + limit]}


@app.get("/scenario/03/{widget_id}")
def get_widget(widget_id: int) -> dict[str, str]:
    if widget_id not in WIDGETS:
        raise HTTPException(status_code=404, detail="widget not found")
    return {"name": WIDGETS[widget_id]}


@app.get("/scenario/04")
def average(values: list[float] | None = Query(default=None)) -> dict[str, float]:
    if not values:
        raise HTTPException(status_code=422, detail="at least one value is required")
    return {"average": sum(values) / len(values)}


@app.get("/scenario/05")
def next_event() -> dict[str, str]:
    return {"at": datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).isoformat()}


@app.get("/scenario/06")
def total() -> dict[str, str]:
    return {"total": f"{Decimal('0.1') + Decimal('0.2'):.2f}"}


@app.patch("/scenario/07")
def patch_profile(payload: dict[str, Any]) -> dict[str, str]:
    if "name" in payload:
        PROFILE["name"] = payload["name"]
    return PROFILE


@app.post("/scenario/08", status_code=201)
def create_widget() -> dict[str, str]:
    return {"id": "new-widget"}


@app.post("/scenario/09/{order_id}")
def create_order(order_id: str) -> JSONResponse:
    if order_id in ORDERS:
        raise HTTPException(status_code=409, detail="order already exists")
    ORDERS[order_id] = {"id": order_id}
    return JSONResponse(ORDERS[order_id], status_code=201)


@app.get("/scenario/10")
def private_data(
    x_api_key: str | None = Header(default=None),
) -> dict[str, str]:
    if x_api_key != "test-key":
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"secret": "private"}


@app.patch("/scenario/11")
def patch_user(payload: dict[str, Any]) -> dict[str, Any]:
    USER["name"] = payload.get("name", USER["name"])
    return USER


@app.get("/scenario/12")
def bounded_page(limit: int = 10) -> dict[str, list[int]]:
    return {"items": list(range(min(limit, 100)))}


@app.get("/scenario/13")
def safe_error() -> JSONResponse:
    try:
        raise RuntimeError("private database password")
    except Exception:  # noqa: BLE001 - intentional fixture error
        return JSONResponse({"detail": "internal server error"}, status_code=500)


@app.get("/scenario/14")
def resolve_file(filename: str) -> dict[str, str]:
    target = (ROOT / filename).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise HTTPException(status_code=403, detail="path is outside the file root")
    return {"path": str(target)}


@app.delete("/scenario/15/{widget_id}")
def delete_widget(widget_id: int) -> Response:
    return Response(status_code=204)


@app.post("/scenario/16/{quantity}")
def reserve_inventory(quantity: int) -> dict[str, int]:
    if quantity <= 0 or quantity > STOCK["sku"]:
        raise HTTPException(status_code=409, detail="insufficient inventory")
    STOCK["sku"] -= quantity
    return {"remaining": STOCK["sku"]}


@app.get("/scenario/17")
def leaderboard() -> list[dict[str, int | str]]:
    return sorted(LEADERBOARD, key=lambda player: player["score"], reverse=True)


@app.post("/scenario/18/{key}")
def charge_payment(key: str) -> dict[str, int]:
    if key not in {charge["key"] for charge in CHARGES}:
        CHARGES.append({"key": key})
    return {"charged": len(CHARGES)}


@app.get("/scenario/19")
def search_catalog(q: str = "") -> list[str]:
    if not q.strip():
        raise HTTPException(status_code=422, detail="search query is required")
    return [item for item in CATALOG if q.lower() in item.lower()]


@app.get("/scenario/20")
def readiness() -> dict[str, str]:
    if not DEPENDENCY_READY:
        raise HTTPException(status_code=503, detail="dependency unavailable")
    return {"status": "ready"}
