# Real historical bug benchmark

## Status

This benchmark mines historical source-and-test fixes from the pinned repositories in
[`benchmarks/real_bugs/repos.yaml`](../benchmarks/real_bugs/repos.yaml). Mining and preparation
produce eligible cases; they are not model repair attempts. Only JSONL records produced by
`benchmarks.real_bugs.run` count as provider results. The five preserved v5/v7/v8 attempt records
and their scorer output are committed under `benchmarks/real_bugs/results/published/`; full local
run files remain in the ignored `results/runs/` directory.

**Status: partial pilot evidence exists; the 10-case pilot is incomplete.** The original selection
is unchanged. Five records are preserved across v5, v7, and v8. The v5/v7 records are the same
case/arm ending in request timeouts. The three v8 records cover two selected case IDs and both
arms; all ended in Gemini HTTP 400 `INVALID_ARGUMENT` because function-call history omitted the
model's `thoughtSignature`, including a `run_tests` call. No repair completed; eight selected IDs
remain unattempted. These outcomes and all raw records are saved under
[`results/published/`](../benchmarks/real_bugs/results/published/) and the ignored local source run
file is preserved unchanged. The v9 adapter fix and both leakage audits are committed separately
from these historical outcomes. Do not resume into an existing result file.

A separate VIMJ fee-balance repair used one genuine AI-generated code change against frozen
repository commit `6490833d178b71624d024b16046240a20a3f78a6`. The response needed patch metadata
normalization; saved local logs report 1 frozen regression and 30 backend tests passed. It did not
create a VIMJ commit or pull request and is not a result from this benchmark runner. See the
[VIMJ evidence report](evidence/vimj-fee-balance-2026-11-10.md).

**Diagnostic-request disclosure:** before the test environment was made hermetic, one targeted
test rerun sent unmatched diagnostic requests to Gemini's endpoint using the deliberately fake
test key installed by `tests/conftest.py`; Gemini returned HTTP 400. The recorded adjustment note
states that no user's API key was sent. These were test-mock failures, not benchmark repair
attempts, and they produced no benchmark outcomes. See
[`2026-11-08-runner-adjustment.md`](../benchmarks/real_bugs/results/2026-11-08-runner-adjustment.md).

## Separate 10-case pilot

This pilot is a separately labeled exploratory slice, not a replacement or partial score for the
41-case benchmark. Its original deterministic IDs are preserved in the v9 pilot file
[`2026-11-09-gemini-3.8-flash-v9-pilot-10.json`](../benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v9-pilot-10.json).
The rule groups case IDs by repository family, ranks each family by ascending SHA-256 of the UTF-8
string `selfheal-real-bug-pilot-v1:` followed by the case ID (case ID breaks ties), and takes two
cases from each of the five families. This gives ten selected cases while spanning all five
repositories. The frozen v4 config and its hash are unchanged.

| Pilot status | Measured value |
|---|---:|
| Selected | 10 |
| Attempt records through v8 | 5 (2 timeout records in v5/v7; 3 HTTP 400 records in v8) |
| Unique selected cases attempted | 2 of 10 |
| Completed repairs | 0 |
| Resolve rate | 0/5 (0%) |
| Resolve rate with full suite green | 0/5 (0%); no full suite passed |
| False-positive proxy versus human fix | Not measurable: 0 eligible denominator |
| Median elapsed time | 121.094s across the five saved attempt records |
| Provider tokens | 6,989 reported across the five records |
| Cost | Unknown in all five records; no billed cost was reported |
| Failure categories | `provider_error`: 2; `repair_error`: 3 |
| Not attempted | 8 of 10 pilot IDs; 41-case cohort |

These are saved attempts, not extrapolations. No repair resolved and no full suite passed. The
false-positive proxy is undefined because its denominator is zero. The scorer summary is
[`pilot-through-v8.json`](../benchmarks/real_bugs/results/published/pilot-through-v8.json), its
readable table is [`pilot-through-v8.md`](../benchmarks/real_bugs/results/published/pilot-through-v8.md),
and the preserved five-record source is
[`gemini-v5-v8-attempts.jsonl`](../benchmarks/real_bugs/results/published/gemini-v5-v8-attempts.jsonl).
The original v8 three-record run file remains unmodified at
`benchmarks/real_bugs/results/runs/gemini-v8-pilot-10.jsonl` in the local results directory.

