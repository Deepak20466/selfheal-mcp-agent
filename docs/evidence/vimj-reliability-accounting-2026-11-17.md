# VIMJ repair attempts: accounting (2026-11-17)

**Scope:** every model-generated VIMJ attempt recorded for the two frozen cases, counted from their
verification records. Failed attempts are kept. This page corrects earlier counts.

## Counts

| Category | fee-balance | password-reset-expiry | Total |
|---|---|---|---|
| Blind single-shot (fresh Claude CLI process, no tools, no repository access) | 8 / 8 | 7 / 7 | **15 / 15** |
| Agentic tool loop (list, read, run tests, propose patch) | 5 / 5 | 7 / 8 | **12 / 13** |
| **All model attempts** | 13 / 13 | 14 / 15 | **27 / 28** |

Per model, passes over attempts: blind sonnet 7/7, opus 4/4, haiku 4/4; agentic sonnet 6/6, opus 2/2,
haiku 4/5.

**Failure kept:** the agentic haiku attempt on password-reset (`haiku-run1`) produced no patch. It is
recorded as a failure.

**Haiku on password-reset:** one original failure, then three retries. The retries ran only after
that failure, so they are not an independent sample. Four attempts, three passes.

## What is not counted here

- Human-authored repair (the owner's upstream commit for password-reset).
- The operator-authored scripted fixture for fee-balance.
- The in-session AI-assisted patch for fee-balance (written in an interactive session, not blind).
- Harness self-checks (a known-good patch and a comment-only negative control).
- Infrastructure stops (sandbox process cap, path mistakes): not model attempts.
- The external real-bug pilot runs (Gemini, Groq, OpenRouter). They cover different cases and a
  different harness, and are reported in the published pilot results.

## Correction

The earlier figures were 14/14 blind, 9/10 then 12/13 agentic, and 24 trials with 23 passes. The blind
count omitted one passing run, and the agentic total predated the retries. The per-attempt file is
[reliability-accounting-2026-11-17-corrected.json](reliability-accounting-2026-11-17-corrected.json)
(SHA-256 `04cd9f356e5d895e88d3b4ba23d9e9bfc025bf17b2e96aa8215e2cd0d2af233d`).

## Limits

Two frozen cases and 28 attempts. These numbers do not estimate reliability on other bugs, and they do
not establish that a model finds the fix without prior exposure.
