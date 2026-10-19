"""Credential-safe environment for trusted Git control-plane commands."""

from __future__ import annotations

import base64
import os

from sentinel.scrubber import scrub_text


def git_subprocess_env(
    *, github_token: str | None = None
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return a minimal Git environment and values that must be scrubbed.

    GitHub credentials travel as a process-scoped HTTP header through Git's
    command config environment variables. This keeps them out of argv and
    repository config. System/global config and configured credential helpers
    are disabled so ambient helpers and filters do not run.
    """
    env = {
        name: os.environ[name]
        for name in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "LANG",
            "LC_ALL",
        )
        if name in os.environ
    }
    config_entries = [
        ("credential.helper", ""),
        ("core.fsmonitor", "false"),
        ("core.hooksPath", os.devnull),
    ]
    if os.name == "nt":
        # Git for Windows normally reads core.autocrlf=true from its system
        # config. Keep the behavior deterministic after system config is
        # disabled so LF patches produced from read_file still apply to
        # CRLF checkouts of connected repositories.
        config_entries.append(("core.autocrlf", "true"))

    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "Selfheal Bot"),
            "GIT_AUTHOR_EMAIL": os.environ.get(
                "GIT_AUTHOR_EMAIL", "selfheal-bot@users.noreply.github.com"
            ),
            "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "Selfheal Bot"),
            "GIT_COMMITTER_EMAIL": os.environ.get(
                "GIT_COMMITTER_EMAIL", "selfheal-bot@users.noreply.github.com"
            ),
        }
    )
    for index, (key, value) in enumerate(config_entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(len(config_entries))

    sensitive_values: tuple[str, ...] = ()
    if github_token:
        encoded = base64.b64encode(f"x-access-token:{github_token}".encode()).decode("ascii")
        header = f"AUTHORIZATION: basic {encoded}"
        auth_index = len(config_entries)
        env.update(
            {
                "GIT_CONFIG_COUNT": str(auth_index + 1),
                f"GIT_CONFIG_KEY_{auth_index}": "http.https://github.com/.extraheader",
                f"GIT_CONFIG_VALUE_{auth_index}": header,
            }
        )
        sensitive_values = (github_token, encoded, header)
    return env, sensitive_values


def scrub_git_message(message: str, sensitive_values: tuple[str, ...] = ()) -> str:
    for value in sensitive_values:
        if value:
            message = message.replace(value, "[REDACTED]")
    return scrub_text(message)
