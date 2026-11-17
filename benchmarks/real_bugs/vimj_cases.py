"""Frozen VIMJ repair cases shared by the sandboxed benchmark harnesses.

Every case is pinned to the same clean baseline commit. A case names its single allowed source
file, the frozen regression test (added from a pinned test-only overlay), and full SHA-256 pins
for the source, the overlay and the canonical (LF-normalized) staged test file. The reference
patch, where one exists, is used ONLY to self-check the harness; it is never shown to a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from benchmarks.real_bugs import unresolved_eval as ue

BASELINE_COMMIT = "6490833d178b71624d024b16046240a20a3f78a6"
UNRESOLVED = ue.ROOT / "unresolved"


@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    source_path: str  # relative to backend/
    test_file: str  # relative to backend/
    test_id: str  # pytest node id, relative to backend/
    overlay_path: Path
    overlay_sha256: str
    source_sha256: str
    test_canonical_sha256: str
    failure_tokens: tuple[str, ...]  # must all appear in the baseline failure output
    failure_text: str  # the baseline failure shown to a model (assertion lines only)
    reference_patch: Path | None = None  # harness self-check only; never shown to a model


FEE_BALANCE = Case(
    case_id="fee-balance",
    title="Outstanding fee balance not updated when a fee amount changes",
    source_path=ue.SOURCE_PATH,
    test_file="tests/test_requested_changes.py",
    test_id=ue.TEST_PATH,
    overlay_path=ue.PATCH_PATH,
    overlay_sha256="c77b210cef093b15370493e96daf76219f5f16c3f13224a26b955a40e70c61d4",
    source_sha256=ue.EXPECTED_SOURCE_SHA256,
    test_canonical_sha256=ue.EXPECTED_TEST_SHA256,
    failure_tokens=("125.00", "175", "1 failed"),
    failure_text=(
        f"FAILED {ue.TEST_PATH}\n"
        "tests/test_requested_changes.py:333: in "
        "test_updating_unpaid_fee_amount_keeps_outstanding_balance_in_sync\n"
        "    self.assertEqual(Decimal(updated.json()['balance_amount']), Decimal('175'))\n"
        "E   AssertionError: Decimal('125.00') != Decimal('175')\n"
        "1 failed"
    ),
)

PASSWORD_RESET = Case(
    case_id="password-reset-expiry",
    title="Password reset fails when the database returns a naive expiry timestamp",
    source_path="app/routers/auth.py",
    test_file="tests/test_auth_login.py",
    test_id=(
        "tests/test_auth_login.py::AuthLoginTests::test_password_reset_accepts_a_valid_database_token"
    ),
    overlay_path=UNRESOLVED / "vimj-password-reset-v1-test.patch",
    overlay_sha256="6cffbe7dd07c2317f310cc7c61a81b8d66a8449c3922aff2d29b20a82739d1fc",
    source_sha256="9333b972ac797cfef4533d0c338d833470d24f8a1f16a7af806a279026484474",
    test_canonical_sha256="d9c96a919b02b8c58d0eb804166571aa759b73da1acd998ff0b762ca05105f9c",
    failure_tokens=("TypeError", "offset-naive and offset-aware", "1 failed"),
    failure_text=(
        "FAILED tests/test_auth_login.py::AuthLoginTests::"
        "test_password_reset_accepts_a_valid_database_token\n"
        "app/routers/auth.py:160: in reset_password\n"
        "    if not entry or entry.used or entry.expires_at < datetime.now(timezone.utc):\n"
        "E   TypeError: can't compare offset-naive and offset-aware datetimes\n"
        "1 failed"
    ),
    reference_patch=UNRESOLVED / "vimj-password-reset-v1-upstream-reference.patch",
)

CASES: dict[str, Case] = {FEE_BALANCE.case_id: FEE_BALANCE, PASSWORD_RESET.case_id: PASSWORD_RESET}


def get_case(case_id: str) -> Case:
    try:
        return CASES[case_id]
    except KeyError:
        raise ue.EvaluationSafetyError(f"unknown case: {case_id}") from None
