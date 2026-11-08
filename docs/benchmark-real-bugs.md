# Real historical bug benchmark

## Status

This benchmark mines historical source-and-test fixes from the pinned repositories in
[`benchmarks/real_bugs/repos.yaml`](../benchmarks/real_bugs/repos.yaml). Mining and preparation
produce eligible cases; they are not model repair attempts. Only JSONL records produced by
`benchmarks.real_bugs.run` count as provider results. `benchmarks/real_bugs/results/` contains
generated score reports when runs are scored; no provider run or score report is committed for this
benchmark. Raw local datasets, provider run files, and generated score reports are ignored.

**Status: the 10-case pilot selection is frozen, but live execution is blocked.** The separate
pilot selected 10 cases deterministically from frozen v4 before observing any pilot outcomes. In
the latest execution preflight, required Gemini credentials, PostgreSQL password, and operator
free-tier attestations were unavailable. PostgreSQL authentication and a Gemini request were not
attempted. No pilot cases were attempted; no pilot repair result or metric is available. The
41-case v4 benchmark remains pending and is not represented by this pilot. See the safe preflight
record and pilot-specific leakage audit under `benchmarks/real_bugs/frozen/`.

**Diagnostic-request disclosure:** before the test environment was made hermetic, one targeted
test rerun sent unmatched diagnostic requests to Gemini's endpoint using the deliberately fake
test key installed by `tests/conftest.py`; Gemini returned HTTP 400. The recorded adjustment note
states that no user's API key was sent. These were test-mock failures, not benchmark repair
attempts, and they produced no benchmark outcomes. See
[`2026-11-08-runner-adjustment.md`](../benchmarks/real_bugs/results/2026-11-08-runner-adjustment.md).

## Separate 10-case pilot

This pilot is a separately labeled exploratory slice of v4, not a replacement or partial score
for the 41-case benchmark. Its selection is recorded in
[`2026-11-08-gemini-3.8-flash-v4-pilot-10.json`](../benchmarks/real_bugs/frozen/2026-11-08-gemini-3.8-flash-v4-pilot-10.json).
The rule groups case IDs by repository family, ranks each family by ascending SHA-256 of the UTF-8
string `selfheal-real-bug-pilot-v1:` followed by the case ID (case ID breaks ties), and takes two
cases from each of the five families. This gives ten selected cases while spanning all five
repositories. The frozen v4 config and its hash are unchanged.

| Pilot status | Measured value |
|---|---:|
| Selected | 10 |
| Attempted | 0 |
| Resolve rate with full suite green | Not measured |
| False-positive rate versus human fix | Not measured |
| Median execution time | Not measured |
| Cost | Not measured |
| Failure categories | No case outcomes; no frequencies measured |

Preflight stopped before a database query or provider request because the required runtime
credentials and free-tier attestations were unavailable. The timestamp freshness check passed,
but a fresh timestamp alone does not authorize or enable a run. The preflight record is
[`2026-11-08-gemini-3.8-flash-v4-pilot-10-status.json`](../benchmarks/real_bugs/frozen/2026-11-08-gemini-3.8-flash-v4-pilot-10-status.json).

The selected pilot contexts were audited independently: 10 of 10 passed with zero dropped cases.
See [`2026-11-08-gemini-3.8-flash-v4-pilot-10-leakage-audit.json`](../benchmarks/real_bugs/frozen/2026-11-08-gemini-3.8-flash-v4-pilot-10-leakage-audit.json).
The 20/20 synthetic fixture benchmark is historical evidence only and is not part of this pilot
or the pending 41-case real-bug benchmark.

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

The selected pilot has no attempted repairs, so it has no observed repair failures to classify.
Its zero attempts are an environment preflight blocker, not a provider error and not a failed case;
do not count them in a repair denominator. On execution, record each arm for every attempted case
including `no_patch`, invalid or policy-rejected patches, target-test failures, timeouts, rate
limits, provider errors, token/cost limits, and repair errors. The frozen v4 cohort has 41
leakage-audited cases and no leakage-driven drops. Before that freeze, 75 candidates were discarded
during preparation because they were flaky or their human fix did not pass, and 127 candidates had
not been prepared at freeze time; these exclusions are recorded in the frozen config and are not
pilot outcomes.

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
  token budgets. Failed and incomplete records remain in scoring; they are not silently removed.
- The 10-case pilot is a deterministic, five-repository slice and is too small to estimate
  performance over the full frozen list. Selection by repository-stratified hash is reproducible
  but is not a random sample of real-world bugs. Because it has not run, it currently provides no
  evidence about repair quality, model reliability, time, or cost.
- The current pilot blocker is environmental: absent credentials/attestations prevented even
  database authentication from being checked. This is not a provider failure and does not enter
  any benchmark denominator.
