# OpenRouter v9 controlled real-bug case

**Date:** 2026-11-10 UTC

**Outcome:** Failed before repair verification; no repair is claimed.

## Controlled run

One `with_validation` arm was run for frozen case `attrs-73dc0d2e2860` using the verified
zero-priced `cohere/north-mini-code:free` route, on the disposable PostgreSQL database. This was
a separate result file; the earlier three-attempt pilot and all Gemini/VIMJ records remain
unchanged.

| Measure | Observed |
|---|---:|
| Arm attempts | 1 |
| Repair verified | No |
| Post-patch target-test passes | 0 |
| Full-suite passes | 0 |
| OpenRouter requests | 10 |
| Input tokens | 40,172 |
| Output tokens | 888 |
| Total reported tokens | 41,060 |
| Provider-reported cost | $0.00 |

The completion usage reported exact zero cost. The benchmark's 40,000-token aggregate case guard
stopped the attempt. The model made ten requests, all for `read_file`; three tool calls had invalid
`start_line` values encoded as two-element arrays. It did not reach `propose_patch` or the
in-conversation regression-test verification. The runner had independently confirmed the frozen
target test failed before handing the case to the model.

## What the saved evidence shows

The earlier three-attempt pilot explains the separate failure modes:

- **Request cap:** the initial `with_validation` arm stopped at the then-configured ten-request
  per-arm guard while spending calls on repeated source inspection, including malformed
  `read_file` arguments. This was the benchmark's safety cap, not evidence that the provider's
  daily free quota was exhausted.
- **Unappliable patch:** the `without_validation` arm proposed a diff containing an unexpected
  `<function call>` line and a hunk that did not apply to `src/attr/_config.py`. Its direct
  `git apply` path raised a raw exception instead of a recoverable MCP tool error, so the model
  could not use the application error to correct the patch.
- **40,000-token guard:** provider-reported input tokens are summed across completions. Each new
  completion includes prior conversation/tool history, so repeated source reads increase aggregate
  usage even when the outgoing adapter compacts an individual large file result. The earlier
  `attrs-2772a29b7bf2` attempt reached 46,139 input tokens in nine requests and stopped at the
  per-case guard.

## Workflow changes and verification

Offline changes now return baseline-ablation patch errors as recoverable tool results, ask for a
strict raw unified diff and one correction after an apply error, compact prior identical source
reads on the OpenRouter outgoing copy, normalize the observed two-number `read_file.start_line`
shape when `end_line` is absent, and stop repeated failed or inspection-only tool loops early.
The existing fail-before/pass-after regression check and full-suite verification remain required.
The frozen case list, its tests, and all prior results are unchanged.

The first offline suite passed **131 tests, 2 skipped** before the controlled request. After using
that request's actual trace to add the range normalization and a four-turn inspection-only limit,
the final offline suite passed **133 tests, 2 skipped**. Those last two workflow refinements were
not exercised by another live request; the single controlled result above is a failure, not a
success claim.

- [Raw controlled attempt](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-controlled-1case-20261110/attempts.jsonl)
- [Scorer JSON](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-controlled-1case-20261110/2026-11-10.json)
- [Scorer Markdown](../../benchmarks/real_bugs/results/published/openrouter-north-mini-code-v9-controlled-1case-20261110/2026-11-10.md)
- [Earlier three-attempt OpenRouter pilot](openrouter-v9-pilot-results-2026-11-10.md)
