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
- Operator's frozen regression file SHA256 (original Windows bytes):
  `1bd8dcbec1edfa404723df1ecbfc496dc14eabfcbbcd37a814e90733f447a007`
- Canonical regression content SHA256 (line endings normalized):
  `c5dd62175204e73a82cd3741178debf0d71a340080e17187a371d3986242e67a`
- Model-visible freeze file SHA256:
  `9c9f1e6e71ce96925c0c716dd452ccec2e1254241e523632ec4226701b7555f6`
- Harness config SHA256:
  `f586130a223d2ba4df0dd3762c10894327277a344701b90e92030308da8ab1c2`
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
calls. The canonical test hash matches the operator's frozen Windows-byte hash
after line-ending normalization. The operator's model-visible freeze is saved
as `benchmarks/real_bugs/unresolved/vimj-fee-balance-v1-model-visible-freeze.json`.
Evidence aligned with it is saved in
`benchmarks/real_bugs/results/unresolved/vimj-verified-offline-preflight-v2.jsonl`.
Earlier draft preflight records remain preserved but are not the authoritative
preflight evidence.
The unresolved-context guard tests exercise the actual Selfheal agent loop
using mocked provider and MCP responses. This is harness verification only; it
is not an autonomous repair result.

## Saved provider attempts

Two authorized Groq attempts are preserved separately from the frozen
benchmark cohorts. The first failed before any tool call with a `RecursionError`
in Selfheal's scoped MCP tracer wrapper; the wrapper recursively constructed
itself after replacing the runner's global tracer. Commit `0c1f8c3` fixes this
by injecting the original tracer factory, and an offline loop regression test
covers the fix.

The later record
`vimj-fee-balance-live-post-recursion-fix-2026-11-09.jsonl` reached Groq and
received one response (2,718 input and 56 output tokens), then stopped locally
before a second request. The byte-based prompt estimate was 11,354 tokens,
above the 4,889 remaining TPM. No patch was generated, the frozen regression
still failed on baseline, and billed cost is unknown. The current change
compacts oversized `read_file` results only in a copied outgoing request and
marks omitted 1-indexed ranges for retrieval; the original MCP history and
evidence remain unchanged. This behavior has offline adapter coverage but has
not been verified with a live provider call.

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
python -m benchmarks.real_bugs.unresolved_eval --candidate-root "$env:TEMP\vimj-baseline-clean-6490833" --evidence ".\benchmarks\real_bugs\results\unresolved\vimj-fee-balance-live-token-compaction-2026-11-09.jsonl" --live
```

The command above is a template for a later explicitly authorized attempt; it
must not be run without confirming the configured model's free quota, disabled
paid billing, and the freshness of the operator-set quota timestamp. The runner
fails closed on missing or stale attestations, unexpected model configuration,
database isolation failure, changed source/test/freeze hashes, or unavailable
OS sandboxing. It makes one attempt and does not retry. The earlier failed
attempt files remain preserved; use a new evidence filename for every run.

## Limitations

The target full backend suite has not been run by the harness yet. The local
Selfheal full test suite could not start because its configured PostgreSQL test
endpoint refused the connection. A symlink-specific test is present but was
skipped on this Windows host because symlink creation was unavailable. GitHub
CI is required to provide the final independent suite result.
