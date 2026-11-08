# GroqCloud repair path

## Adapter and model

`groq_api` reuses the project's OpenAI-compatible chat-completions adapter and the existing
Selfheal repair loop. The default and frozen model is `openai/gpt-oss-120b`. The adapter carries
assistant tool-call IDs and matching tool results across turns, including ordered batches of
returned calls. It caps each response's output-token reservation at 4,096, applies the remaining
case deadline to each HTTP request, and reports sanitized timeout failures without retrying them.
Explicit 502/503/504 responses retain the existing bounded retry behavior. A confirmed TPM 429
can be retried once using the server's delay, bounded at 10 seconds; daily quota failures are not
retried. Complete conversation history is preserved, and `finish_reason=length` is a failure rather
than a partial successful turn. API keys are read from
`GROQ_API_KEY` and are never part of benchmark artifacts.

Groq's current [model documentation](https://console.groq.com/docs/models) lists GPT-OSS 120B and
its [local tool-use guide](https://console.groq.com/docs/tool-use/local-tool-calling) documents
function-call/tool-result conversation turns. Groq's [tool-use overview](https://console.groq.com/docs/tool-use/overview)
currently lists parallel tool use as unsupported for this model. The adapter preserves IDs and
order if multiple calls are returned, but this is not a claim that GPT-OSS 120B will produce
parallel calls or that account quotas are available. The operator must verify free-plan access,
the exact model quota, and disabled paid billing in their Groq account immediately before a live
run. Codex has no API key, and no v2 provider call was made; the earlier v1 operator call is kept
as a separate historical attempt.

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

Before running, the operator must independently verify the Groq free tier and billing state, set
the required operator attestations, and provide `GROQ_API_KEY` privately in the external shell.
`GROQ_API_MODEL` must be `openai/gpt-oss-120b`. `REAL_BUG_DATABASE_URL` must identify the isolated
PostgreSQL 16 database at `127.0.0.1:5434/selfheal_real_bug_test`; the runner verifies the identity
and refuses other targets. Do not put a database URL or credentials in this repository.

### Fresh one-case v2 diagnostic

Run only after the operator has confirmed the account's current free quota and disabled paid
billing. This command records a single diagnostic separately; it does not complete or score the
official pilot. It uses the frozen caps and timeout, and resume semantics preserve any attempt:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v2-diagnostic-1.json --run-file benchmarks/real_bugs/results/runs/groq-v2-diagnostic-1.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

If it records an attempt, preserve the JSONL and score it as a diagnostic only. Do not copy it into
the pilot file. Before starting the official pilot, review that diagnostic and independently
reconfirm free quota/billing. The separate ten-case result file is
`benchmarks/real_bugs/results/runs/groq-v2-pilot-10.jsonl`.

After the operator reviews the diagnostic, independently confirms quota/billing again, and
decides to start the separate official pilot, run:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v2-pilot-10.json --run-file benchmarks/real_bugs/results/runs/groq-v2-pilot-10.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
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
