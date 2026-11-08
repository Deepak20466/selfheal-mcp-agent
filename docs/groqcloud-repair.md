# GroqCloud repair path

## Adapter and model

`groq_api` reuses the project's OpenAI-compatible chat-completions adapter and the existing
Selfheal repair loop. The default and frozen model is `openai/gpt-oss-120b`. The adapter carries
assistant tool-call IDs and matching tool results across turns, including ordered batches of
returned calls. Before each request it conservatively estimates prompt size, includes tool schemas,
and reduces the output reservation so the prompt plus completion fits the lower of the documented
131,072-token context window and the configured 8,000-token free-tier TPM window (or a lower
server-reported remaining-token count). The estimate is a UTF-8 JSON byte upper bound with framing
allowance; it does not rely on an unverified tokenizer. Oversized `list_files` output is compacted
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
SHA-256 `2fb6d1bab3e28980629eaca54b8721b6d68e116e6aa4a65499f7cbf9a4340aaa`. Its leakage audits
pass for 41/41 cases, the original pilot 10/10, and the diagnostic 1/1. V1/v2 config files,
attempt records, and all Gemini evidence remain unchanged. The v3 diagnostic has not been run.

Before running, the operator must independently verify the Groq free tier and billing state, set
the required operator attestations, and provide `GROQ_API_KEY` privately in the external shell.
`GROQ_API_MODEL` must be `openai/gpt-oss-120b`. `REAL_BUG_DATABASE_URL` must identify the isolated
PostgreSQL 16 database at `127.0.0.1:5434/selfheal_real_bug_test`; the runner verifies the identity
and refuses other targets. Do not put a database URL or credentials in this repository.

### Fresh one-case v3 diagnostic

Run only after the operator has confirmed the account's current free quota and disabled paid
billing. This command records a single diagnostic separately; it does not complete or score the
official pilot. It uses the frozen caps and timeout, and resume semantics preserve any attempt:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v3-diagnostic-1.json --run-file benchmarks/real_bugs/results/runs/groq-v3-diagnostic-1.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

If it records an attempt, preserve the JSONL and score it as a diagnostic only. Do not copy it into
the pilot file. Before starting the official pilot, review that diagnostic and independently
reconfirm free quota/billing. The separate ten-case result file is
`benchmarks/real_bugs/results/runs/groq-v3-pilot-10.jsonl`.

After the operator reviews the diagnostic, independently confirms quota/billing again, and
decides to start the separate official pilot, run:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v3-pilot-10.json --run-file benchmarks/real_bugs/results/runs/groq-v3-pilot-10.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
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
the relevant tests. VIMJ has not been modified or tested by this change.