The selected pilot contexts passed independent leakage audits with zero dropped IDs in v4, v7,
v8, and v9. The v9 full cohort audit passed all 41 cases; its pilot audit passed all 10. The v9
freeze preserves the original IDs and rules while recording that it postdates prior attempts. See
the v9 audit files alongside the config.
The 20/20 synthetic fixture benchmark is historical evidence only and is not part of this pilot
or the pending 41-case real-bug benchmark.

## Separate GroqCloud diagnostics and v3 configuration

The Groq v1 diagnostic has one preserved attempt: an HTTP 429 TPM limit of 8,000, with 1,366 used,
7,843 requested, and a provider-suggested 9.0675-second wait. It failed before any repair; no target
or full suite passed. The saved run records 1,497 input and 75 output tokens from earlier successful
turns, and its tool trace includes `get_error` and `read_file`. The failed request body was not
saved, so the input-history/output-reservation share of the 7,843 cannot be separated from that
record. V2 capped the output reservation at 4,096 but did not budget it against conversation growth
or the remaining TPM window.

The saved v2 diagnostic has two failed arm attempts on `attrs-73dc0d2e2860`. The validation arm
hit the 8,000 TPM limit with 5,630 used and 7,689 requested; Groq suggested 39.8925 seconds. Before
failure the saved aggregate usage was 8,909 input and 151 output tokens, and tool names were
`get_error`, `read_file`, and `run_tests`. The no-validation arm got HTTP 400, “Please reduce the
length of the messages or completion,” after `get_error` and `list_files`; prior aggregate usage was
1,503 input and 35 output tokens. Neither arm repaired the case. V2 records do not save per-tool
output sizes or rejected request bodies, so the exact prompt/completion shares and the length that
triggered HTTP 400 cannot be established offline. Both traces show multi-turn context growth before
failure.

Groq uses the existing `groq_api` adapter, not Gemini output or fallback routing. Its separately
frozen v3 configuration preserves the original 41-case cohort, deterministic ten pilot IDs,
prompts, arms, caps, timeouts, and scoring rules. The freeze hash is
`9f81fe6a0dc75db874d7f6246ce51039330e42a9e194d8da75761e6c353a5e29`. V3 budgets a local prompt
token estimate plus completion against the 8,000 TPM cap and the documented 131,072-token context
window. The estimate is serialized UTF-8 JSON bytes divided by two plus framing overhead; it is a
planning heuristic, not exact tokenizer telemetry or a formal upper bound. V3 uses
server-reported remaining TPM where available, visibly compacts rediscoverable `list_files`
results and non-diagnostic `run_tests` lines, retains full `read_file` content, and fails locally
when essential context cannot fit. A temporary TPM error is retried once at the exact server delay
only if it is at most 60 seconds and fits the remaining case deadline; daily quota is never retried.
Gemini v4-v9, Groq v1/v2 freezes, and prior attempts remain unchanged. V3 leakage audits pass
41/41, 10/10, and 1/1.

Groq's official model and tool-use documentation lists `openai/gpt-oss-120b` and local function
tool calling, but currently lists no parallel tool use for this model. The adapter's offline tests
cover ordered call IDs/results in a multi-call turn and a later sequential `run_tests` call. This
does not establish free quota for the operator's account. Codex made no live Groq calls.

| Groq evidence | Status |
|---|---|
| Frozen cohort / pilot selection | 41 / original 10 IDs |
| Groq v1 diagnostic | 1 attempt; `attrs-73dc0d2e2860`; HTTP 429 TPM |
| Groq v1 completed repairs | 0; target/full suite did not pass |
| Groq v2 diagnostic | 2 arm attempts; TPM 429 and HTTP 400; 0 repairs |
| Groq v3 diagnostic attempts / completed repairs | 1 / 0 |
| Groq v3 successful API responses before stop | 1; next request was rejected locally before sending |
| Groq v3 target-test pass | 0/1 |
| Groq v3 full-suite-green resolve | 0/1 |
| Groq v3 elapsed time | 2.438s |
| Groq v3 reported tokens | 1,505 input + 89 output = 1,594 |
| Groq v3 reported cost | Unknown; provider returned no billed-cost value |
| Groq v3 failure | Prompt estimate 6,818 exceeded 5,936 remaining TPM after a successful turn; no repair completed |
| Groq v4 diagnostic / official pilot attempts | 0 / 0 |
| Account free quota and billing state | Operator verification required |

The one v3 diagnostic attempt is preserved at
[`groq-v3-diagnostic-1.jsonl`](../benchmarks/real_bugs/results/published/groq-v3-diagnostic-1.jsonl).
Its scorer label is `provider_error`; the detailed record shows a local `GroqRequestBudgetError`
after one successful response. The conversation included the initial repair prompt, the model's
tool-call history, and `get_error`/`read_file` context. The saved artifact does not retain per-message
sizes, so it cannot establish the precise contribution of each item.

