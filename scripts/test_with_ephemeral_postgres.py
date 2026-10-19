"""Run pytest against a disposable local PostgreSQL server without Docker.

Install the isolated test-server helper first with ``python -m pip install
pgserver==0.1.4``. The helper is used only to supply PostgreSQL binaries;
server data, the bound port, and both databases are created under a unique
temporary directory for this run. The server listens on loopback only.

Examples::

    python scripts/test_with_ephemeral_postgres.py
    python scripts/test_with_ephemeral_postgres.py -- tests/test_healer_app.py
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from core.process_env import sanitized_subprocess_env


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _arguments(argv: Sequence[str] | None) -> list[str]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    args = list(parsed.pytest_args)
    if args[:1] == ["--"]:
        args = args[1:]
    return args or ["-q"]


def _stop_server(pg_ctl: Callable[..., str], data_dir: Path) -> bool:
    try:
        pg_ctl(["-w", "stop"], pgdata=data_dir, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return not (data_dir / "postmaster.pid").exists()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from pgserver import initdb, pg_ctl, postgres
    except ImportError:
        print(
            "Missing optional disposable PostgreSQL helper. Install it with "
            "`python -m pip install pgserver==0.1.4`.",
            file=sys.stderr,
        )
        return 2

    pytest_args = _arguments(argv)
    temp_root = Path(tempfile.mkdtemp(prefix="selfheal-pytest-postgres-"))
    data_dir = temp_root / "data"
    server_started = False
    remove_temp_root = True

    try:
        password = secrets.token_urlsafe(32)
        password_file = temp_root / ".postgres-superuser-password"
        password_file.write_text(password + "\n", encoding="utf-8")
        try:
            password_file.chmod(0o600)
        except OSError:
            # Windows ACLs, rather than POSIX mode bits, protect this file.
            pass
        initdb(
            [
                "--auth-local=scram-sha-256",
                "--auth-host=scram-sha-256",
                "--encoding=UTF8",
                "-U",
                "postgres",
                f"--pwfile={password_file}",
            ],
            pgdata=data_dir,
            timeout=120,
        )
        password_file.unlink(missing_ok=True)
        port = _free_loopback_port()
        pg_ctl(
            [
                "-w",
                "-o",
                '-h "127.0.0.1"',
                "-o",
                f"-p {port}",
                "-o",
                "-c listen_addresses=127.0.0.1",
                "-l",
                str(data_dir / "server.log"),
                "start",
            ],
            pgdata=data_dir,
            timeout=120,
        )
        server_started = True

        print(f"Disposable PostgreSQL: {postgres(['--version']).strip()}")
        print(f"Listening on 127.0.0.1:{port}; test database: selfheal_test")
        env = sanitized_subprocess_env()
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "Selfheal Test Suite",
                "GIT_AUTHOR_EMAIL": "tests@example.invalid",
                "GIT_COMMITTER_NAME": "Selfheal Test Suite",
                "GIT_COMMITTER_EMAIL": "tests@example.invalid",
                "DATABASE_URL": _database_url(port, "selfheal_app", password),
                "TEST_DATABASE_URL": _database_url(port, "selfheal_test", password),
                "ANTHROPIC_API_KEY": "test-mock-not-a-real-key",
                "GEMINI_API_KEY": "test-mock-not-a-real-key",
                "GROQ_API_KEY": "test-mock-not-a-real-key",
                "OPENROUTER_API_KEY": "test-mock-not-a-real-key",
                "GITHUB_TOKEN": "github_pat_test-mock-not-a-real-token",
                "GITHUB_REPO": "test-org/test-repo",
                "AI_CHAIN": "",
                "CHAT_CHAIN": "",
                "SESSION_SECRET": "test-only-session-secret",
                "HEALER_WEBHOOK_SECRET": "test-only-webhook-secret",
                "ADMIN_PASSWORD_HASH": "",
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *pytest_args],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=False,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print("PostgreSQL setup timed out; see the retained temp directory below.", file=sys.stderr)
        remove_temp_root = False
        return 124
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Disposable PostgreSQL test setup failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if "password_file" in locals():
            password_file.unlink(missing_ok=True)
        cluster_may_be_running = server_started or (data_dir / "postmaster.pid").exists()
        stopped = not cluster_may_be_running or _stop_server(pg_ctl, data_dir)
        if not stopped:
            remove_temp_root = False
            print(
                f"Could not confirm PostgreSQL stopped; preserving its generated directory: "
                f"{temp_root}",
                file=sys.stderr,
            )
        if remove_temp_root and temp_root.exists():
            shutil.rmtree(temp_root)
        elif temp_root.exists():
            print(f"Temporary PostgreSQL files retained at {temp_root}", file=sys.stderr)


def _database_url(port: int, database: str, password: str) -> str:
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+asyncpg",
        username="postgres",
        password=password,
        host="127.0.0.1",
        port=port,
        database=database,
    ).render_as_string(hide_password=False)


if __name__ == "__main__":
    raise SystemExit(main())
