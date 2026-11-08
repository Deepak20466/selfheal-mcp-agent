# GroqCloud repair path

## Adapter and model

`groq_api` reuses the project's OpenAI-compatible chat-completions adapter and the existing
Selfheal repair loop. The default and frozen model is `openai/gpt-oss-120b`. The adapter carries
assistant tool-call IDs and matching tool results across turns, including ordered batches of
returned calls. Before each request it estimates prompt size, includes tool schemas,
and reduces the output reservation so the prompt plus completion fits the lower of the documented
131,072-token context window and the configured 8,000-token free-tier TPM window (or a lower
server-reported remaining-token count). The estimate is a UTF-8 JSON byte heuristic with framing
allowance; it does not rely on an unverified tokenizer and is not exact token telemetry. Oversized `list_files` output is compacted
to an explicit, evenly spaced sample that can be re-queried by glob. Oversized test output retains
test names, errors, exception text and traceback lines and marks omitted log lines. `read_file`
content is never truncated; if diagnostic context cannot fit, the adapter reports the budget and
sends no request. Tool-call IDs and ordering remain unchanged, and compaction affects only the
serialized request copy.

The adapter applies the remaining case deadline to each HTTP request and reports sanitized timeout
failures without retrying them. Explicit 502/503/504 responses retain the existing bounded retry
behavior. A confirmed TPM 429 may retry once using the exact server delay, only when it is at most
60 seconds and the delay plus one second fits the remaining case deadline. Daily quota failures are
not retried. Complete conversation history is preserved, and `finish_reason=length` is a failure
rather than a partial successful turn. API keys are read from
`GROQ_API_KEY` and are never part of benchmark artifacts.