V4 preserves the 41 cases and original 10 pilot IDs, with unchanged prompts and scoring. It adds a
single wait based only on the server token-reset header when the complete prompt and minimum output
fit a fresh 8,000-TPM window and the request deadline allows it; histories that cannot fit a fresh
window still fail locally. Full, pilot, and diagnostic leakage audits pass. V4 has not been run.
Its hash is `f63154b3b6b9fa110a450ff69a524753f335652f6c6c80eb5824c948ce7b8335`; v1-v3 configs and
all prior result rows remain unchanged.

The fresh v4 diagnostic command and separate VIMJ safe testing workflow are documented in
[`groqcloud-repair.md`](groqcloud-repair.md). Its JSONL path is separate from Gemini results and
from the official ten-case Groq pilot. Run it only after verifying free quota and disabled paid
billing; retain failures and timeouts as recorded.

### Frozen setup history

v4 froze the original 41-case cohort and pilot selection; v5 corrected database initialization
ordering; v6 added sanitized exception-chain evidence; v7 made workspace snapshots collision-free
on Windows; v8 propagated the request deadline; v9 preserves complete Gemini model Parts, including
opaque `thoughtSignature` values, and maps responses to the exact function names and IDs. The
v8 hash is `f270ac813c88bc776b1b958d84d4f9158bacbae01b0007bddb2e9b53ee5a3b1b`; the v9 hash is
`ffb0dd226184027dde73ca031e7c88cd4e51aa2c6f366e7ea2ccbc7063da0e33`. v9 changes neither cases,
prompts, limits, nor scoring. Its source hashes and two leakage audits are included in the frozen
files.

## Case selection

`mine.py` clones each configured repository and selects non-merge commits whose subject contains a
whole-word “fix”. A candidate must change one or more Python test files and one to three Python
source files. It must produce separate bounded diffs for tests and source. Commits are enumerated
newest-first within each repository to prioritize cases compatible with the current Python test
runtime; this affects processing order only, not eligibility. The recorded metadata
contains the parent and fixing commit hashes, test paths, source paths, dependency pins, and hashes
of the human patch and commit subject. The plaintext commit subject is not stored.

`prepare.py` checks out the parent commit, applies only the test diff, and runs the selected tests
three times inside Selfheal's untrusted-command sandbox. It then applies the source-only historical
fix and runs those tests three more times. Any nonreproducing, flaky, or human-fix-failing case is
discarded. The full suite is checked once after the human fix and recorded as extra-check evidence;
it does not determine eligibility because unrelated historical suite failures are common. Each case
gets a separate virtual environment. Dependencies are downloaded as wheels, then installed offline.

## Contamination safeguards

- Clones, raw metadata, prepared workspaces, and both diff artifacts live in the per-user cache
  outside the MCP repository root. `SELFHEAL_REAL_BUG_DATA_DIR` can override the location only with
  another path outside that root.
- The model workspace is copied from the selected parent plus the test-only diff. It has no `.git`
  directory, fixing commit subject, fixing commit hash in model context, or later history. Case IDs
  are opaque repository-scoped digests and do not expose the fixing commit SHA.
- The human source diff is kept in a sibling `scoring_only/` directory and is never copied into the
  MCP workspace or prompt. Scoring compares aggregate tests and does not reveal the patch.
- Mining/preparation subprocesses receive no environment variable whose name contains `TOKEN`,
  `SECRET`, `PASSWORD`, or `API_KEY`; Git system/global config and interactive prompts are disabled.
- With-validation runs use Selfheal's MCP patch gate and test sandbox. The no-policy arm skips
  Selfheal's patch-size and anti-cheating policy but retains absolute path confinement and the OS
  sandbox for untrusted tests. It therefore measures the patch policy's contribution, not the value
  of executing mined code without a sandbox.
- Only `read_file`, `list_files`, `run_tests`, and `propose_patch` schemas are shown to the model.
  Fixing commit information and benchmark result metadata are not tools.

## Reproduce

First install the repository's development dependencies and prepare a dedicated disposable
PostgreSQL database whose name ends with `_test`. Set `REAL_BUG_DATABASE_URL` to that database for
the runner. Do not reuse `DATABASE_URL` or a production database. Preparation needs network access
to clone public repositories and download binary wheels; individual test runs have no network and
no inherited credentials.

```powershell
python -m benchmarks.real_bugs.mine
python -m benchmarks.real_bugs.prepare
```

