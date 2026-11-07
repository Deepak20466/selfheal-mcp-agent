"""Worker shutdown interrupts idle waits and releases the notification task."""

from __future__ import annotations

import asyncio

import pytest

from healer.worker import _wait_for_notification_or_shutdown


class BlockingListener:
    def __init__(self) -> None:
        self.cancelled = False

    async def wait(self) -> str:
        try:
            await asyncio.Future()
        finally:
            self.cancelled = True


@pytest.mark.asyncio
async def test_shutdown_signal_interrupts_wait_and_cancels_notification() -> None:
    listener = BlockingListener()
    shutdown_requested = asyncio.Event()
    waiting = asyncio.create_task(
        _wait_for_notification_or_shutdown(listener, shutdown_requested)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    shutdown_requested.set()
    await asyncio.wait_for(waiting, timeout=0.1)

    assert listener.cancelled