Groq's current [model documentation](https://console.groq.com/docs/models) lists GPT-OSS 120B and
its [local tool-use guide](https://console.groq.com/docs/tool-use/local-tool-calling) documents
function-call/tool-result conversation turns. Groq's [tool-use overview](https://console.groq.com/docs/tool-use/overview)
currently lists parallel tool use as unsupported for this model. The adapter preserves IDs and
order if multiple calls are returned, but this is not a claim that GPT-OSS 120B will produce
parallel calls or that account quotas are available. The operator must verify free-plan access,
the exact model quota, and disabled paid billing in their Groq account immediately before a live
run. Codex makes no live provider calls; earlier operator attempts are preserved in separate v1
and v2 result files.

## Groq v1 finding and frozen v2 configuration

The prior Groq v1 diagnostic record is preserved in
`benchmarks/real_bugs/results/runs/groq-v1-diagnostic-1.jsonl`. It records one TPM 429 on
`attrs-73dc0d2e2860`: limit 8,000, used 1,366, requested 7,843, with the server suggesting a
9.0675-second wait. Its trace shows the initial error lookup and one source read before the failure;
the record does not contain the failed HTTP payload, so the exact prompt/output contribution to
`Requested` cannot be reconstructed. The adapter allowed up to 8,000 output tokens per response,
which left little room under the observed TPM cap. V2 halves that reservation while retaining the
full conversation. The saved evidence cannot prove whether the 7,843 request projection came from
the output allowance, accumulated prompt/tool history, or both; the new diagnostic will measure
whether the cap resolves the rate limit without losing repair context.

The new Groq v2 freeze is
[`2026-11-09-groq-openai-gpt-oss-120b-v2.json`](../benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v2.json),
SHA-256 `4c1adfa78b9add4bb446fc0b20e50b446ff396595b06f39d78699c23d3e90559`. It retains the same
41 cases, original ten pilot IDs, prompts, validation/sandbox arms, scoring, and case limits as v1.
Groq-specific changes cap each response at 4,096 output tokens and allow one automatic retry only
for a confirmed TPM 429 with server delay no greater than 10 seconds. Daily quota 429s are not
retried. Full conversation and tool history are retained; `finish_reason=length` is reported as an
incomplete-response error. V1 and all Gemini freezes and results are unchanged. Leakage audits pass
for all 41 v2 cases, the ten-case pilot, and the separate one-case diagnostic.

The ten IDs remain the original deterministic selection in
[`the Groq v2 pilot list`](../benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v2-pilot-10.json).
The separate diagnostic still uses only its first ID, `attrs-73dc0d2e2860`, and remains outside the
official ten-case denominator.

## Groq v2 findings and frozen v3 configuration

The preserved v2 diagnostic has two failed arm attempts on `attrs-73dc0d2e2860`. The validation
arm recorded the specified TPM 429 (8,000 limit, 5,630 used, 7,689 requested, 39.8925-second server
delay) after `get_error`, `read_file`, and `run_tests`; its prior successful-turn usage totals were
8,909 input and 151 output tokens. The no-validation arm recorded HTTP 400, “Please reduce the
length of the messages or completion,” after `get_error` and `list_files`; its prior successful-turn
usage totals were 1,503 input and 35 output tokens. Neither arm completed a repair. V2 stored tool
names and aggregate usage, but not per-tool output byte counts or the rejected request bodies, so
the individual contribution of source/test/listing history and completion reservation cannot be
reconstructed. The traces do show that multi-turn history grew before the failures.

Frozen v3 keeps the same cases, prompts, scoring, limits and 10 pilot IDs. It budgets the prompt
estimate plus completion against 8,000 TPM and the model's documented 131,072-token context limit;
uses current remaining-token headers where available; compacts only rediscoverable file listings
and non-diagnostic test-log lines; preserves complete source and tool-call order; and fails closed
if essential context will not fit. The local estimate uses serialized UTF-8 byte length at two
bytes per estimated token plus framing overhead; this is a conservative planning heuristic, not an
exact GPT-OSS tokenizer or a formal upper bound. Response rate-limit headers are authoritative
after a successful request, and a server 429 may trigger one bounded wait/retry. A TPM retry uses
the actual server delay only once, only when it is at most 60 seconds and fits the case's remaining
deadline. Daily quota errors are never retried.

Groq's [rate-limit documentation](https://console.groq.com/docs/rate-limits) currently lists 8K
TPM for GPT-OSS 120B on the Free plan and documents the remaining/reset-token headers; the
[model page](https://console.groq.com/docs/model/openai/gpt-oss-120b) lists a 131,072-token context.
Account-specific limits can differ and must be checked by the operator.

V3 is frozen in
[`the Groq v3 config`](../benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v3.json),
SHA-256 `9f81fe6a0dc75db874d7f6246ce51039330e42a9e194d8da75761e6c353a5e29`. Its leakage audits
pass for 41/41 cases, the original pilot 10/10, and the diagnostic 1/1. V1/v2 config files,
attempt records, and all Gemini evidence remain unchanged. V3 has one preserved diagnostic attempt:
[`groq-v3-diagnostic-1.jsonl`](../benchmarks/real_bugs/results/published/groq-v3-diagnostic-1.jsonl).
It made one successful API call (1,505 input/89 output tokens), then its next request was stopped
locally with `prompt_estimate=6818` and `available_tpm=5936`. Runtime was 2.438s, cost was not
reported, and no repair completed. The trace contains `get_error` and `read_file`; the artifact does
not record per-message sizes, so their exact contribution cannot be separated. The runner labels the
failure `provider_error`, while the detailed cause is a local `GroqRequestBudgetError`.

Frozen v4 addresses the confirmed temporary-window blocker without changing the cohort, prompts,
scoring, timeouts, or caps. When the request can fit a fresh 8,000-token window, the adapter waits
once for the exact `x-ratelimit-reset-tokens` time, bounded to 60 seconds and the request deadline.
It stops without another request if the prompt plus minimum output cannot fit a fresh window or if
the server did not provide usable reset timing. The prompt estimate remains heuristic, so this is a
safe bounded attempt rather than a guarantee that the account will accept the request. V4 hash:
`f63154b3b6b9fa110a450ff69a524753f335652f6c6c80eb5824c948ce7b8335`. Leakage audits pass for
41/41, 10/10, and 1/1. V1-v3 freezes and all historical results are unchanged; no v4 provider run
has occurred.

### Groq v5 tool-name compatibility correction

The v4 diagnostic preserved the model-returned name `repo_browser.list_files`. MCP registers and
the adapter advertises only the canonical `list_files` schema; the failure was in the benchmark
wrapper's exact-name authorization check, not an MCP registration mismatch. The wrapper now maps
only that exact observed alias to `list_files`, then applies the existing allowlist and dispatches
the canonical name. Tool events retain both the canonical dispatch name and the requested alias.
Other namespaces and unknown tools remain rejected. Tool arguments, call IDs, result matching, and
MCP audit behavior are unchanged.

The v5 frozen config keeps all 41 cases, pilot IDs, prompts, arms, scoring, and limits unchanged;
only frozen source hashes, version references, and the leakage-audit artifacts differ. Its full
config hash is `2621014dd12c6de1b759f75e9987d8251cc914523522a8ffcbe2af8144d4063b`. Leakage audits
pass for 41/41, 10/10, and 1/1 cases. No v5 provider call has been made.

Before running, the operator must independently verify the Groq free tier and billing state, set
the required operator attestations, and provide `GROQ_API_KEY` privately in the external shell.
`GROQ_API_MODEL` must be `openai/gpt-oss-120b`. `REAL_BUG_DATABASE_URL` must identify the isolated
PostgreSQL 16 database at `127.0.0.1:5434/selfheal_real_bug_test`; the runner verifies the identity
and refuses other targets. Do not put a database URL or credentials in this repository.

### Fresh one-case v5 diagnostic

Run only after the operator has confirmed the account's current free quota and disabled paid
billing. This uses a new results file; do not resume into the already failed v3 results file. It
records one diagnostic outside the official pilot denominator:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v5-diagnostic-1.json --run-file benchmarks/real_bugs/results/runs/groq-v5-diagnostic-1.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

If it records an attempt, preserve the JSONL and score it as a diagnostic only. Do not copy it into
the pilot file. Before starting the official pilot, review that diagnostic and independently
reconfirm free quota/billing. The separate ten-case result file is
`benchmarks/real_bugs/results/runs/groq-v5-pilot-10.jsonl`.

After the operator reviews the diagnostic, independently confirms quota/billing again, and
decides to start the separate official pilot, run:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v5-pilot-10.json --run-file benchmarks/real_bugs/results/runs/groq-v5-pilot-10.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

## VIMJ Studio OS SaaS safe workflow

The public [VIMJ repository](https://github.com/Deepak20466/vimj-studio-os-saas) documents a FastAPI/
SQLAlchemy backend, React/Vite frontend, Flutter client, PostgreSQL, and a local Admin-only MCP
server. Its five MCP tools include read operations and attendance/fee mutations; that application
MCP server is not required for this repair workflow and must not be connected to an autonomous
repair session.

For a future repair, first identify one reproducible issue and its existing failing test. Work from
a fresh local clone on a new `selfheal/diagnostic-<issue>` branch. Inspect only tracked source and
sanitized test output; use synthetic fixtures and a disposable local test database, never deployed
services, production credentials, student/guardian records, or customer data. Let Selfheal prepare
an isolated repository snapshot, propose a narrow source-only diff, and run the target test followed
by the relevant backend suite. Review the diff and test output yourself, push only that branch, and
open a PR for human review. Do not merge, deploy, or claim a repair unless the actual patch passes
the relevant tests. Separately, VIMJ PR [#1](https://github.com/Deepak20466/vimj-studio-os-saas/pull/1)
contains a Codex-assisted SQLite password-reset timestamp fix; its 30 backend tests and PR CI pass.
That patch was not produced by Selfheal and is not verified against production PostgreSQL. No
Selfheal run against VIMJ is recorded.
