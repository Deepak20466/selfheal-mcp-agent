"""Manual bug reports: allowlisted creation and suggest-only handling."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as core_settings
from core.db import get_db
from core.models import AuditLog, Finding, FindingCategory, MonitoredApp

PASSWORD = "manual findings test password"


@pytest.fixture(autouse=True)
def _configure_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "admin_password_hash", PasswordHasher().hash(PASSWORD))
    monkeypatch.setattr(core_settings, "session_secret", "manual-findings-test-session-secret")
    monkeypatch.setattr(core_settings, "environment", "development")


@pytest_asyncio.fixture
async def registered_app(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[MonitoredApp, None]:
    repo = f"acme/manual-{uuid.uuid4().hex[:10]}"
    app_row = MonitoredApp(
        name=f"manual-{uuid.uuid4().hex[:10]}",
        language="python",
        local_repo_path="examples/runtime_demo",
        github_repo=repo,
        allowed_write_paths=["examples/runtime_demo/"],
        test_command="pytest",
        ingest_token=uuid.uuid4().hex,
    )
    db_session.add(app_row)
    await db_session.flush()
    monkeypatch.setattr(core_settings, "github_allowed_repositories", repo)
    yield app_row


@pytest_asyncio.fixture
async def manual_client(
    db_session: AsyncSession,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    from healer import app as healer_app_module

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    healer_app_module.app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=healer_app_module.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://healer.test")
    try:
        yield client
    finally:
        await client.aclose()
        healer_app_module.app.dependency_overrides.clear()


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 200


def _report(repo: str, **overrides: str) -> dict[str, str]:
    data = {
        "repo": repo,
        "file_path": "examples/runtime_demo/app.py",
        "description": "The branch misses a boundary case.",
    }
    return data | overrides


async def test_manual_report_is_scrubbed_and_only_queues_suggest_mode(
    manual_client: httpx.AsyncClient,
    registered_app: MonitoredApp,
    db_session: AsyncSession,
) -> None:
    async with manual_client as client:
        await _login(client)
        created = await client.post(
            "/api/findings/manual",
            json=_report(
                registered_app.github_repo, description="Bug details token=local-test-value"
            ),
        )
        assert created.status_code == 201, created.text
        finding_data = created.json()
        assert finding_data["category"] == FindingCategory.MANUAL.value
        assert finding_data["file_path"] == "examples/runtime_demo/app.py"
        assert finding_data["message"] == "Bug details token=[REDACTED]"

        # Manual reports cannot enter the verification-capable fix route.
        verified_attempt = await client.post(f"/api/findings/{finding_data['id']}/fix", json={})
        assert verified_attempt.status_code == 409
        assert "--suggest" in verified_attempt.json()["detail"]

        suggested = await client.post(
            f"/api/findings/{finding_data['id']}/fix", json={"suggest": True}
        )
        assert suggested.status_code == 200, suggested.text

    finding = await db_session.get(Finding, finding_data["id"])
    assert finding is not None
    assert finding.category == FindingCategory.MANUAL
    assert finding.message == "Bug details token=[REDACTED]"
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "suggest_mode_requested",
                AuditLog.heal_job_id == suggested.json()["heal_job_id"],
            )
        )
    ).scalar_one_or_none()
    assert audit is not None


async def test_manual_report_rejects_invalid_paths_and_blank_descriptions(
    manual_client: httpx.AsyncClient, registered_app: MonitoredApp
) -> None:
    async with manual_client as client:
        await _login(client)
        traversal = await client.post(
            "/api/findings/manual",
            json=_report(registered_app.github_repo, file_path="../secret.py"),
        )
        outside_scope = await client.post(
            "/api/findings/manual", json=_report(registered_app.github_repo, file_path="README.md")
        )
        secret_path = await client.post(
            "/api/findings/manual",
            json=_report(registered_app.github_repo, file_path="examples/runtime_demo/.env"),
        )
        blank_description = await client.post(
            "/api/findings/manual", json=_report(registered_app.github_repo, description="  ")
        )

    assert traversal.status_code == 400
    assert outside_scope.status_code == 400
    assert secret_path.status_code == 400
    assert blank_description.status_code == 422


async def test_manual_report_requires_the_existing_repository_allowlist(
    manual_client: httpx.AsyncClient,
    registered_app: MonitoredApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "github_allowed_repositories", "acme/other")
    async with manual_client as client:
        await _login(client)
        response = await client.post(
            "/api/findings/manual", json=_report(registered_app.github_repo)
        )
    assert response.status_code == 403
