"""Health, request correlation, and Prometheus metrics endpoints."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

import healer.app as healer_app
from core.models import HealJobStatus


def test_health_response_carries_a_validated_correlation_id() -> None:
    request_id = str(uuid.uuid4())
    with TestClient(healer_app.app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": request_id})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == request_id


def test_invalid_correlation_id_is_replaced_with_a_uuid() -> None:
    with TestClient(healer_app.app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "bad id\r\n"})

    assert response.status_code == 200
    assert str(uuid.UUID(response.headers["X-Request-ID"]))


def test_prometheus_metrics_report_jobs_latency_and_cost(monkeypatch: Any) -> None:
    class Result:
        def all(self) -> list[tuple[HealJobStatus, int]]:
            return [(HealJobStatus.QUEUED, 2), (HealJobStatus.FAILED, 1)]

    class Session:
        def __init__(self) -> None:
            self.values = [Decimal("0.03"), Decimal("12.5")]

        async def execute(self, _statement: Any) -> Result:
            return Result()

        async def scalar(self, _statement: Any) -> Decimal:
            return self.values.pop(0)

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield Session()

    monkeypatch.setattr(healer_app, "session_scope", fake_scope)
    response = asyncio.run(healer_app.prometheus_metrics())
    body = response.body.decode("utf-8")

    assert "selfheal_repairs_attempted_total 3" in body
    assert "selfheal_repairs_failed_total 1" in body
    assert "selfheal_repair_duration_seconds_avg 12.500000" in body
    assert "selfheal_repair_cost_usd_total 0.030000" in body
