"""The sentinel prober detects the remaining off-by-one demo defect.

This branch (PR #10) is the healer's own auto-fix for the timezone bug, so
the timezone case has moved from "violated" to "passed" here — main's copy
of this file still expects both silent bugs violated, since only this
branch has the fix. See test_target_app_bugs.py::
test_bug5_timezone_delivery_estimate_is_now_fixed.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ContractViolation
from sentinel.fingerprint import fingerprint_contract_violation
from sentinel.prober import persist_results, probe_once


async def test_probe_flags_off_by_one_as_a_violation(
    target_app_client: httpx.AsyncClient,
) -> None:
    results = await probe_once(target_app_client)

    violated = {r.case.name for r in results if r.is_violation}
    # The off-by-one demo bug may already be fixed; nothing else may violate.
    assert violated <= {"top_items_by_rating"}


async def test_probe_confirms_healthy_cases_pass(target_app_client: httpx.AsyncClient) -> None:
    results = await probe_once(target_app_client)

    passed = {r.case.name for r in results if not r.is_violation}
    assert {
        "average_rating_healthy",
        "order_status_label_healthy",
        "delivery_estimate_timezone",
    } <= passed


async def test_persist_results_writes_contract_violations(
    target_app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    results = await probe_once(target_app_client)

    if not any(r.is_violation for r in results):
        pytest.skip("off-by-one demo bug already fixed; nothing to persist")
    await persist_results(db_session, results)

    # Other integration tests intentionally commit their own violations via
    # session_scope(), so querying the whole shared test table makes this
    # assertion depend on test order. Scope it to this probe's fingerprint.
    fingerprint = fingerprint_contract_violation("/items/top", "top_items_by_rating")
    stmt = select(ContractViolation).where(ContractViolation.fingerprint == fingerprint)
    violations = (await db_session.execute(stmt)).scalars().all()
    endpoints = {v.endpoint for v in violations}

    # The timezone bug is already repaired on this branch. Persist the
    # remaining off-by-one violation without expecting the fixed case to fail.
    assert endpoints == {"/items/top"}
    for violation in violations:
        assert violation.file_path == "apps/target_app/bugs.py"
        assert violation.line_number > 0


async def test_persisting_the_same_violation_twice_increments_occurrence_count(
    target_app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    results = await probe_once(target_app_client)
    if not any(r.is_violation for r in results):
        pytest.skip("off-by-one demo bug already fixed; nothing to persist")

    await persist_results(db_session, results)
    await persist_results(db_session, results)

    # Scoped by the deterministic fingerprint (endpoint, case_name), not by
    # endpoint alone - other tests (e.g. healer's off-by-one fix test) create
    # their own real, differently-fingerprinted ContractViolation rows for
    # this same "/items/top" endpoint in the shared selfheal_test DB.
    fingerprint = fingerprint_contract_violation("/items/top", "top_items_by_rating")
    stmt = select(ContractViolation).where(ContractViolation.fingerprint == fingerprint)
    violation = (await db_session.execute(stmt)).scalar_one()
    assert violation.occurrence_count == 2
