# Unresolved bug evaluation: VIMJ fee balance

This is a separate, one-defect evaluation path. It does not alter the historical
real-bug cohorts, their scoring, or their saved provider results.

## Frozen challenge

- Repository: `Deepak20466/vimj-studio-os-saas`
- Baseline: `6490833d178b71624d024b16046240a20a3f78a6`
- Baseline source: `backend/app/routers/fees.py`
- Source SHA256: `6d131b6b91d079f61af2d78b1e80e54ab9512093951b8caae0ff5a4735d09d28`
- Regression: `backend/tests/test_requested_changes.py::RequestedChanges::test_updating_unpaid_fee_amount_keeps_outstanding_balance_in_sync`
- Test overlay SHA256: `c77b210cef093b15370493e96daf76219f5f16c3f13224a26b955a40e70c61d4`
- Frozen config SHA256: `1498dc35e62128428936bec657178c9bf96047dc30653735a1389e4c1a39b13f`
- Exact patch allowlist: `app/routers/fees.py`

The frozen regression failed on the unmodified baseline with the observed
balance `125.00` versus expected `175`. The baseline source and regression
test hashes matched the freeze. It was run through Selfheal's OS sandbox.

## Isolation and writes

The candidate is copied to a disposable workspace with Git history, secret
files, and evaluator-named material excluded. The model has only the four
existing safe MCP tools. Read and list paths are mapped into the per-attempt
candidate snapshot; traversal, absolute paths, backslashes, and paths outside
that snapshot are rejected before MCP dispatch. Internal snapshot prefixes are
removed from tool responses.

Test execution is restricted to the unique repair worktree and either the
frozen regression test or Selfheal's built-in full-suite check. Patch validation
requires every diff path to equal `app/routers/fees.py`; changes to tests,
configuration, evaluation files, secrets, and every other path are rejected.
No evaluator-only note or expected fix is an input to the runner, prompt, MCP
tools, evidence, or model workspace.

## Offline verification

The baseline preflight completed with the expected failure and zero provider
calls. Evidence is saved in
`benchmarks/real_bugs/results/unresolved/vimj-harness-offline-final-2026-11-09.jsonl`.
The unresolved-context guard tests exercise the actual Selfheal agent loop
using mocked provider and MCP responses. This is harness verification only; it
is not an autonomous repair result.

The existing complete frozen-cohort blinding audit also passed for 41 cases,
with 41 retained and zero dropped:
`benchmarks/real_bugs/results/audits/2026-11-09-unresolved-harness-context-audit.json`.

## Reproduction

Offline baseline and isolation check (no provider request):

```powershell
python -m benchmarks.real_bugs.unresolved_eval --candidate-root "$env:TEMP\vimj-baseline-clean-6490833" --evidence ".\benchmarks\real_bugs\results\unresolved\vimj-offline-next.jsonl"
```

One genuine attempt uses a new evidence file and requires the operator to
authorize the live command only after confirming the configured Groq free-tier
quota, disabled billing, and the freshness of the operator-set quota timestamp:

```powershell
python -m benchmarks.real_bugs.unresolved_eval --candidate-root "$env:TEMP\vimj-baseline-clean-6490833" --evidence ".\benchmarks\real_bugs\results\unresolved\vimj-fee-balance-live.jsonl" --live
```

The runner itself fails closed on missing or stale attestations, unexpected
model configuration, database isolation failure, changed source/test/freeze
hashes, or unavailable OS sandboxing. It makes one attempt and does not retry.
No live provider call has been made as part of this harness work.

## Limitations

The target full backend suite has not been run by the harness yet. The local
Selfheal full test suite could not start because its configured PostgreSQL test
endpoint refused the connection. A symlink-specific test is present but was
skipped on this Windows host because symlink creation was unavailable. GitHub
CI is required to provide the final independent suite result.
