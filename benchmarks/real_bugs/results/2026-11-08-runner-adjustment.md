# Runner adjustment record

Before any benchmark case was attempted, inspection found that
`scripts/run_real_bug_benchmark.ps1` generated and later removed
`GEMINI_QUOTA_CHECKED_UTC` itself. This conflicted with the operator requirement
that only the user set or refresh that timestamp. The runner now validates the
provided UTC timestamp and its 15-minute age before each invocation, without
changing or deleting it.

No benchmark case ran under the pre-adjustment runner. The frozen config,
prompts, validation rules, timeout, case list, and run plan were not changed.

## Test isolation diagnosis

The first full local test run reported 13 failures: three Gemini adapter tests,
eight OpenRouter repair tests, one Gemini repair-provider test, and one scanner
test.

- The three adapter tests registered `gemini-2.0-flash` HTTP mocks, while the
  ambient `GEMINI_API_MODEL=gemini-3.8-flash` selected a different URL. The
  unmatched requests reached Google's Gemini endpoint using the fake key that
  `tests/conftest.py` installs, then returned HTTP 400. No user's API key was
  sent.
- The eight OpenRouter tests disabled the older Gemini 2.5 quota flags but did
  not disable the newer Gemini 3.8 and 3.5 flags. Ambient quota eligibility
  selected Gemini ahead of the mocked OpenRouter provider; catalog calls
  returned HTTP 400 and masked the expected OpenRouter paths.
- The Gemini repair-provider test explicitly disabled the 2.5 quotas but
  inherited `GEMINI_38_FLASH_FREE_QUOTA=true`. It therefore queried the mocked
  catalog instead of stopping before HTTP, producing an unexpected-shape
  error.
- The one scanner failure was not reproducible in the selected-node reruns or
  either full-suite rerun. Its exact transient cause was not established, so no
  assertion or scanner code was changed.

`tests/conftest.py` now removes ambient `GEMINI_*`, `REAL_BUG_*`, and
`I_CONFIRM_GEMINI_FREE_TIER_AND_NO_BILLING` variables before application
settings load, pins the adapter test model, and disables live Gemini, Groq, and
OpenRouter eligibility. The OpenRouter test fixture also explicitly disables
the newer Gemini model quota flags. Assertions remain unchanged.

The first post-fix selected-node run passed **13/13** in the normal shell; the
sanitized child process also passed **13/13**. The full suite passed in both
environments: **845 passed, 11 skipped, 4 warnings**. Ruff check, Ruff format
check, and scoped mypy passed.

For comparison, main's successful CI run was [run 42411324243](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/42411324243)
at `5088c44f11196c7970519101d296fa713f2f0bb9`; its CI test job, Ruff,
formatting, and scoped mypy steps all succeeded.

## Frozen source update

The v3 config hashes both `scripts/run_real_bug_benchmark.ps1` and
`benchmarks/real_bugs/run.py`. Because the runner changed, v4 was frozen before
any benchmark case ran. V3 remains intact as history. V4 preserves the same 41
case IDs and benchmark prompts, validation rules, timeouts, and arms; it
updates the runner source hash and records hashes for the changed test harness
files. The v4 leakage audit passed all 41 cases with zero drops. V4 config
SHA-256: `ed15776e865da22181d3900b85eec8a582258c526de9d0b9d7cd012dc2f03f06`.

No benchmark case or benchmark result exists yet.
