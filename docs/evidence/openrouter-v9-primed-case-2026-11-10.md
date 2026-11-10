# OpenRouter v9 primed controlled case

**Date:** 2026-11-10 UTC
**Outcome:** Failed; the agent proposed no patch, so no repair is claimed.

## Run result

One `with_validation` attempt ran frozen case `attrs-73dc0d2e2860` using the previously
verified zero-priced `cohere/north-mini-code:free` route and the disposable benchmark database.
It used 2 provider completion requests, 4,417 input tokens, 356 output tokens (4,773 total),
and provider-reported cost of **$0.00**. The provider had been checked for the exact free model
and the adapter was configured without fallbacks before this run.

The agent proposed no patch (`failure_category: no_patch`); therefore, nothing was applied. The
frozen regression remained failing, and the post-repair full suite was not run. The runner's
independent pre-run baseline confirmed the regression failed before the model attempt.

## Trace and diagnosis

The first model completion called `get_error` and then `read_file` with
`rb-<attempt>/s/src/attr/validators.py`. MCP inspection paths must be repository-relative; the extra
worktree/checkout prefix made that path invalid. The second completion tried `list_files` rather
than running the supplied regression or proposing a patch. The runner's inspection limit blocked
that call. Thus, the request/token loop was curtailed, but the model still spent both completions
on inspection and did not attempt repair. This is not evidence of an incorrect patch or a verified
regression failure after a patch.

## Offline workflow changes

The benchmark now puts the frozen failing test, observed baseline failure, a concise import map,
and bounded source excerpts into the first model message, built only from the prepared parent
checkout. It also limits supplemental inspection to one call, includes explicit repository-relative
path guidance in both the context and prompt, and asks the model to run the regression before
proposing a source-only unified diff. Patch application and target/full-suite verification remain
required for success. Frozen case data and tests were not modified.

The focused offline suite passed **135 tests, 2 skipped** using `pytest --noconftest`; this avoids
the repository-wide session hook that provisions a separate test database. The standard pytest
invocation could not start because its configured PostgreSQL test endpoint refused the connection.
Ruff and `git diff --check` passed. No database-backed suite result is claimed.

## Evidence

- [Raw attempt and tool trace](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-primed-1case-20261110/attempts.jsonl)
- [Scorer JSON](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-primed-1case-20261110/scorer.json)
- [Scorer Markdown](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-primed-1case-20261110/2026-11-10.md)
- [Prior controlled attempt](openrouter-v9-controlled-case-2026-11-10.md)
- [Earlier pilot results](openrouter-v9-pilot-results-2026-11-10.md)
