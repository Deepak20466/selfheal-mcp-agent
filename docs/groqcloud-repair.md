# GroqCloud repair path

## Adapter and model

`groq_api` reuses the project's OpenAI-compatible chat-completions adapter and the existing
Selfheal repair loop. The default and frozen model is `openai/gpt-oss-120b`. The adapter carries
assistant tool-call IDs and matching tool results across turns, including ordered batches of
returned calls. It applies the remaining case deadline to each HTTP request and reports sanitized
HTTP timeout failures without retrying them. Explicit 502/503/504 responses retain the existing
bounded retry behavior; 429 remains a recorded rate-limit stop. API keys are read from
`GROQ_API_KEY` and are never part of benchmark artifacts.

Groq's current [model documentation](https://console.groq.com/docs/models) lists GPT-OSS 120B and
its [local tool-use guide](https://console.groq.com/docs/tool-use/local-tool-calling) documents
function-call/tool-result conversation turns. Groq's [tool-use overview](https://console.groq.com/docs/tool-use/overview)
currently lists parallel tool use as unsupported for this model. The adapter preserves IDs and
order if multiple calls are returned, but this is not a claim that GPT-OSS 120B will produce
parallel calls or that account quotas are available. The operator must verify free-plan access,
the exact model quota, and disabled paid billing in their Groq account immediately before a live
run. The API key and operator attestations are unavailable to Codex and have not been tested live.

## Frozen Groq configuration

The separate Groq freeze is
[`2026-11-09-groq-openai-gpt-oss-120b-v1.json`](../benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v1.json),
SHA-256 `35cb2c6013a62f72c2821d12c5119b88ed0b63fb6c032bd995a0e135366e5d51`. It retains the same
41 case IDs, prompt, validation/sandbox arms, limits, and scoring definition as Gemini v9; it
changes only provider/model-specific configuration and current source/test hashes. Gemini v4-v9,
all historical attempt records, and all Gemini results remain untouched. The source configuration
hash is recorded in the derived pilot case list. Leakage audits pass for all 41 frozen cases, the
ten-case pilot, and the one-case diagnostic.

The ten IDs are the original deterministic pilot selection, preserved in
[`the Groq pilot case list`](../benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v1-pilot-10.json).
The separate diagnostic uses only its first ID, `attrs-73dc0d2e2860`, as specified in
[`the diagnostic case list`](../benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v1-diagnostic-1.json).
It is not part of the official ten-case result denominator. No Groq request or repair has been
made, so there are no measured Groq results yet.

Before running, the operator must independently verify the Groq free tier and billing state, set
the required operator attestations, and provide `GROQ_API_KEY` privately in the external shell.
`GROQ_API_MODEL` must be `openai/gpt-oss-120b`. `REAL_BUG_DATABASE_URL` must identify the isolated
PostgreSQL 16 database at `127.0.0.1:5434/selfheal_real_bug_test`; the runner verifies the identity
and refuses other targets. Do not put a database URL or credentials in this repository.

### First one-case diagnostic

Run only after the operator has confirmed the account's current free quota and disabled paid
billing. This command records a single diagnostic separately; it does not complete or score the
official pilot. It uses the frozen caps and timeout, and resume semantics preserve any attempt:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v1-diagnostic-1.json --run-file benchmarks/real_bugs/results/runs/groq-v1-diagnostic-1.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

If it records an attempt, preserve the JSONL and score it as a diagnostic only. Do not copy it into
the pilot file. Before starting the official pilot, review that diagnostic and independently
reconfirm free quota/billing. The separate ten-case command is recorded in the full configuration
and pilot documentation; its result file is
`benchmarks/real_bugs/results/runs/groq-v1-pilot-10.jsonl`.

After the operator reviews the diagnostic, independently confirms quota/billing again, and
decides to start the separate official pilot, run:

```powershell
$env:GROQ_API_MODEL = "openai/gpt-oss-120b"; python -m benchmarks.real_bugs.run --provider groq_api --case-list benchmarks/real_bugs/frozen/2026-11-09-groq-openai-gpt-oss-120b-v1-pilot-10.json --run-file benchmarks/real_bugs/results/runs/groq-v1-pilot-10.jsonl --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
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
