"""Environment filtering for commands that execute repository-controlled code.

Connected repositories and AI-generated commands are untrusted input. Their
test/install subprocesses must not inherit the control plane's database,
GitHub, webhook, or provider credentials.
"""

from __future__ import annotations

import os
from collections.abc import Collection

_SECRET_NAME_MARKERS = (
    "API_KEY",
    "ACCESS_KEY",
    "AUTH_TOKEN",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "AUTHORIZATION",
)

_SENSITIVE_ENV_NAMES = frozenset(
    {
        "DATABASE_URL",
        "DATABASE_DSN",
        "TEST_DATABASE_URL",
        "PGPASSWORD",
        "PGPASSFILE",
        "BROKER_URL",
        "CELERY_BROKER_URL",
        "REDIS_URL",
        "SENTRY_DSN",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "HEALER_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
        "SMTP_URL",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "DOCKER_CONFIG",
        "DOCKER_AUTH_CONFIG",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "UV_INDEX_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    }
)


def _is_sensitive_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _SENSITIVE_ENV_NAMES or any(
        marker in normalized for marker in _SECRET_NAME_MARKERS
    )


def sanitized_subprocess_env(*, allow_secret_names: Collection[str] = ()) -> dict[str, str]:
    """Copy the environment while dropping credential-bearing variables.

    A caller may retain a specific credential needed to authenticate the
    selected provider CLI (for example ``OPENAI_API_KEY`` for Codex CLI).
    Repository install/test commands should use the default, which retains no
    secret variables.
    """
    allowed = {name.upper() for name in allow_secret_names}
    return {
        key: value
        for key, value in os.environ.items()
        if not _is_sensitive_name(key) or key.upper() in allowed
    }
