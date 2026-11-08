# Real historical bug benchmark

## Status

This benchmark mines historical source-and-test fixes from the pinned repositories in
[`benchmarks/real_bugs/repos.yaml`](../benchmarks/real_bugs/repos.yaml). Mining and preparation
produce eligible cases; they are not model repair attempts. Only JSONL records produced by
`benchmarks.real_bugs.run` count as provider results. `benchmarks/real_bugs/results/` contains
generated score reports when runs are scored; no provider run or score report is committed for this
benchmark. Raw local datasets, provider run files, and generated score reports are ignored.

**Status: partial pilot evidence exists; the 10-case pilot is incomplete.** The predetermined
selection remains unchanged. The saved v5 and v7 records contain two attempts for one case in the
`with_validation` arm. Both Gemini requests timed out before patch generation. No case completed,
nine selected cases were not attempted, and no `without_validation` case was attempted. The 41-case
cohort remains pending and separate. The current environment does not have the required runtime
credentials, operator attestations, fresh quota timestamp, or verified isolated database access,
so no further live attempt was made. Exact records and the scorer output are committed under
[`results/published/`](../benchmarks/real_bugs/results/published/).

**Diagnostic-request disclosure:** before the test environment was made hermetic, one targeted
test rerun sent unmatched diagnostic requests to Gemini's endpoint using the deliberately fake
test key installed by `tests/conftest.py`; Gemini returned HTTP 400. The recorded adjustment note
states that no user's API key was sent. These were test-mock failures, not benchmark repair
attempts, and they produced no benchmark outcomes. See
[`2026-11-08-runner-adjustment.md`](../benchmarks/real_bugs/results/2026-11-08-runner-adjustment.md).

## Separate 10-case pilot

This pilot is a separately labeled exploratory slice, not a replacement or partial score for the
41-case benchmark. Its original deterministic IDs are preserved in the v8 pilot file
[`2026-11-09-gemini-3.8-flash-v8-pilot-10.json`](../benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v8-pilot-10.json).
The rule groups case IDs by repository family, ranks each family by ascending SHA-256 of the UTF-8
string `selfheal-real-bug-pilot-v1:` followed by the case ID (case ID breaks ties), and takes two
cases from each of the five families. This gives ten selected cases while spanning all five
repositories. The frozen v4 config and its hash are unchanged.

| Pilot status | Measured value |
|---|---:|
| Selected | 10 |
| Attempt records | 2 (same case and arm, v5 and v7) |
| Unique selected cases attempted | 1 of 10 |
| Completed repairs | 0 |
| Resolve rate | 0/2 (0%) |
| Resolve rate with full suite green | 0/2 (0%); verification was not reached |
| False-positive proxy versus human fix | Not measurable: 0 eligible denominator |
| Median elapsed time | 121.046s (v5); 121.094s (v7) |
| Provider tokens | 0 reported in both records |
| Cost | Unknown in both records; no cost was reported |
| Failure category | `provider_error`: 1 in v5 and 1 in v7 |
| Not attempted | 9 of 10 pilot IDs; all no-validation arms; 41-case cohort |

These are actual saved attempt records, not extrapolations. Neither attempt reached target tests or
the full suite. False-positive rate is undefined because no eligible candidate passed target tests
and the false-positive denominator requires a human-fix-passing case. The v7 record preserves the
sanitized nested exception chain. The scorer summary is
[`2026-11-08.json`](../benchmarks/real_bugs/results/published/2026-11-08.json), its readable table is
[`2026-11-08.md`](../benchmarks/real_bugs/results/published/2026-11-08.md), and the source JSONL is
[`gemini-v5-v7-attempts.jsonl`](../benchmarks/real_bugs/results/published/gemini-v5-v7-attempts.jsonl).

The selected pilot contexts passed independent leakage audits with zero dropped IDs in v4, v7,
and v8. The full v8 cohort audit passed all 41 cases with zero leakage-driven drops; its pilot
audit passed all 10. See the v8 audit files alongside the config.
The 20/20 synthetic fixture benchmark is historical evidence only and is not part of this pilot
or the pending 41-case real-bug benchmark.

