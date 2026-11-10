# OpenRouter v9 frozen 10-case pilot: partial results

**Run date:** 2026-11-10 UTC

**Status:** Stopped after three arm attempts; the frozen 10-case evaluation is incomplete.

## Observed results

The run used the unchanged frozen IDs in
[`2026-11-09-gemini-3.8-flash-v9-pilot-10.json`](../../benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v9-pilot-10.json)
with `cohere/north-mini-code:free` through the OpenRouter adapter. The authenticated Free-tier
key, exact model pricing, and tool-capable endpoint were reverified immediately before resuming.
The adapter sends the exact model ID and disables fallback routing.

| Measure | Observed |
|---|---:|
| Frozen cases selected | 10 |
| Arm attempts recorded | 3 |
| Unique cases attempted | 2 of 10 |
| Completed repairs (target tests and full suite green) | 0 |
| Repair resolve rate among attempts | 0/3 (0%) |
| Target-test passes | 0/3 |
| Full-suite passes | 0/3 |
| OpenRouter completion requests | 26 |
| Reported total cost | $0.00 |
| Reported tokens | 102,913 input + 5,727 output |

The per-arm scorer reports 0/2 resolves for `with_validation` and 0/1 for `without_validation`.
These are three attempted arms, not a 10-case success rate. Eight frozen case IDs were not
attempted. The scorer's false-positive measure is undefined because no target test passed.

## Attempt details

- `attrs-73dc0d2e2860`, `with_validation`: 10 requests; stopped at the old per-arm request cap
  before repair verification. The saved trace includes malformed `read_file` arguments from the
  model.
- `attrs-73dc0d2e2860`, `without_validation`: 7 requests; the proposed patch did not apply to
  `src/attr/_config.py`, so no repair was verified.
- `attrs-2772a29b7bf2`, `with_validation`: 9 requests; stopped when the case's 40,000-token limit
  was exceeded, before repair verification.

The runner stops after provider errors and does not try another model or paid route. The later
attempts show the request-cap fix let the first resumed arm proceed beyond 10 calls, but this
free-model run still did not complete a repair and did not reach the other eight cases.
These outcomes reflect this model run only; they do not change the historical Gemini or VIMJ
results.

## Environment and reproducibility

The database preflight confirmed role `selfheal` on only
`127.0.0.1:5434/selfheal_real_bug_test` (PostgreSQL 16). The `public` schema owner and Alembic
table owner were `selfheal`; schema USAGE/CREATE, Alembic SELECT/INSERT/UPDATE/DELETE, and existing
table/sequence ownership checks passed. The benchmark completed its migration preflight before
running cases.

Offline benchmark and adapter verification passed **127 tests, 2 skipped**. The frozen cases,
case tests, historical result rows, and Gemini artifacts were not changed. The complete raw
attempts and scorer outputs are published beside this report:

- [Raw JSONL attempts](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-pilot-10-20261110/attempts.jsonl)
- [Scorer JSON](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-pilot-10-20261110/2026-11-10.json)
- [Scorer Markdown](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-pilot-10-20261110/2026-11-10.md)
- [Historical preflight record](openrouter-v9-pilot-preflight-2026-11-10.md)

OpenRouter metadata checks confirmed the model and endpoint were listed at zero prompt,
completion, and endpoint prices before generation. Every recorded completion reported exactly
zero cost. OpenRouter documents its [free model collection](https://openrouter.ai/collections/free-models)
and the model's current [free endpoint](https://openrouter.ai/cohere/north-mini-code:free).
