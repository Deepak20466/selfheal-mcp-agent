# Gemini 10-Case Real-Bug Pilot Audit

**Date:** 2026-11-10

**Status:** Offline integration checks passed; live provider evaluation blocked before any request.

## Outcome

No new Gemini attempt was made. The isolated run environment did not provide a Gemini API key, a dedicated benchmark database credential, or current operator attestations that the selected model has free quota and account billing is disabled. No provider request was sent and no cost was incurred. This is **not** a 0/10 benchmark result and does not change the historical pilot score.

The preserved pilot remains five attempt records with zero completed repairs, eight of ten selected IDs unattempted, and the historical failures consisting of two provider timeouts and three function-call protocol rejections. Those saved rows were not edited or resumed.

## Verification

| Check | Result |
|---|---|
| Gemini adapter and benchmark offline tests | **95 passed, 1 skipped** in 37.19s |
| Fresh blinding audit on the frozen pilot | **PASS, 10/10 IDs retained, 0 dropped** |
| Frozen v9 41-case config SHA-256 | `ffb0dd226184027dde73ca031e7c88cd4e51aa2c6f366e7ea2ccbc7063da0e33` |
| Pilot audit config SHA-256 | `dff373e22f36d6e37c7cb56ca06fa0144b5d6bdbea60c84310749a0aae2227c0` |
| Live provider attempts in this audit | **0; blocked before API client construction, database verification, or migration** |

The adapter tests exercise Gemini 3 model Part round-tripping, opaque thought-signature preservation, and exact function-call/response ID mapping with mocked HTTP. Google documents these fields as required when manually managing Gemini 3 function-calling history; see [Gemini function calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling).

## Changes made

- Updated the Gemini adapter documentation to describe the current Gemini 3 function-call ID and thought-signature behavior accurately.
- Reordered benchmark preflight so provider/key/free-quota/billing attestations are checked before database verification and migrations. Client construction makes no network request; the DB is still verified and migrated before any case can call the provider.
- Added a regression test proving missing provider attestation stops before database verification or migration.

## Reproduction

Run the offline checks from the repository root:

```powershell
python -m pytest --noconftest -q tests/test_healer_api_adapters.py tests/test_real_bug_benchmark.py
python -m benchmarks.real_bugs.audit_blinding --config benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v9-pilot-10.json --output docs/evidence/gemini-v9-pilot-leakage-audit-2026-11-10.json --sample-size 10
```

After an operator independently confirms the exact Gemini model has free quota and paid billing is disabled, supplies the API/database credentials outside Git, and sets the documented current attestations, the frozen 10-case command is:

```powershell
python -m benchmarks.real_bugs.run `
  --provider gemini_api `
  --case-list benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v9-pilot-10.json `
  --run-file benchmarks/real_bugs/results/runs/gemini-v9-pilot-10.jsonl `
  --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

That pilot contains ten cases and two arms per case. The run uses the frozen IDs, stops on provider errors or rate limits, preserves attempt rows, and caps provider-reported cost at USD 0. This report makes no claim about a live Gemini response, quota, or repair result.
