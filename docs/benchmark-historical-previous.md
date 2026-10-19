# Historical benchmark notes

This file preserves the earlier benchmark report. Its one-scenario Codex
claim predates the current 20-scenario run in [benchmark.md](benchmark.md)
and should not be treated as the current result.

## Independent FastAPI failure fixtures — 2026-10-19

Run with `python scripts/benchmark_fastapi.py validate`. The harness copied
[`benchmarks/fastapi_failure_repo/`](../benchmarks/fastapi_failure_repo/) to a
temporary directory, initialized a local Git repository with no remote, ran
the broken app's regression suite, applied the reference app, then reran the
suite.

| Check | Result |
|---|---:|
| Realistic contract/security failure scenarios | 20 |
| Broken baseline | 20 failed, 0 passed |
| Reference repairs | 20 passed, 0 failed |
| Changed files in reference repair | `app.py` only |
| External GitHub operations | none |

One actual Codex CLI repair was also run in a separate temporary copy for
scenario S03 (missing-resource handling). Codex returned the expected root
cause label, its suggested function was applied to the temporary `app.py`,
the scope check passed, and the targeted regression test passed. The result
is recorded in
[`benchmarks/results/codex-S03-2026-10-19.json`](../benchmarks/results/codex-S03-2026-10-19.json).

| AI metric | Measured value | Sample size |
|---|---:|---:|
| Diagnosis accuracy | 100% | 1 attempted scenario |
| Successful verified fixes | 1 | 1 attempted scenario |
| Average repair time | 34.601 seconds | 1 attempted scenario |
| False fixes | 0 | 1 attempted scenario |
| Manual intervention | 0 | 1 attempted scenario |

These metrics describe only S03. The other 19 scenarios have fixture tests
and reference repairs, but have not had AI repair attempts. Recalculate the
recorded metrics with `python scripts/benchmark_fastapi.py evaluate
benchmarks/results/codex-S03-2026-10-19.json`. The harness deliberately keeps
fixture validation separate from AI success metrics.

## Historical runtime-healer run — 2026-09-26

**0/2 bugs fixed so far. This is an honest, incomplete result:** two bugs ran,
both failed for real healer/repo weaknesses that are fixed now, and the
verification rerun is blocked by a circuit breaker until the 24h window passes.
The other bugs are still pending. It is a single deliberate run, kept separate
from the all-time and AI-attempted success rates on the Metrics page.

Setup: all seeded bugs reset (`scripts/reset_demo_bugs.py --local`), stale
leftover jobs marked failed (history kept), real free-mode AI CLI, real MCP
tools, real git worktrees, no mocking, no bypassed circuit breakers.

| Bug | Result | Attempts | CLI turns | Time to PR | Full suite | Cost | PR |
|---|---|---|---|---|---|---|---|
| `zero` | failed | 3 | 22 / 31 / 21 | - | FAILED (attempts 1 and 3) | about $4.7 | - |
| `key` | failed | 2 | 19 / 30 | - | PASSED (attempt 2) | about $3.1 | - |
| `key` rerun | blocked by the circuit breaker, 0 AI calls | 0 | 0 | - | not reached | $0 | - |
| `validation` | aborted before any attempt | 0 | 0 | - | not reached | $0 | - |
| others | pending / skipped (see below) | | | | | | |

## Root causes (from the logs)

- **`zero`: the AI's fix was correct but rejected by a pinned demo test.**
  The fix (guard the zero-rating division) passed its own regression test, but
  the full-suite gate ran `test_bug1_zero_division_on_unrated_item`, which
  asserted the *broken* behavior (`pytest.raises(ZeroDivisionError)`). The AI
  cannot edit tests outside `apps/target_app/`, so it could not fix that, and
  attempt 2 ran out of turns. Only bug #4's test had been made fix-tolerant.
  Fixed since: every seeded-bug test (bugs 1, 2, 3, 6, 7 and the prober tests)
  now passes whether the bug is present or fixed.
- **`key`: the fix was verified, then `git push` timed out.** Attempt 2 passed
  the regression test and the full suite, but the push hit the 30s git timeout
  and the whole job failed with no PR. Fixed since: push has a 120s timeout
  and one retry (unit-tested).
- **A weakness in my own gate, found before the run:** the first version of the
  full-suite gate ran all of pytest, not the app's configured suite, which
  would have tripped on unrelated environment-dependent tests. It now runs the
  app's `test_command`.

## What is not proven yet

- The `key` rerun that would show the push fix works end to end was refused by
  the per-fingerprint circuit breaker (3 real attempts already used in 24h).
  It was not bypassed. Rerun `python scripts/benchmark.py key` after the window
  resets to complete the verification.
- `validation` was aborted before any attempt (it would have hit the same
  pinned-test problem); `off_by_one` is a contract violation the script cannot
  trigger by name; `none_lookup` already has an open PR (#15); `timezone` is
  already fixed on main; `timeout` depends on network behaviour. None of these
  ran, so no claim is made about them.
