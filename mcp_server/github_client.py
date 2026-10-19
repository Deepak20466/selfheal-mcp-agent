"""Thin GitHub REST API client (tech stack: httpx, token from `GITHUB_TOKEN`).

Used by `tools/cicd.py`. Tests mock this with `respx` rather than hitting
the real API.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import structlog

from core.config import settings

GITHUB_API_BASE = "https://api.github.com"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_APP_TOKEN_PERMISSIONS = {
    "actions": "write",
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
}
logger = structlog.get_logger(__name__)


class GitHubClientError(Exception):
    """Raised for a missing configuration value or a non-2xx GitHub response."""


class GitHubClient:
    """`repo` overrides `GITHUB_REPO` for this instance (`owner/repo`) — used for
    connect-a-repo (multi-app) flows where each `MonitoredApp` may target a
    different GitHub repo than the default `settings.github_repo`. Omitted,
    this behaves exactly as before (single hardcoded repo from `.env`).
    """

    def __init__(self, client: httpx.AsyncClient | None = None, *, repo: str | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=GITHUB_API_BASE, timeout=15.0)
        self._repo_override = repo
        self._app_tokens: dict[str, tuple[str, datetime]] = {}

    @staticmethod
    def _validate_repository(repo: str) -> str:
        if not _REPOSITORY_RE.fullmatch(repo) or any(
            part in {".", ".."} for part in repo.split("/")
        ):
            raise GitHubClientError("GitHub repository must use the exact owner/repo form")
        return repo

    def _repo_or_raise(self) -> str:
        repo = self._repo_override or settings.github_repo
        if not repo:
            raise GitHubClientError("GITHUB_REPO is not configured")
        return self._validate_repository(repo)

    def _repository_from_path(self, path: str) -> str:
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "repos":
            return self._validate_repository(f"{parts[1]}/{parts[2]}")
        return self._repo_or_raise()

    async def get_access_token(self, repo: str | None = None) -> str:
        """Return a repository-scoped GitHub App token, or a dev PAT.

        Production refuses long-lived PAT fallback. Installation tokens are
        scoped to one repository and expire per GitHub's installation-token
        policy; the token is held only by this trusted client instance.
        """
        target_repo = self._validate_repository(repo or self._repo_or_raise())
        app_values = (
            settings.github_app_id,
            settings.github_app_installation_id,
            settings.github_app_private_key,
        )
        if any(app_values) and not all(app_values):
            raise GitHubClientError(
                "GitHub App ID, installation ID, and private key must all be set"
            )
        if all(app_values):
            assert settings.github_app_id is not None
            assert settings.github_app_installation_id is not None
            if (
                not settings.github_app_id.isdigit()
                or not settings.github_app_installation_id.isdigit()
            ):
                raise GitHubClientError("GitHub App and installation IDs must be numeric")
            return await self._installation_token(target_repo)
        if settings.environment.lower() == "production":
            raise GitHubClientError(
                "Production GitHub access requires a GitHub App installation; configure "
                "GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, and GITHUB_APP_PRIVATE_KEY"
            )
        if settings.github_token:
            return settings.github_token
        raise GitHubClientError("GITHUB_TOKEN or GitHub App credentials are not configured")

    async def _installation_token(self, repo: str) -> str:
        now = datetime.now(UTC)
        cached = self._app_tokens.get(repo)
        if cached is not None and cached[1] > now + timedelta(minutes=2):
            return cached[0]

        assert settings.github_app_id is not None
        assert settings.github_app_installation_id is not None
        assert settings.github_app_private_key is not None
        issued_at = int(now.timestamp())
        try:
            app_jwt = jwt.encode(
                {"iat": issued_at - 60, "exp": issued_at + 540, "iss": settings.github_app_id},
                settings.github_app_private_key,
                algorithm="RS256",
            )
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise GitHubClientError(
                "GitHub App private key could not sign an authentication token"
            ) from exc

        response = await self._client.post(
            f"/app/installations/{settings.github_app_installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "repositories": [repo.split("/", 1)[1]],
                "permissions": _APP_TOKEN_PERMISSIONS,
            },
        )
        if response.status_code >= 400:
            logger.warning(
                "github.app.installation_token_failed",
                repository=repo,
                status_code=response.status_code,
                github_request_id=response.headers.get("x-github-request-id"),
            )
            raise GitHubClientError(f"GitHub App token request failed: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise GitHubClientError("GitHub App returned an invalid installation token response")
        token = data.get("token")
        expires_at = data.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires_at, str):
            raise GitHubClientError("GitHub App returned an invalid installation token response")
        repositories = data.get("repositories")
        actual_repositories = (
            {
                item.get("full_name")
                for item in repositories
                if isinstance(item, dict) and isinstance(item.get("full_name"), str)
            }
            if isinstance(repositories, list)
            else set()
        )
        permissions = data.get("permissions")
        allowed_permission_names = set(_APP_TOKEN_PERMISSIONS) | {"metadata"}
        if (
            actual_repositories != {repo}
            or not isinstance(permissions, dict)
            or not set(permissions).issubset(allowed_permission_names)
            or set(_APP_TOKEN_PERMISSIONS).difference(permissions)
            or any(permissions.get(name) != level for name, level in _APP_TOKEN_PERMISSIONS.items())
            or permissions.get("metadata", "read") != "read"
        ):
            raise GitHubClientError(
                "GitHub App did not grant exactly the authorized repository and "
                "required permissions"
            )
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubClientError("GitHub App returned an invalid token expiration") from exc
        if expires.tzinfo is None or expires <= datetime.now(UTC) + timedelta(minutes=2):
            raise GitHubClientError("GitHub App returned an expired or near-expiry token")
        self._app_tokens[repo] = (token, expires)
        logger.info(
            "github.app.installation_token_minted",
            repository=repo,
            permissions=permissions,
            expires_at=expires.isoformat(),
            github_request_id=response.headers.get("x-github-request-id"),
        )
        return token

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self.get_access_token(self._repository_from_path(path))
        headers = {
            **kwargs.pop("headers", {}),
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = await self._client.request(method, path, headers=headers, **kwargs)
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/repos/"):
            event = {
                "method": method.upper(),
                "repository": self._repository_from_path(path),
                "endpoint": path.split("?", 1)[0],
                "status_code": response.status_code,
                "github_request_id": response.headers.get("x-github-request-id"),
            }
            if response.status_code >= 400:
                logger.warning("github.control_plane.mutation_failed", **event)
            else:
                logger.info("github.control_plane.mutation", **event)
        if response.status_code >= 400:
            raise GitHubClientError(
                f"GitHub API {method} {path} failed: {response.status_code} {response.text[:500]}"
            )
        return response

    async def get_repo(self, repo: str | None = None) -> dict[str, Any]:
        """Fetch repo metadata — used by the "connect a repo" flow to confirm
        `GITHUB_TOKEN` can actually see this repo before cloning/registering it."""
        target = self._validate_repository(repo or self._repo_or_raise())
        response = await self._request("GET", f"/repos/{target}")
        data: dict[str, Any] = response.json()
        return data

    async def list_workflow_runs(
        self, *, branch: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        params: dict[str, str] = {}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        response = await self._request("GET", f"/repos/{repo}/actions/runs", params=params)
        runs: list[dict[str, Any]] = response.json().get("workflow_runs", [])
        return runs

    async def get_workflow_run(self, run_id: int) -> dict[str, Any]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}")
        data: dict[str, Any] = response.json()
        return data

    async def list_jobs_for_run(self, run_id: int) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
        jobs: list[dict[str, Any]] = response.json().get("jobs", [])
        return jobs

    async def get_job_logs_text(self, job_id: int) -> str:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        return response.text

    async def rerun_workflow(self, run_id: int, *, failed_only: bool = True) -> None:
        repo = self._repo_or_raise()
        suffix = "rerun-failed-jobs" if failed_only else "rerun"
        await self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/{suffix}")

    async def cancel_workflow(self, run_id: int) -> None:
        repo = self._repo_or_raise()
        await self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/cancel")

    async def get_pull_request(self, pr_number: int) -> dict[str, Any]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}")
        data: dict[str, Any] = response.json()
        return data

    async def list_pull_request_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}/reviews")
        reviews: list[dict[str, Any]] = response.json()
        return reviews

    async def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/commits/{sha}/check-runs")
        runs: list[dict[str, Any]] = response.json().get("check_runs", [])
        return runs

    async def dispatch_workflow(
        self, workflow_file: str, *, ref: str, inputs: dict[str, str] | None = None
    ) -> None:
        repo = self._repo_or_raise()
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        await self._request(
            "POST", f"/repos/{repo}/actions/workflows/{workflow_file}/dispatches", json=payload
        )

    async def get_branch_sha(self, branch: str) -> str:
        """The commit sha a branch currently points at -- used to base a new
        branch off it (`create_branch`) without a local git clone."""
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        data: dict[str, Any] = response.json()
        sha: str = data["object"]["sha"]
        return sha

    async def create_branch(self, branch: str, *, from_sha: str) -> None:
        """Create `branch` pointing at `from_sha`, via the Git Data API --
        used for a single-file onboarding commit with no local clone."""
        repo = self._repo_or_raise()
        await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )

    async def create_or_update_file(
        self, *, branch: str, path: str, content: str, message: str
    ) -> dict[str, Any]:
        """Create (or update) a single file on `branch` via the Contents API
        -- base64-encodes `content` itself so callers pass plain text."""
        repo = self._repo_or_raise()
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        response = await self._request(
            "PUT",
            f"/repos/{repo}/contents/{path}",
            json={"message": message, "content": encoded, "branch": branch},
        )
        data: dict[str, Any] = response.json()
        return data

    async def create_pull_request(
        self, *, title: str, body: str, head: str, base: str = "main"
    ) -> dict[str, Any]:
        """Open a PR from `head` into `base`. Used by the healer, never surfaced as an MCP tool.

        Not in SPEC.md's mcp-pod tool list — SPEC.md's healer-pod bullet has
        the healer itself "commit ... push and open a PR", so this is called
        directly from `healer/github_ops.py`, the same way `tools/cicd.py`
        already imports `GitHubClient` directly rather than going through MCP.
        """
        repo = self._repo_or_raise()
        response = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        data: dict[str, Any] = response.json()
        return data

    async def add_labels(self, issue_or_pr_number: int, labels: list[str]) -> None:
        """Add labels to a PR or issue (PRs are issues for labeling purposes)."""
        repo = self._repo_or_raise()
        await self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_or_pr_number}/labels",
            json={"labels": labels},
        )

    async def create_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        """Open an issue — SPEC.md's "low confidence" fallback when a heal attempt
        can't produce a verified fix (used instead of opening a PR)."""
        repo = self._repo_or_raise()
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = await self._request("POST", f"/repos/{repo}/issues", json=payload)
        data: dict[str, Any] = response.json()
        return data

    async def create_issue_comment(self, issue_or_pr_number: int, body: str) -> None:
        """Post a comment on an issue or PR (Phase 5 posts CI-fix evidence here)."""
        repo = self._repo_or_raise()
        await self._request(
            "POST", f"/repos/{repo}/issues/{issue_or_pr_number}/comments", json={"body": body}
        )
