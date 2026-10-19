"""mcp_server.github_client: config guards and non-2xx handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.config import settings as core_settings
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient, GitHubClientError


async def test_missing_github_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "github_token", None)
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="GITHUB_TOKEN"):
            await client.list_workflow_runs()


async def test_missing_github_repo_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", None)

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="GITHUB_REPO"):
            await client.list_workflow_runs()


async def test_production_rejects_a_long_lived_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "environment", "production")
    monkeypatch.setattr(core_settings, "github_token", "long-lived-pat")
    monkeypatch.setattr(core_settings, "github_app_id", None)
    monkeypatch.setattr(core_settings, "github_app_installation_id", None)
    monkeypatch.setattr(core_settings, "github_app_private_key", None)
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="requires a GitHub App"):
            await client.get_access_token()


async def test_github_app_token_is_short_lived_and_repository_scoped(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(core_settings, "environment", "production")
    monkeypatch.setattr(core_settings, "github_token", None)
    monkeypatch.setattr(core_settings, "github_app_id", "123456")
    monkeypatch.setattr(core_settings, "github_app_installation_id", "987654")
    monkeypatch.setattr(core_settings, "github_app_private_key", pem)
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    expires_at = (datetime.now(UTC) + timedelta(minutes=55)).isoformat()
    token_route = respx_mock.post(f"{GITHUB_API_BASE}/app/installations/987654/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs-short-lived",
                "expires_at": expires_at,
                "repositories": [{"full_name": "acme/repo"}],
                "permissions": {
                    "actions": "write",
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "metadata": "read",
                },
            },
        )
    )
    repo_route = respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo").mock(
        return_value=httpx.Response(200, json={"full_name": "acme/repo"})
    )

    async with GitHubClient() as client:
        repo = await client.get_repo()
        assert await client.get_access_token() == "ghs-short-lived"

    assert repo["full_name"] == "acme/repo"
    assert repo_route.calls.last.request.headers["Authorization"] == "Bearer ghs-short-lived"
    assert token_route.call_count == 1
    request = token_route.calls.last.request
    assert request.headers["Authorization"].startswith("Bearer ")
    claims = jwt.decode(
        request.headers["Authorization"].removeprefix("Bearer "),
        options={"verify_signature": False, "verify_exp": False},
    )
    assert claims["iss"] == "123456"
    assert claims["exp"] - claims["iat"] == 600
    assert json.loads(request.content) == {
        "repositories": ["repo"],
        "permissions": {
            "actions": "write",
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
        },
    }


async def test_github_app_token_rejects_extra_repository_permissions(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(core_settings, "environment", "production")
    monkeypatch.setattr(core_settings, "github_token", None)
    monkeypatch.setattr(core_settings, "github_app_id", "123456")
    monkeypatch.setattr(core_settings, "github_app_installation_id", "987654")
    monkeypatch.setattr(core_settings, "github_app_private_key", pem)
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    respx_mock.post(f"{GITHUB_API_BASE}/app/installations/987654/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs-overbroad",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=55)).isoformat(),
                "repositories": [{"full_name": "acme/repo"}],
                "permissions": {
                    "actions": "write",
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "administration": "write",
                },
            },
        )
    )

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="exactly the authorized"):
            await client.get_access_token()


async def test_non_2xx_response_raises_with_status_and_body(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/actions/runs/1").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="404"):
            await client.get_workflow_run(1)


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (403, '{"message":"API rate limit exceeded"}'),
        (429, '{"message":"secondary rate limit"}'),
    ],
)
async def test_rate_limited_pr_creation_fails_without_replaying_write(
    monkeypatch: pytest.MonkeyPatch, respx_mock, status_code: int, body: str
) -> None:
    """Surface GitHub rate limits without blindly replaying a non-idempotent PR write."""
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(status_code, text=body)
    )

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match=str(status_code)):
            await client.create_pull_request(
                title="Auto-fix: example", body="evidence", head="autofix/example"
            )

    assert route.call_count == 1


async def test_request_includes_bearer_token_header(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "my-secret-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/actions/runs/1").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    async with GitHubClient() as client:
        await client.get_workflow_run(1)

    assert route.calls.last.request.headers["Authorization"] == "Bearer my-secret-token"


async def test_create_pull_request_posts_title_body_head_base(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(201, json={"number": 7, "html_url": "https://example/pr/7"})
    )

    async with GitHubClient() as client:
        pr = await client.create_pull_request(
            title="Auto-fix: ZeroDivisionError",
            body="root cause...",
            head="autofix/abc-1",
            base="main",
        )

    assert pr["number"] == 7
    payload = json.loads(route.calls.last.request.content)
    assert payload == {
        "title": "Auto-fix: ZeroDivisionError",
        "body": "root cause...",
        "head": "autofix/abc-1",
        "base": "main",
    }


async def test_add_labels_posts_to_issue_labels_endpoint(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues/7/labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with GitHubClient() as client:
        await client.add_labels(7, ["auto-fix"])

    assert json.loads(route.calls.last.request.content) == {"labels": ["auto-fix"]}


async def test_create_issue_posts_title_body_and_labels(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues").mock(
        return_value=httpx.Response(201, json={"number": 9})
    )

    async with GitHubClient() as client:
        issue = await client.create_issue(
            title="Needs human review: KeyError", body="details...", labels=["needs-human-review"]
        )

    assert issue["number"] == 9
    assert json.loads(route.calls.last.request.content) == {
        "title": "Needs human review: KeyError",
        "body": "details...",
        "labels": ["needs-human-review"],
    }


async def test_create_issue_comment_posts_body(monkeypatch: pytest.MonkeyPatch, respx_mock) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues/12/comments").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    async with GitHubClient() as client:
        await client.create_issue_comment(12, "CI is now green.")

    assert json.loads(route.calls.last.request.content) == {"body": "CI is now green."}
