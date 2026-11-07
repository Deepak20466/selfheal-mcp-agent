# Real historical bug benchmark

## Status

This benchmark mines historical source-and-test fixes from the pinned repositories in
[`benchmarks/real_bugs/repos.yaml`](../benchmarks/real_bugs/repos.yaml). Mining and preparation
produce eligible cases; they are not model repair attempts. Only JSONL records produced by
`benchmarks.real_bugs.run` count as provider results. `benchmarks/real_bugs/results/` contains
committed score reports, while its raw local dataset and provider run files are ignored.

The initial target is at least 40 prepared cases. A smaller count must be reported as measured; the
benchmark never duplicates or hand-selects cases to meet the target. See the README's results table
and this repository's verification report for the current count.

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
the run.

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
provider errors/timeouts from the same JSONL file.

Score one or more JSONL files with:

```powershell
python -m benchmarks.real_bugs.score benchmarks/real_bugs/results/runs/<run>.jsonl
```

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
`tests_still_failing`, `timeout`, `provider_error`, `token_cap`, `cost_cap`, `cost_unknown`, and
`repair_error`. A case that fixes target tests but breaks the full suite is counted as resolved and
as a false-positive proxy, preserving both facts.

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