For a provider run, use a supported free-tier API backend, verify the provider account has no paid
billing path, and make the required operator attestations. The API cannot independently verify
account billing settings. Never set an attestation without checking the account immediately before
the run. The one-command scripts run both validation arms over the full set and repeat the first ten
cases twice for variance; they write raw JSONL plus a dated JSON report and Markdown table. They
resume each run file on re-execution.

```powershell
$env:REAL_BUG_PROVIDER = "gemini_api"
$env:GEMINI_API_MODEL = "gemini-3.8-flash"
$env:REAL_BUG_FREE_TIER_CONFIRMED = "true"
$env:REAL_BUG_BILLING_DISABLED = "true"
$env:GEMINI_FREE_TIER_CONFIRMED = "true"
$env:GEMINI_QUOTA_CHECKED_UTC = "<current UTC timestamp ending in Z>"
$env:GEMINI_38_FLASH_FREE_QUOTA = "true"
```

To resume the separate v9 pilot after the operator has independently checked and set every
required value (including a fresh UTC quota timestamp and `PGPASSWORD`), use this exact runner
command. It loads the already frozen ten IDs, runs both arms, caps each case at 40,000 tokens and
zero reported cost, applies the 240-second case timeout, and uses resume semantics. The runner
fails closed on stale quota, database identity, or provider checks; do not substitute another
provider or model.

```powershell
python -m benchmarks.real_bugs.run `
  --provider gemini_api `
  --case-list benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v9-pilot-10.json `
  --run-file benchmarks/real_bugs/results/runs/gemini-v9-pilot-10.jsonl `
  --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

This command is a reproduction instruction, not evidence that a v9 provider run occurred. Preserve
the JSONL on interruption; do not delete failed rows or selectively retry them.

The Gemini quota timestamp must be checked against AI Studio immediately before the run; the
benchmark rejects missing, malformed, future, or older-than-15-minute attestations. Set the model's
quota flag only after confirming that exact model has positive free quota. `GEMINI_API_KEY` must be
available to the benchmark process; never put its value in the run file or documentation.

For Groq, use:

```powershell
$env:REAL_BUG_DATABASE_URL = "postgresql+asyncpg://selfheal:<password>@localhost:5432/selfheal_test"
$env:REAL_BUG_PROVIDER = "groq_api"
$env:REAL_BUG_FREE_TIER_CONFIRMED = "true"
$env:REAL_BUG_BILLING_DISABLED = "true"
$env:GROQ_FREE_PLAN_CONFIRMED = "true"
$env:GROQ_GPT_OSS_120B_FREE_QUOTA = "true"
$runFile = "benchmarks/real_bugs/results/runs/groq-free.jsonl"
python -m benchmarks.real_bugs.run --run-file $runFile --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

The historical OpenRouter benchmark route was restricted to
`cohere/north-mini-code:free`. Set `OPENROUTER_API_KEY`,
`ANTHROPIC_MODEL` to that exact ID, and `OPENROUTER_FREE_TIER_CONFIRMED=true`.
Before inference, it verifies the key's Free-tier status, exact model pricing,
and an active zero-priced endpoint advertising `tools`. Provider fallbacks and
automatic completion retries are disabled. Each response must report the exact
model, `is_byok=false`, and zero cost; missing or positive cost stops the case.
The VIMJ budget-v2 one-case harness caps aggregate input plus output tokens at
32,768 and provider requests at 10, below OpenRouter's documented unaffiliated Free-tier
limit of 50 requests/day. See the VIMJ runbook for its full attestation and
evidence procedure. The current separately versioned VIMJ route uses Gemini 2.5 Flash through
`GeminiApiClient`; its exact budget and account-level free-tier gates are documented in the
[VIMJ evaluation record](unresolved-bug-evaluation.md). The preserved Cohere/OpenRouter files
and attempt evidence remain historical and unchanged.

Score one or more JSONL files with:

```powershell
python -m benchmarks.real_bugs.score benchmarks/real_bugs/results/runs/<run>.jsonl
```

Once the free-tier account and dedicated local PostgreSQL database are configured, the complete
orchestration can be started with `scripts/run_real_bug_benchmark.sh` or
`scripts/run_real_bug_benchmark.ps1`. The local path uses native processes and does not require
Docker. The PostgreSQL service must already be running and `REAL_BUG_DATABASE_URL` must point to a
disposable database ending in `_test`.

This writes a dated JSON report and Markdown table in `benchmarks/real_bugs/results/`. Keep raw
provider outputs local; the committed score report contains aggregate results and input paths only.

## Metrics and failure labels

