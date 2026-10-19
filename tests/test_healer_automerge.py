"""AI-generated PRs are never merged by the healer service."""

from __future__ import annotations

import pytest

from core.models import HealJob, HealJobType, MonitoredApp
from healer.automerge import check_and_merge_eligible_jobs, effective_auto_merge


def _job(*, override: bool | None) -> HealJob:
    return HealJob(
        type=HealJobType.RUNTIME_ERROR,
        fingerprint="repair-test",
        auto_merge_override=override,
    )


def _app(*, auto_merge: bool) -> MonitoredApp:
    return MonitoredApp(
        name="repair-test",
        language="python",
        local_repo_path="apps/repair-test",
        github_repo="owner/repo",
        allowed_write_paths=["apps/repair-test/"],
        test_command="pytest",
        ingest_token="test-token",
        auto_merge=auto_merge,
    )


@pytest.mark.parametrize(
    ("override", "app_setting"),
    [(None, False), (None, True), (False, True), (True, False)],
)
def test_legacy_settings_never_authorize_automatic_merge(
    override: bool | None, app_setting: bool
) -> None:
    assert effective_auto_merge(_job(override=override), _app(auto_merge=app_setting)) is False


async def test_merge_worker_is_a_no_op() -> None:
    """No database query or GitHub API call can result in an automatic merge."""
    assert await check_and_merge_eligible_jobs() == 0
