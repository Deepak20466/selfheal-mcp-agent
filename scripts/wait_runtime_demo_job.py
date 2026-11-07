"""Wait for the runtime demo's most recently captured repair job to finish."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.db import dispose_engine, session_scope
from core.models import HealJob, MonitoredApp


async def _latest_job(since: datetime) -> tuple[str, int | None, str | None] | None:
    async with session_scope() as session:
        app = (
            await session.execute(select(MonitoredApp).where(MonitoredApp.name == "runtime_demo"))
        ).scalar_one_or_none()
        if app is None:
            return None
        job = (
            await session.execute(
                select(HealJob)
                .where(HealJob.app_id == app.id, HealJob.created_at >= since)
                .order_by(HealJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        return job.status.value, job.pr_number, job.error_message


async def _wait(since: datetime, timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        row = await _latest_job(since)
        if row is None:
            print("Waiting for Sentinel to enqueue a runtime_demo job...")
        else:
            status, pr_number, error = row
            print(f"Runtime demo heal job status: {status}")
            if status == "pr_opened":
                print(
                    f"Review PR: https://github.com/Deepak20466/selfheal-mcp-agent/pull/{pr_number}"
                )
                return 0
            if status in {"failed", "paused_budget"}:
                print(f"No PR was opened (job status: {status}).")
                if error:
                    print("Job recorded an error; inspect the healer log and job details.")
                return 2
        await asyncio.sleep(3)
    print(f"No completed PR job appeared within {timeout_seconds:g}s.")
    return 2


async def _run(since: datetime, timeout_seconds: float) -> int:
    try:
        return await _wait(since, timeout_seconds)
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--timeout", dest="timeout_seconds", type=float, default=600)
    args = parser.parse_args()
    return asyncio.run(_run(args.since, args.timeout_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
