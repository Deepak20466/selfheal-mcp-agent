# VIMJ coach deletion repair case (2026-11-17)

**Scope:** one real defect in the VIMJ Studio OS backend, fixed and merged in the VIMJ repository, and
checked in an isolated sandbox. This page does not claim a production deployment, a production database
check, or an autonomously generated repair.

## How the repair was produced (read this first)

| Stage | Who or what | Evidence |
|---|---|---|
| Defect identified | Operator and an interactive Claude Code session reading the VIMJ source | Read-only inspection of models and routes |
| Expected behaviour | Set by the repository owner | Approved in the session before coding |
| Regression tests written | Assisted, in the same interactive session | Test file in PR #6 |
| Repair code written | **Assisted, by hand, in the interactive session. Not a provider-driven autonomous repair.** | Commits `41a45065`, `5c070f63` and `409edf57` on the VIMJ branch; PR #6 |
| Review and merge | Repository owner | PR #6 merged |
| Verification | Scripted runs inside Bubblewrap | Evidence below |

No provider API generated this repair, and no autonomous agent ran against this case.

## Problem

`DELETE /coaches/{id}` deleted the coach with a plain database delete. The database then cascaded
(`ON DELETE CASCADE`) to every row that referenced the coach:

- class sessions, and the student attendance recorded under those sessions;
- facility (coach) attendance;
- salary rows;
- fee receipts;
- fee reminder drafts;
- leave requests;
- activity assignments.

One admin action could therefore erase historical student attendance and financial records. The
cascade was confirmed from the SQLAlchemy model definitions. Its runtime behaviour was tested on SQLite
with foreign keys enabled, which mirrors the same cascade and `SET NULL` actions. Production uses
PostgreSQL, so the behaviour there was not tested directly.

## Repair

- If the coach has any of the records above, the endpoint now returns **409 Conflict** with a message
  telling the administrator to deactivate the coach instead. Deactivation uses the existing
  `PUT /coaches/{id}` with `is_active=false`.
- A coach with no such records can still be deleted.
- The change is confined to `backend/app/routers/coaches.py` (about 30 lines, CRLF endings preserved)
  and the new test file `backend/tests/test_coach_delete_guard.py` (10 tests).

## Merged change

- **Repository:** `Deepak20466/vimj-studio-os-saas`
- **Pull request:** #6, `fix/coach-delete-data-loss` into `master`, merged 2026-11-17 at 14:38:54Z
- **Merged commit:** `acf11b6edd3c6fc7c64cc6c6ad0e9d36641a2163`

## Verification

**Targeted tests (post-merge, merged tree):** 10 of 10 pass. Eight check a 409 for each blocked record
type. One checks that a coach with no records can still be deleted. One checks that deactivation keeps
every record.

**Whole backend suite (post-merge, merged tree):** 51 passed, 0 failed. This is the count recorded in
the evidence file below, not a figure from a separate run.

**Sandbox:** Bubblewrap, run as the `benchmark` user with the VIMJ pinned requirements and SQLite with
foreign keys enabled. The repository checkout was unchanged during the run. The run exited with code 0.

**Pre-merge baseline:** on the unrepaired master with the same tests, the eight blocked-record tests
fail (the endpoint returns 204 where 409 is expected), the two unblocked tests pass, and the whole
backend directory shows exactly those eight failures (8 failed, 43 passed).

### Evidence files

| File | Where it is kept | SHA-256 |
|---|---|---|
| `vimj-coach-deletion-post-merge-2026-11-17.jsonl` (post-merge run, merged `acf11b6e`) | this repository, `docs/evidence/` | `3b116a1dcb0fe5597f0c0c9ce8057893d88febd43e3eb58122df4469ef562a9f` |
| `coach-delete-baseline-20261117T142934Z.jsonl` (pre-merge baseline) | `benchmarks/real_bugs/results/unresolved/` on main (PR #61, merge `47c6c73`) | `0d16905ea876fcde68a66ea071b73c60bdb495c28c74166bb3ed34d32597fed2` |
| `coach-delete-repaired-20261117T142934Z.jsonl` (pre-merge repaired branch) | `benchmarks/real_bugs/results/unresolved/` on main (PR #61, merge `47c6c73`) | `353052c318ffc15ba15f5cd54f8b8b4196336a37136f5c8ea16d599016a83b3f` |
| `coach-delete-baseline-20261117T141631Z.jsonl` (earlier baseline, kept as history) | `benchmarks/real_bugs/results/unresolved/` on main (PR #61, merge `47c6c73`) | `0238b3d863732ef2fa0788f26aa03f2c306da8f635280b67553e95ac6402287d` |

The post-merge evidence file records integrity for each line and confirms `provider_calls: 0` and
`live: false`. It contains no credentials, personal email addresses or account metadata. It does
contain the benchmark account's home-directory path.

## Limitations

- Tests used SQLite with foreign keys on. The PostgreSQL production database was not tested or accessed.
- Production was not deployed or checked against production data.
- The repository has no CI on pull requests, so no CI result exists for PR #6.
- The fix blocks deletion. It does not add a soft-delete model or a migration.
- Candidate batch coach reassignment (existing future sessions keep the old coach) is not addressed.
- The benchmark runner had a comparison bug in its baseline whole-directory check. The bug was fixed
  and the existing baseline evidence was re-checked offline with the corrected logic. Nothing was
  overwritten.

## What this does not show

- It does not show that any AI provider generated this repair.
- It does not show a live autonomous repair.
- It does not establish reliability from a sample.
