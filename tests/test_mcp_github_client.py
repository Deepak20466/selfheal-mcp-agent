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


async def test_repository_outside_explicit_allowlist_is_rejected_before_api_call(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock,
) -> None:
    monkeypatch.setattr(core_settings, "environment", "production")
    monkeypatch.setattr(core_settings, "github_token", "dev-test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    monkeypatch.setattr(core_settings, "github_allowed_repositories", "acme/repo,octo/tool")

    async with GitHubClient(repo="other/private") as client:
        with pytest.raises(GitHubClientError, match="not in the configured repository allowlist"):
            await client.get_repo()

    assert not respx_mock.calls


async def test_unset_allowlist_restricts_access_to_configured_default_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "environment", "production")
    monkeypatch.setattr(core_settings, "github_token", "dev-test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    monkeypatch.setattr(core_settings, "github_allowed_repositories", None)

    async with GitHubClient(repo="octo/tool") as client:
        with pytest.raises(GitHubClientError, match="not in the configured repository allowlist"):
            await client.get_access_token()


async def test_repository_allowlist_comparison_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "environment", "production")
    monkeypatch.setattr(core_settings, "github_token", "dev-test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    monkeypatch.setattr(core_settings, "github_allowed_repositories", "ACME/Repo,octo/tool")

    async with GitHubClient(repo="Acme/Repo") as client:
        assert client._validate_repository("Acme/Repo") == "Acme/Repo"


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

    def issue_token(request: httpx.Request) -> httpx.Response:
        requested = json.loads(request.content)["permissions"]
        return httpx.Response(
            201,
            json={
                "token": "ghs-short-lived",
                "expires_at": expires_at,
                "repositories": [{"full_name": "acme/repo"}],
                "permissions": {**requested, "metadata": "read"},
            },
        )

    token_route = respx_mock.post(f"{GITHUB_API_BASE}/app/installations/987654/access_tokens").mock(
        side_effect=issue_token
    )
    repo_route = respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo").mock(
        return_value=httpx.Response(200, json={"full_name": "acme/repo"})
    )

    async with GitHubClient() as client:
        repo = await client.get_repo()
        assert await client.get_access_token() == "ghs-short-lived"

    assert repo["full_name"] == "acme/repo"
    assert repo_route.calls.last.request.headers["Authorization"] == "Bearer ghs-short-lived"
    assert token_route.call_count == 2
    read_scope = json.loads(token_route.calls[0].request.content)["permissions"]
    assert read_scope == {"contents": "read"}
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
        "permissions": {"contents": "write"},
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


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/repos/acme/repo/actions/runs", {"actions": "read"}),
        ("POST", "/repos/acme/repo/actions/runs/3/rerun-failed-jobs", {"actions": "write"}),
        ("GET", "/repos/acme/repo/pulls/3", {"pull_requests": "read"}),
        ("POST", "/repos/acme/repo/pulls", {"pull_requests": "write"}),
        ("GET", "/repos/acme/repo/issues/3/comments", {"issues": "read"}),
        ("POST", "/repos/acme/repo/issues/3/comments", {"issues": "write"}),
        ("GET", "/repos/acme/repo/commits/abc/check-runs", {"checks": "read"}),
        ("GET", "/repos/acme/repo/git/ref/heads/main", {"contents": "read"}),
        ("POST", "/repos/acme/repo/git/refs", {"contents": "write"}),
    ],
)
def test_github_app_permission_is_minimal_for_each_api_operation(
    method: str,
    path: str,
    expected: dict[str, str],
) -> None:
    assert GitHubClient._permissions_for_request(method, path) == expected


def test_github_app_permission_policy_rejects_unmapped_mutation() -> None:
    with pytest.raises(GitHubClientError, match="no permission policy"):
        GitHubClient._permissions_for_request("DELETE", "/repos/acme/repo/unknown")


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

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
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

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
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


async def test_create_pull_request_returns_existing_exact_open_pr_without_write(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    existing = {
        "number": 7,
        "state": "open",
        "head": {"ref": "autofix/abc-1", "repo": {"full_name": "acme/repo"}},
        "base": {"ref": "main", "repo": {"full_name": "acme/repo"}},
    }
    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(200, json=[existing])
    )
    create_route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls")

    async with GitHubClient() as client:
        result = await client.create_pull_request(
            title="retry title", body="retry body", head="autofix/abc-1", base="main"
        )

    assert result == existing
    assert create_route.call_count == 0


async def test_create_pull_request_reconciles_concurrent_duplicate_422(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    existing = {
        "number": 8,
        "state": "open",
        "head": {"ref": "autofix/race", "repo": {"full_name": "acme/repo"}},
        "base": {"ref": "main", "repo": {"full_name": "acme/repo"}},
    }
    lookup = respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[existing]),
        ]
    )
    create_route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(422, json={"message": "A pull request already exists"})
    )

    async with GitHubClient() as client:
        result = await client.create_pull_request(
            title="Auto-fix", body="evidence", head="autofix/race", base="main"
        )

    assert result == existing
    assert lookup.call_count == 2
    assert create_route.call_count == 1


@pytest.mark.parametrize(
    ("pull", "expected"),
    [
        (
            {
                "state": "open",
                "merged": False,
                "head": {"ref": "pr/fix", "repo": {"full_name": "acme/repo"}},
                "base": {"repo": {"full_name": "acme/repo"}},
            },
            True,
        ),
        (
            {
                "state": "open",
                "merged": False,
                "head": {"ref": "pr/other", "repo": {"full_name": "acme/repo"}},
                "base": {"repo": {"full_name": "acme/repo"}},
            },
            False,
        ),
        (
            {
                "state": "open",
                "merged": False,
                "head": {"ref": "pr/fix", "repo": {"full_name": "fork/repo"}},
                "base": {"repo": {"full_name": "acme/repo"}},
            },
            False,
        ),
        (
            {
                "state": "closed",
                "merged": False,
                "head": {"ref": "pr/fix", "repo": {"full_name": "acme/repo"}},
                "base": {"repo": {"full_name": "acme/repo"}},
            },
            False,
        ),
    ],
)
async def test_open_same_repository_pr_head_validation(
    monkeypatch: pytest.MonkeyPatch, respx_mock, pull: dict[str, object], expected: bool
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/pulls/17").mock(
        return_value=httpx.Response(200, json=pull)
    )

    async with GitHubClient() as client:
        assert await client.is_open_same_repository_pr_head(17, "pr/fix") is expected


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
