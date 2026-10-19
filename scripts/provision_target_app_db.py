"""Create the least-privileged database used only by the monitored demo app.

This runs in the trusted post-install migration Job. The app pod receives the
target database URL only; the main control-plane database remains unavailable
to its role at the PostgreSQL permission layer as well as at the NetworkPolicy
layer.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from sqlalchemy.engine import URL, make_url


class ProvisioningError(RuntimeError):
    """A sanitized failure description that never includes database secrets."""


def _postgres_dsn(url: URL) -> str:
    """Render a SQLAlchemy URL in the URI form accepted by asyncpg."""
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def provision() -> None:
    stage = "parse database URLs"
    connection: asyncpg.Connection | None = None
    try:
        admin_url = make_url(os.environ["DATABASE_URL"])
        target_url = make_url(os.environ["TARGET_APP_DATABASE_URL"])
        admin_database = admin_url.database
        target_database = target_url.database
        target_username = target_url.username
        target_password = target_url.password

        stage = "validate database separation"
        if not admin_database or not target_database or not target_username or not target_password:
            raise ValueError("database URLs must include database and target-role credentials")
        if (
            target_database in {admin_database, "postgres", "template0", "template1"}
            or target_username == admin_url.username
        ):
            raise ValueError("target-app database and role must be separate from the control plane")
        if (target_url.host, target_url.port) != (admin_url.host, admin_url.port):
            raise ValueError(
                "target-app and control-plane databases must use the configured Postgres service"
            )

        stage = "connect to Postgres administration database"
        bootstrap_url = admin_url.set(drivername="postgresql", database="postgres")
        connection = await asyncpg.connect(_postgres_dsn(bootstrap_url), timeout=10)

        stage = "verify administrator privileges"
        is_superuser = await connection.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        if not is_superuser:
            raise PermissionError(
                "configured migration database role is not a Postgres administrator"
            )

        role_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", target_username
        )
        stage = "create or update restricted target-app role"
        role_verb = "ALTER ROLE" if role_exists else "CREATE ROLE"
        role_statement = (
            "SELECT format($1 || ' %I WITH LOGIN PASSWORD %L "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 20', $2, $3)"
        )
        role_sql = await connection.fetchval(
            role_statement,
            role_verb,
            target_username,
            target_password,
        )
        await connection.execute(role_sql)

        database_owner = await connection.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1",
            target_database,
        )
        if database_owner is None:
            stage = "create isolated target-app database"
            create_database_sql = await connection.fetchval(
                "SELECT format('CREATE DATABASE %I OWNER %I', $1, $2)",
                target_database,
                target_username,
            )
            await connection.execute(create_database_sql)
        elif database_owner != target_username:
            raise PermissionError("existing target-app database has an unexpected owner")

        # PUBLIC can connect to databases by default in PostgreSQL. Remove that
        # path from the control-plane and maintenance databases so a compromised
        # target app cannot switch database names and reuse its own role.
        database_rows = await connection.fetch(
            "SELECT datname FROM pg_database WHERE datallowconn AND datname <> $1",
            target_database,
        )
        stage = "restrict PUBLIC access to databases"
        for database_row in database_rows:
            database_name = database_row["datname"]
            revoke_sql = await connection.fetchval(
                "SELECT format('REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC', $1)",
                database_name,
            )
            await connection.execute(revoke_sql)

        revoke_target_sql = await connection.fetchval(
            "SELECT format('REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC', $1)",
            target_database,
        )
        await connection.execute(revoke_target_sql)
        grant_target_sql = await connection.fetchval(
            "SELECT format('GRANT CONNECT ON DATABASE %I TO %I', $1, $2)",
            target_database,
            target_username,
        )
        await connection.execute(grant_target_sql)
    except Exception as exc:
        raise ProvisioningError(
            f"Target-app database provisioning failed during {stage} ({type(exc).__name__})."
        ) from None
    finally:
        if connection is not None:
            await connection.close()

    print(
        f"Provisioned isolated target-app database {target_database!r} "
        f"for restricted role {target_username!r}."
    )


def main() -> None:
    try:
        asyncio.run(provision())
    except ProvisioningError as exc:
        # The exception message contains only a fixed stage name and class;
        # driver/server details can contain SQL or database credentials.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
