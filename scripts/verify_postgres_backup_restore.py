"""Exercise a complete PostgreSQL dump and restore in the disposable CI service."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

DATABASE_NAME = "selfheal_backup_restore"
PROBE_VALUE = "restore-round-trip-ok"


def _docker_exec(
    container_id: str,
    *args: str,
    input_stream: object | None = None,
    output_stream: object | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", "exec", *(["-i"] if input_stream else []), container_id, *args],
        check=True,
        stdin=input_stream,  # type: ignore[arg-type]
        stdout=subprocess.PIPE if capture_output else output_stream,  # type: ignore[arg-type]
        text=capture_output,
    )


def _database_command(container_id: str, tool: str, *args: str) -> None:
    _docker_exec(container_id, tool, "-U", "selfheal", *args)


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("backup/restore validation is restricted to disposable GitHub Actions")

    container_id = os.environ.get("POSTGRES_SERVICE_CONTAINER", "").strip()
    database_url = os.environ.get("DATABASE_URL", "")
    if not container_id or not database_url:
        raise RuntimeError("PostgreSQL service container and DATABASE_URL are required")

    parsed_url = make_url(database_url)
    if (
        parsed_url.username != "selfheal"
        or parsed_url.host not in {"localhost", "127.0.0.1"}
        or parsed_url.port != 5432
        or parsed_url.database != "selfheal"
    ):
        raise RuntimeError("refusing to run the destructive restore drill outside the CI database")

    restore_url = parsed_url.set(database=DATABASE_NAME).render_as_string(hide_password=False)
    migration_env = os.environ.copy()
    migration_env["DATABASE_URL"] = restore_url

    _database_command(container_id, "dropdb", "--if-exists", DATABASE_NAME)
    _database_command(container_id, "createdb", DATABASE_NAME)
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=migration_env,
            check=True,
        )
        _database_command(
            container_id,
            "psql",
            "-d",
            DATABASE_NAME,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE TABLE backup_restore_probe (value text PRIMARY KEY)",
            "-c",
            "INSERT INTO backup_restore_probe VALUES ('restore-round-trip-ok')",
        )

        with tempfile.TemporaryDirectory(prefix="selfheal-pg-restore-") as temporary:
            archive = Path(temporary) / "selfheal.dump"
            with archive.open("wb") as dump:
                _database_command(
                    container_id,
                    "pg_dump",
                    "--format=custom",
                    "--create",
                    "--dbname",
                    DATABASE_NAME,
                    output_stream=dump,
                )

            _database_command(container_id, "dropdb", DATABASE_NAME)
            with archive.open("rb") as dump:
                _docker_exec(
                    container_id,
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "--create",
                    "--dbname",
                    "postgres",
                    input_stream=dump,
                )

        probe = _docker_exec(
            container_id,
            "psql",
            "-U",
            "selfheal",
            "-d",
            DATABASE_NAME,
            "-At",
            "-c",
            "SELECT value FROM backup_restore_probe",
            capture_output=True,
        )
        if str(probe.stdout).strip() != PROBE_VALUE:
            raise RuntimeError("restored PostgreSQL data does not match the backup")

        expected_revision = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        revision = _docker_exec(
            container_id,
            "psql",
            "-U",
            "selfheal",
            "-d",
            DATABASE_NAME,
            "-At",
            "-c",
            "SELECT version_num FROM alembic_version",
            capture_output=True,
        )
        if str(revision.stdout).strip() != expected_revision:
            raise RuntimeError("restored Alembic revision does not match the current head")
    finally:
        _database_command(container_id, "dropdb", "--if-exists", DATABASE_NAME)

    print("PostgreSQL backup/restore passed: schema, data, and migration state survived")


if __name__ == "__main__":
    main()
