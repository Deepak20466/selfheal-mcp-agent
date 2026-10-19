# AI repair benchmark

> **Historical result, not current production evidence:** this benchmark ran
> with Codex CLI before the provider CLI fail-closed gate was merged in PR #21.
> Provider CLI benchmark execution is currently blocked until Linux OS
> containment is implemented and independently verified. The 20/20 result
> below remains valid for the recorded fixture run; it does not establish an
> isolated live repair workflow or arbitrary-repository reliability.

| Run detail | Value |
|---|---|
| Date | 2026-10-19 |
| Final artifact | [codex-full-2026-10-19-run5.json](../benchmarks/results/codex-full-2026-10-19-run5.json) |
| Backend | Codex CLI 0.162.0-alpha.17.2, authenticated with the existing ChatGPT login |
| Target | Disposable copy of the independent FastAPI failure repository |
| Final benchmark result | 20/20 scenarios passed; exit code 0 |

## Fixture validation

The harness used a temporary local Git repository with no remote. Its broken
baseline failed all 20 scenario tests. Applying the separate reference repair
changed only `app.py`; all 20 tests then passed. This validates the fixture and
reference solution, not the AI result.

## Live Codex results

Each scenario was sent separately to the live Codex CLI. The model saw the
broken `app.py`, one selected test, and that test's captured failure. It did not
see or apply the reference repair. A proposed patch had to modify only
`app.py`, pass strict diff and path validation, and pass the selected test in
the OS sandbox. A failed first generation could receive one regression retry;
the retry saw the failing test output and prior patch as untrusted data.

| Measure | Final result |
|---|---:|
| Scenarios attempted / skipped | 20 / 0 |
| Diagnosis labels correct | 20/20 (100%) |
| Verified repairs | 20/20 (100%) |
| Improvement from previous 8/20 result | +12 repairs; 40% to 100% (+60 percentage points) |
| Terminal false fixes | 0 |
| Total model generations | 21 (S20 used one retry) |
| Mean repair and verification time | 15.8649 seconds |
| Input tokens reported by CLI | 336,069 |
| Cached input tokens reported by CLI | 12,544 |
| Output tokens reported by CLI | 6,090 |
| Cost reported by CLI | No; cost is unknown, not zero |
| Manual interventions | 0 |

S20's first generation did not pass its response-model regression test. Its
single retry changed the readiness endpoint's return annotations and both
branches to `JSONResponse`; the same test then passed. The final result counts
one verified outcome per scenario while the token and generation counts include
both S20 attempts. No scenario, fixture, or test criterion was changed to
produce the result.

| Scenario | Diagnosis label | Final result |
|---|---|---|
| S01 | request_validation | PASS |
| S02 | pagination_offset | PASS |
| S03 | missing_resource_handling | PASS |
| S04 | empty_collection_guard | PASS |
| S05 | timezone_awareness | PASS |
| S06 | decimal_money_arithmetic | PASS |
| S07 | partial_update_semantics | PASS |
| S08 | http_status_contract | PASS |
| S09 | duplicate_request_handling | PASS |
| S10 | missing_authorization_check | PASS |
| S11 | mass_assignment | PASS |
| S12 | unbounded_pagination | PASS |
| S13 | exception_detail_leak | PASS |
| S14 | path_traversal | PASS |
| S15 | delete_status_contract | PASS |
| S16 | inventory_invariant | PASS |
| S17 | result_ordering | PASS |
| S18 | idempotency | PASS |
| S19 | empty_query_validation | PASS |
| S20 | readiness_check | PASS after one retry |

The raw report contains per-generation model output, diffs, validation gates,
test output, duration, and reported usage. Provider output is scrubbed before
it is saved. The evaluator derives the figures above from this single complete
run; it does not combine partial runs. The initial run needed harness recovery
after an interrupted report write; the saved `recovery_errors` field preserves
that operational history.

## Reliability scope

These 20 scenarios are 20 seeded failures in **one synthetic FastAPI fixture
repository**. They are not 20 independent repositories, and they do not meet
the separate production-readiness goal of evaluating three independent
repositories with different failure types. No fresh multi-repository
reliability run was performed for this audit. The 20/20 result is limited to
the fixed fixture suite and must not be used as an arbitrary-repository
success-rate estimate.

## Reproduce

Run the fixture checks:

```powershell
python scripts/benchmark_fastapi.py validate
```

Run a fresh genuine Codex benchmark (requires an authenticated Codex CLI):

```powershell
python scripts/run_ai_benchmark.py --output benchmarks/results/my-run.json --timeout 240
```

Evaluate a saved run:

```powershell
python scripts/benchmark_fastapi.py evaluate benchmarks/results/my-run.json
```

The harness uses Windows AppContainer plus a Job Object or Linux Bubblewrap and
fails closed when sandbox setup is unavailable. The recorded run used
AppContainer on Windows. Linux Bubblewrap remains unverified on a non-root
Linux worker. See [architecture.md](architecture.md) for isolation limits.

## Historical runtime-healer report

The earlier 2026-09-26 live runtime-healer experiment is preserved in
[benchmark-historical-previous.md](benchmark-historical-previous.md). Its
results predate this independent fixture benchmark and should not be combined
with the Codex measurements above.