### Frozen setup history

v4 froze the original 41-case cohort and pilot selection; v5 corrected database initialization
ordering; v6 added sanitized exception-chain evidence; v7 made workspace snapshots collision-free
on Windows; v8 corrected Gemini request deadline propagation. Each version preserves the prior
version as history. v8 hash: `f270ac813c88bc776b1b958d84d4f9158bacbae01b0007bddb2e9b53ee5a3b1b`.
Its source hashes and leakage audit are recorded in the frozen files. The v8 change does not alter
cases, prompts, scoring, or limits.

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

To reproduce the separate v8 pilot after the operator has independently checked and set every
required value (including a fresh UTC quota timestamp and `PGPASSWORD`), use this exact runner
command. It loads the already frozen ten IDs, runs both arms, caps each case at 40,000 tokens and
zero reported cost, applies the 240-second case timeout, and uses resume semantics. The runner
fails closed on stale quota, database identity, or provider checks; do not substitute another
provider or model.

```powershell
python -m benchmarks.real_bugs.run `
  --provider gemini_api `
  --case-list benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v8-pilot-10.json `
  --run-file benchmarks/real_bugs/results/runs/gemini-v8-pilot-10.jsonl `
  --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

This command is a reproduction instruction, not evidence that a v8 provider run occurred. Preserve
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

For OpenRouter, choose `REAL_BUG_PROVIDER=openrouter_api`, set `ANTHROPIC_MODEL` to
`openrouter/free` or a model name ending in `:free`, and set
`OPENROUTER_FREE_TIER_CONFIRMED=true`. The runner uses no provider fallback. A provider-reported
cost above the configured cap stops that case. If cost is not reported, a positive cost cap cannot
be enforced and the case stops; zero-cost operation requires the explicit free-tier attestations.
Each case also has a hard timeout and token cap. `--resume` skips terminal results and retries only
provider errors, timeouts, and rate limits from the same JSONL file. A 429 is recorded and stops the
current run; resuming honors `Retry-After` or bounded exponential backoff before another provider
call.

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

Both observed v5/v7 attempts failed before repair: the saved exception chain ends in
`httpx.ReadTimeout` from the Gemini `generateContent` HTTP request. The preceding benchmark
bootstrap and `get_error` tool call use in-memory MCP transport; no MCP network request was active
at the timeout. The runner had a 240-second case deadline but the adapter kept a hardcoded
120-second HTTP timeout, which caused the request to expire early. v8 propagates the remaining
case budget into HTTPX and emits a sanitized timeout classification; tests verify propagation and
that expired deadlines do not start requests. Evidence cannot establish whether Gemini itself or
the network path stalled. Current execution is blocked at preflight: all required Gemini runtime
values/attestations and PostgreSQL password are unavailable, and quota freshness cannot be
verified. This blocker is not a benchmark case and is not in the denominator.

The frozen v8 cohort contains 41 IDs and its leakage audit passes with zero drops; the separate
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
  token budgets. The two observed attempt records are failures and both remain in the denominator;
  no retry was made to improve their scores.
- The 10-case pilot is a deterministic, five-repository slice and is too small to estimate
  performance over the full frozen list. Selection by repository-stratified hash is reproducible
  but is not a random sample of real-world bugs. Only one of ten selected IDs was attempted, once
  per v5 and v7; these two requests are not independent samples. No repair-quality result can be
  inferred from this failure-only partial record.
- Full-suite-green resolve rate is 0/2 because neither attempt got to tests; the false-positive
  proxy has no eligible denominator. Provider token usage was reported as zero, which means no
  generation usage was returned in these records; billed cost is unknown, not zero.
- The current environment preflight lacks required credentials, operator attestations, fresh quota
  evidence, and database authentication. No new provider request was made. Operator attestations
  and quota timestamp must be refreshed by the operator after checking the exact free-tier account.
