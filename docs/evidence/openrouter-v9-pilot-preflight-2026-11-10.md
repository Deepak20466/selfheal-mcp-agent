# OpenRouter v9 10-Case Pilot Preflight

**Date:** 2026-11-10

**Status:** Free-model and tool-capability checks passed; benchmark stopped before any model completion because of disposable database permissions.

## Verified before the stop

- The locally configured OpenRouter key passed the authenticated `/api/v1/key` Free-tier check. The key value was not printed or saved.
- `cohere/north-mini-code:free` was present in OpenRouter's current model catalog. Prompt, completion, and endpoint prices were all zero; the catalog and active endpoint passed the benchmark's minimum 32,768-token context, 4,096-token output, and `tools` capability checks.
- The existing OpenRouter adapter sends that exact model ID, disables provider fallbacks, includes usage, and rejects a mismatched model, nonzero or unknown cost, or BYOK routing.
- The disposable target `selfheal_real_bug_test` was reachable on `127.0.0.1:5434` and identified itself as PostgreSQL 16.
- Offline compatibility and runner tests passed: **125 passed, 2 skipped**. The separate frozen Gemini v9 blinding audit also passed 10/10 with no dropped IDs.

OpenRouter's documentation describes free variants and their pricing in its [free-model collection](https://openrouter.ai/collections/free-models), and exposes key tier metadata through [Get current API key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key). The model catalog's current entry is [Cohere North Mini Code (free)](https://openrouter.ai/cohere/north-mini-code:free).

## Why the evaluation stopped

The database accepted a connection with the locally configured `selfheal` role, but that role cannot read or update `public.alembic_version` and cannot create in the `public` schema. The database and `alembic_version` table are owned by `postgres`. The runner therefore failed during migrations, before case execution.

The run file `benchmarks/real_bugs/results/runs/openrouter-north-mini-code-v9-pilot-10-20261110.jsonl` was not created. There are **zero new benchmark attempts, zero model completions, and no new success-rate result**. Only non-generative OpenRouter metadata GETs were used to verify the key and model. No paid request was sent. Historical Gemini and VIMJ artifacts remain unchanged.

## Required configuration to resume

Provide the runner with a credential for the isolated database that owns `selfheal_real_bug_test`, `public`, and its existing Alembic-managed tables, or grant the benchmark role equivalent migration privileges. The runner requires this exact target and PostgreSQL version:

```text
postgresql+asyncpg://<migration-owner>:<secret>@127.0.0.1:5434/selfheal_real_bug_test
```

Set that as `REAL_BUG_DATABASE_URL` in the benchmark process; keep the secret out of Git and chat. Do not point it at the application database. The current `selfheal` role is insufficient for migrations.

The benchmark process must also receive the already verified local OpenRouter key as `OPENROUTER_API_KEY` and select `ANTHROPIC_MODEL=cohere/north-mini-code:free`; the checked-in `.env` currently selects a paid Claude model, so the free model must be an invocation-only override. Set `OPENROUTER_FREE_TIER_CONFIRMED=true`, `REAL_BUG_FREE_TIER_CONFIRMED=true`, and `REAL_BUG_BILLING_DISABLED=true` only for this verified free-tier run. No paid model or provider fallback is permitted. OpenRouter documents free-model limits of 20 requests per minute and 50 per day, with failed requests counting toward the daily cap; a 429 stops the benchmark without paid fallback ([free-model limits](https://openrouter.ai/collections/free-models)).

## Resume command

After the database role is corrected, use the frozen pilot IDs and a new OpenRouter-only run file:

```powershell
python -m benchmarks.real_bugs.run `
  --provider openrouter_api `
  --case-list benchmarks/real_bugs/frozen/2026-11-09-gemini-3.8-flash-v9-pilot-10.json `
  --run-file benchmarks/real_bugs/results/runs/openrouter-north-mini-code-v9-pilot-10-20261110.jsonl `
  --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
```

The runner rechecks the live Free-tier key, exact free model pricing, and tool-capable endpoint before migration. Each completion must return exact zero cost and non-BYOK routing or the run stops. This report records a blocked preflight, not a benchmark outcome.
