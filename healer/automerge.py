"""Automatic PR merging is disabled: AI-generated fixes require human action.

The compatibility functions below keep older callers from turning legacy
AUTO_MERGE settings or database overrides into GitHub merge operations.
"""

from __future__ import annotations

from core.models import HealJob, MonitoredApp


def effective_auto_merge(job: HealJob, app: MonitoredApp | None) -> bool:
    """Always return false, even when legacy configuration asks to auto-merge."""
    return False


async def check_and_merge_eligible_jobs() -> int:
    """Compatibility no-op; this service performs no GitHub merge operation."""
    return 0
