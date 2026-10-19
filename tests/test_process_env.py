"""Credential filtering for commands that execute repository-controlled code."""

from __future__ import annotations

from core.process_env import sanitized_subprocess_env


def test_sanitized_environment_drops_service_credentials(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@host/db")
    monkeypatch.setenv("HEALER_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "C:/private/service-account.json")
    monkeypatch.setenv("API_KEY", "bare-api-key")
    monkeypatch.setenv("TOKEN", "bare-token")
    monkeypatch.setenv("PGPASSWORD", "postgres-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-secret@example.test")

    env = sanitized_subprocess_env()

    assert env["PATH"] == "safe-path"
    assert "GITHUB_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "HEALER_WEBHOOK_SECRET" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "API_KEY" not in env
    assert "TOKEN" not in env
    assert "PGPASSWORD" not in env
    assert "HTTPS_PROXY" not in env


def test_selected_provider_credential_can_be_explicitly_retained(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-provider-key")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")

    env = sanitized_subprocess_env(allow_secret_names={"openai_api_key"})

    assert env["OPENAI_API_KEY"] == "test-provider-key"
    assert "GITHUB_TOKEN" not in env
