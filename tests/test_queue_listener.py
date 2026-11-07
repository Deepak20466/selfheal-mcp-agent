"""NOTIFY callbacks use a bounded, coalescing wakeup queue."""

from __future__ import annotations

import asyncio

import pytest

from core.queue import HealJobListener


@pytest.mark.asyncio
async def test_listener_coalesces_notifications_without_growing_queue() -> None:
    listener = HealJobListener("postgresql://example.invalid/selfheal_test")
    listener._on_notify(None, 1, "heal_jobs", "first")  # type: ignore[arg-type]
    listener._on_notify(None, 1, "heal_jobs", "second")  # type: ignore[arg-type]

    assert listener._queue.maxsize == 1
    assert listener._queue.qsize() == 1
    assert await asyncio.wait_for(listener.wait(), timeout=0.1) == "first"
