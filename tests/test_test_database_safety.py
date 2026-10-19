"""Pytest must fail closed if its configured database aliases the app DB."""

from __future__ import annotations

import pytest

from tests import conftest as shared_conftest


def test_refuses_explicit_test_database_with_same_name_as_application_db(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://app-user:app-password@db.example/appdb"
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://test-user:test-password@localhost/appdb"
    )

    with pytest.raises(RuntimeError, match="same database name") as exc_info:
        shared_conftest._test_database_url()

    assert "app-password" not in str(exc_info.value)
    assert "test-password" not in str(exc_info.value)


def test_accepts_a_distinct_explicit_test_database(monkeypatch) -> None:
    test_url = "postgresql+asyncpg://selfheal:selfheal@localhost/selfheal_test"
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://selfheal:selfheal@localhost/selfheal")
    monkeypatch.setenv("TEST_DATABASE_URL", test_url)

    assert shared_conftest._test_database_url() == test_url


def test_refuses_explicit_database_without_test_suffix(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://selfheal:selfheal@localhost/selfheal")
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://selfheal:selfheal@localhost/postgres"
    )

    with pytest.raises(RuntimeError, match="ending in '_test'"):
        shared_conftest._test_database_url()


def test_derives_throwaway_database_name_when_not_explicit(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://selfheal:selfheal@localhost/appdb")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    assert shared_conftest._test_database_url().endswith("/appdb_test")


def test_does_not_reuse_app_database_that_already_has_test_suffix(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://selfheal:selfheal@localhost/app_test")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    assert shared_conftest._test_database_url().endswith("/app_test_test")
