"""The middleware can select a monitored app's own traceback frame."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.middleware import SentinelMiddleware


class CapturingSentinel:
    def __init__(self) -> None:
        self.errors: list[Any] = []

    async def report_error(self, error: Any) -> None:
        self.errors.append(error)

    async def report_metric(self, _event: Any) -> None:
        return None


def test_custom_in_app_marker_captures_the_application_frame() -> None:
    sentinel = CapturingSentinel()
    app = FastAPI()

    @app.get("/failure")
    async def failure() -> None:
        raise RuntimeError("expected fixture failure")

    app.add_middleware(
        SentinelMiddleware,
        sentinel_client=sentinel,
        in_app_markers=("tests/test_sentinel_middleware_markers.py",),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/failure")

    assert response.status_code == 500
    assert len(sentinel.errors) == 1
    assert sentinel.errors[0].file_path.endswith("tests/test_sentinel_middleware_markers.py")
