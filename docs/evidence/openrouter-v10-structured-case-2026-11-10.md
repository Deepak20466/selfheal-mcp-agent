# OpenRouter structured repair case

**Date:** 2026-11-10 UTC
**Outcome:** Failed; no patch was applied and repair is not claimed.

## Controlled run

Exactly one `with_validation` attempt ran frozen case `attrs-73dc0d2e2860` using the verified
zero-priced `cohere/north-mini-code:free` model. The adapter had provider fallback disabled and
enforced a reported cost of exactly zero for every completion. Only
`127.0.0.1:5434/selfheal_real_bug_test` was used; its PostgreSQL 16 identity and Alembic
permissions were checked before the attempt.

| Measure | Observed |
|---|---:|
| Repair verified | No |
| Patch submissions | 1 |
| Patch applied | No |
| Frozen regression passed | No |
| Full suite run after repair | No; regression remained failing |
| Provider completion requests | 2 |
| Input tokens | 4,555 |
| Output tokens | 1,528 |
| Total tokens | 6,083 |
| Provider-reported cost | $0.00 |

The model attempted a 495-byte diff (six changed lines). Patch validation rejected it with
`Diff does not touch any files`; no file paths could be parsed, so it was never applied. The trace
also records a blocked `read_file` request despite complete supplied context. The provider's next
completion did not submit a correction. The bounded outer correction was then stopped by the
two-request daily headroom guard. The actual error and failing regression output were retained in
the attempt record; the full suite was correctly not run while the target test failed.

## Workflow and offline verification

The benchmark now follows DIAGNOSE, PATCH, APPLY, VERIFY, and RETRY phases. It can suppress
supplemental file inspection when the first message includes the frozen test, baseline failure,
and imported source. Inspection paths and patch paths are repository-relative; patches must parse
as raw unified diffs and may change only source files selected for the frozen case. Existing MCP
sandbox and patch-policy checks still perform safe application in the isolated checkout. The
runner independently re-runs the frozen regression and then the full suite after a passing
regression. It passes actual apply or verification output into at most one correction, requires
that correction to follow the previous tool-result turn, and enforces a two-patch maximum.

Offline verification passed **138 tests, 2 skipped** across runtime recovery, real-bug benchmark,
provider-adapter, and OpenRouter tests using `pytest --noconftest`. Ruff and `git diff --check`
passed. The frozen case file SHA256 remains
`DFF373E22F36D6E37C7CB56CA06FA0144B5D6BDBEA60C84310749A0AAE2227C0`.

The old Gemini and OpenRouter pilot records and earlier controlled attempts remain unchanged.

- [Raw attempt and tool trace](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v10-structured-attrs1-20261110/attempts.jsonl)
- [Scorer JSON](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v10-structured-attrs1-20261110/scorer.json)
- [Scorer Markdown](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v10-structured-attrs1-20261110/2026-11-10.md)
- [Prior primed controlled case](openrouter-v9-primed-case-2026-11-10.md)
