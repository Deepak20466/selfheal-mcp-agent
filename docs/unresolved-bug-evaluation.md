# Unresolved bug evaluation: VIMJ fee balance

This is a separate, one-defect evaluation path. Its current route is separately
versioned for Gemini 2.5 Flash. It does not alter the historical real-bug
cohorts, the frozen VIMJ challenge, or the saved Cohere/OpenRouter and Groq
provider results.

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
using mocked provider and MCP responses. The Gemini workflow test also exercises
the existing `GeminiApiClient`, a mocked tool-call conversation, exact-path
patch rejection, the permitted source patch, regression verification, and the
full-suite tool path. This is offline harness verification only; it is not an
autonomous repair result.

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

## Current Gemini 2.5 Flash route

The current files are `vimj-gemini-2.5-flash-budget-v1.json` and
`vimj-gemini-2.5-flash-context-v1.json`. Their hashes are checked by the
evaluator, and they preserve the original baseline, regression, exact source
allowlist, 32,768 aggregate-token cap, 10-request cap, 600-second timeout, and
$0 cost cap. The previous Cohere/OpenRouter budget-v2 and context-v4 files and
their evidence remain unchanged as historical records.

The evaluator selects `gemini_api` and requires the exact model ID
`gemini-2.5-flash`. Before a live attempt, independently verify the Google AI
Studio project's free quota and that billing is disabled. The provider does
not return a billed-cost field in this adapter, so the $0 requirement depends
on the verified free-only account and fail-closed operator attestations. No
automatic provider fallback is used.

PostgreSQL diagnosis: localhost:5432 has no listener, while the local PostgreSQL
16 service listens on 127.0.0.1:5434. The repository's app/test defaults point
to 5432; do not use those defaults for this VIMJ evaluation. It requires the
separate `selfheal_real_bug_test` database on 5434, which is identity-checked
before migrations. Full Selfheal backend tests use a newly created disposable
PostgreSQL cluster via `python scripts/test_with_ephemeral_postgres.py`; this
does not connect to the 5434 service or a production database.

The command below is for a later, separately authorized run only. Supply the
candidate checkout and private database/API credentials through the environment;
never commit them. Set `REAL_BUG_DATABASE_URL` to the isolated PostgreSQL 16
target at `127.0.0.1:5434/selfheal_real_bug_test` and verify it first. The live
evaluator also requires fresh model quota, free-tier, and disabled-billing
attestations. Without all checks it stops before constructing the Gemini client.

```powershell
$env:GEMINI_API_MODEL = "gemini-2.5-flash"
python -m benchmarks.real_bugs.unresolved_eval `
  --candidate-root "$env:TEMP\vimj-baseline-clean-6490833" `
  --evidence ".\benchmarks\real_bugs\results\unresolved\vimj-gemini-2.5-flash-v1.jsonl" `
  --live
```

`--live` must only be used after separate authorization. It makes at most one
Selfheal attempt and does not resume a previous evidence file.

## Offline preflight and historical Cohere/OpenRouter attempts

Offline baseline and isolation check (no provider request):

```powershell
python -m benchmarks.real_bugs.unresolved_eval --candidate-root "$env:TEMP\vimj-baseline-clean-6490833" --evidence ".\benchmarks\real_bugs\results\unresolved\vimj-offline-next.jsonl"
```

Historical Groq attempts above remain unchanged. The following Cohere/OpenRouter
attempts predate the active Gemini route and remain preserved as historical
evidence. That route was pinned to `cohere/north-mini-code:free`. OpenRouter lists zero input
and output pricing, tool calling, a 256,000-token context, and up to
65,536 completion tokens for this model. Its `:free` tier is limited to 20
requests per minute and 50 requests per day. The previous attempt used an
8,000-token per-case cap and stopped as designed after three successful
responses totaled 7,985 input plus 423 output tokens (8,408 total). The meter
sums input and output usage from each completion, including prompt tokens
repeated in follow-up calls. The stop is expected, not a usage-counting defect;
the failed result and all prior evidence remain unchanged.

The v2 attempt then made nine successful completions, but its trace shows six
failed `read_file` calls, two `list_files` calls, and no `propose_patch` or
`run_tests` call. The captured error path already included the snapshot prefix;
the scope wrapper prepended that prefix again. Subsequent model responses
repeated and compounded the internal path from prior tool errors. This loop
spent 35,140 input tokens and 661 output tokens without reaching repair.

The v3 live attempt is preserved in
`results/unresolved/vimj-v3-20261109-110601-7861741.jsonl`: one HTTP 200,
2,374 input tokens, 416 output tokens, no patch, and a rejected `run_tests`
request. Its saved MCP trace contains only the initial `get_error`; the scope
guard raised before the tracer recorded the rejected arguments. The exact
submitted `test_path` therefore cannot be recovered from this evidence. The
guard proves it was not `None` or the complete frozen selector
`tests/test_requested_changes.py::RequestedChanges::test_updating_unpaid_fee_amount_keeps_outstanding_balance_in_sync`.

The historical OpenRouter v4 run used a separately pinned context policy in
`vimj-cohere-north-mini-context-v4.json`, evaluation ID
`vimj-cohere-north-mini-context-v4-2026-11-09`, and the unchanged budget-v2
config. The prompt gives the exact approved `run_tests.test_path`; omitting
`test_path` selects the approved full-suite command. A rejected test request
now returns an MCP tool error for the model to correct, and records the
sanitized attempted `test_path` in the trace without executing it. The budget
remains 32,768 aggregate tokens, at most 10 provider
requests, $0 reported cost, and 600 seconds. The context policy presents the
source as the exact repo-relative `app/routers/fees.py` path, strips one exact
snapshot prefix if it is echoed, and returns a short correction for failed
reads without exposing the internal workspace path. The v4 OpenRouter adapter sent a
bounded copy of previous `read_file`, `list_files`, and `run_tests` results;
source omissions are marked with line ranges that remain retrievable. Tool
history used for local evidence is not rewritten. The prompt requires the
fail-before-patch, patch, regression-pass sequence; the harness still runs the
full suite after that proof. The challenge freeze, test and source hashes,
source write allowlist, zero-cost check, no-fallback rule, request limit, and
sandbox remain unchanged. The context-policy version is distinct so results
can be compared without treating the unproductive v2 trace as equivalent.

The old OpenRouter invocation and its account attestations are retired. The
active evaluator accepts only the separately versioned Gemini configuration
and uses the command and fresh free-only checks in the current route section
above. The historical OpenRouter files and attempt evidence remain unchanged.

## Limitations

No Gemini request has been made. The frozen baseline was verified to fail and
the deterministic mocked Gemini workflow passed, but a genuine model-generated
patch and post-patch frozen regression result do not exist. The live gate is
blocked until the operator separately authorizes one attempt, verifies the
Google AI Studio free quota and disabled billing, provides the fresh attestation
and API key, and supplies valid credentials for the isolated
`selfheal_real_bug_test` database on port 5434. The read-only connection check
against the app's `selfheal_test` credentials on 5434 returned an invalid
password; no database was modified. Port 5432 has no listener.

The complete Selfheal backend suite was run against a newly created disposable
PostgreSQL 16.2 cluster on a random loopback port using
`python scripts/test_with_ephemeral_postgres.py`: 922 passed, 12 skipped. The
symlink-specific skips are platform-dependent on this Windows host. This suite
result does not substitute for the VIMJ frozen regression after a genuine
repair. Offline run details and the honest NO-GO statuses are recorded in
`results/unresolved/vimj-gemini-offline-readiness-2026-11-09.json`.