Resolve rate is the share of attempts whose target tests pass. “Resolve + full suite” requires both
the target tests and the full suite to pass. The false-positive rate is a conservative available
proxy: target tests pass while the aggregate suite fails, among cases where the human fix passed the
suite and additional test files exist. It does not prove semantic equivalence to the historical
patch; valid alternative implementations may differ from that patch. Costs are medians of provider
reported costs only; unreported cost remains unknown. Time is wall-clock time through the repair and
verification attempt. Token counts come from provider usage fields.

The taxonomy distinguishes `no_patch`, `invalid_patch`, `rejected_by_policy`,
`tests_still_failing`, `timeout`, `rate_limited`, `provider_error`, `token_cap`, `cost_cap`,
`cost_unknown`, and `repair_error`. A case that fixes target tests but breaks the full suite is
counted as resolved and as a false-positive proxy, preserving both facts.

## Where it fails

The two v5/v7 attempts failed before repair: the saved exception chain ends in `httpx.ReadTimeout`
from Gemini `generateContent`. The benchmark's in-memory MCP setup and `get_error` tool call were
not network operations. Its 120-second adapter timeout did not inherit the 240-second case
deadline. v8 propagated the remaining case budget and added sanitized timeout classification.
Separately, all three v8 attempts failed with Gemini HTTP 400 `INVALID_ARGUMENT`: conversation
reconstruction dropped the original model Part's `thoughtSignature`, and used a local tool-use ID
as `functionResponse.name` instead of the function name/provider call ID. The returned error named
`default_api:run_tests`. v9 now returns complete model Parts unchanged and emits matching function
responses in source-call order. Offline tests simulate the 200 → `run_tests` → 400 path, followed
by sequential and parallel function calls. No live request was used to verify the fix. This follows
Google's [function-calling guidance](https://ai.google.dev/gemini-api/docs/generate-content/function-calling):
return original Parts with signatures and include matching function-call IDs in responses.

The separate Groq v3 diagnostic made one successful API call and then rejected the next request
locally: its prompt estimate was 6,818 tokens while the observed remaining TPM was 5,936. The raw
record traces `get_error` and `read_file` but does not preserve per-message sizes. No repair was
attempted after the budget rejection. V4 can wait once for a server reset header if the estimated
request fits a fresh window and the case deadline allows; that behavior has offline tests only.

The frozen v9 cohort contains 41 IDs and its leakage audit passes with zero drops; the separate
10-ID pilot audit also passes with zero drops. Before the cohort freeze, 75 candidates were
discarded because preparation found flaky tests or a human fix that did not pass; 127 candidates
were not prepared at freeze time. Those selection exclusions are not pilot attempts.

## Threats to validity

- Commit-message mining favors fixes whose authors wrote “fix”; it misses bugs fixed under other
  wording and changes without tests.
- The configured repositories are small and Python-centric. They do not represent all languages,
  project sizes, maintainers, or deployment environments.
- Historical tests may be incomplete, nondeterministic, or depend on old platform/library behavior.
  Three repetitions reduce but do not eliminate flakiness; only the target tests are repeated three
  times, while the post-fix full suite is measured once.
- Providers and free quotas change. Operator attestation is not independent billing verification.
- Full-suite failure is only a false-positive proxy; a passing suite does not prove all behavior is
  correct, and a failing suite may be unrelated to the repair.
- The no-policy arm still uses the OS test sandbox for security. It does not estimate outcomes on an
  unsafe host runner.
- Results are per attempt and can be affected by model nondeterminism, rate limits, timeouts, and
  token budgets. All five v5/v7/v8 records remain in the denominator; prior records were preserved,
  not selectively rerun to improve scores.
- Groq's v3 diagnostic is a separate one-case experiment, not part of the Gemini pilot denominator
  or the official Groq pilot. Its byte-based token estimate is heuristic, and its result does not
  prove whether an eventual 1,182-token completion cap can produce a repair.
- The 10-case pilot is a deterministic, five-repository slice and is too small to estimate
  performance over the full frozen list. Selection by repository-stratified hash is reproducible
  but is not a random sample of real-world bugs. Only two of ten selected IDs appear in the five
  records, and the v5/v7 records repeat the same case/arm across versions. No repair-quality result
  can be inferred from this failure-only partial record.
- Full-suite-green resolve rate is 0/5; false-positive proxy has no eligible denominator. The five
  attempt rows report 6,989 tokens total; billed cost is unknown, not zero.
- No new provider request or database operation was made to implement or test the adapter fix. A
  future live resume still requires the operator's quota and database preflight to pass.
