# VIMJ fee-balance repair evidence

**Scope:** one frozen VIMJ fee-balance case, executed in an isolated local checkout. This is not a
corpus benchmark, production repair, or complete autonomous repair-to-pull-request demonstration.

**Demo video:** [1080p MP4 (2:08)](https://github.com/Deepak20466/selfheal-mcp-agent/releases/download/portfolio-demo-vimj-2026-11-10/selfheal-mcp-agent-demo.mp4)
  SHA-256: `EF775A288B4601AB288D9E1FE879A31A2858C3182D4D2B0F2F5CE42CFCFD6FE4`.

## Outcome

The original regression observed an unpaid-fee balance of **INR 125 instead of INR 175** after an
amount update. One authorized inference used OpenRouter model
`cohere/north-mini-code:free`. The provider reported **$0.00** cost. The saved preflight recorded a
free model and zero-credit key; billing-disabled and key-restriction settings were operator-attested,
not independently attested by the provider. The run records one inference request and no retry.

The response contained a code change, but raw patch validation returned
`patch must modify one to three distinct source files`: the code hunk did not include parseable
unified-diff file headers or hunk coordinates. Recovery added exact file
headers and recalculated hunk location/counts from unique unchanged source context at line 155. The
AI-authored code bytes were preserved. The original response and normalized patch are published as
[AI response](vimj-fee-balance-2026-11-10-ai-response.txt) and
[normalized patch](vimj-fee-balance-2026-11-10.patch).

The saved post-repair regression log reports **1 passed** in 1.01 seconds. The saved complete VIMJ
backend test log reports **30 passed** in 10.29 seconds. No tests were independently rerun while
preparing this documentation. The recorded repair used an isolated SQLite database; no production
VIMJ data was accessed. The evidence contains no VIMJ commit or pull request.

## Integrity references

These SHA-256 values were checked against the saved local run artifacts. Only the response and
normalized patch are published; the prompt, raw provider payload, metadata JSON, and full test logs
remain local to avoid publishing request internals, account attestations, and environment-specific
paths. The log hashes below identify the reviewed records without exposing those logs.

| Artifact | SHA-256 |
|---|---|
| Original AI response | `463b0ce3f4de1df1536eabc4afabcad10e3c8c0afa60690c14065712c39d950d` |
| Normalized patch | `06fac2c8fe4d80b665cb1864b3ddfa12486d0848dd051182dd31f835256af68d` |
| Frozen VIMJ source | `6d131b6b91d079f61af2d78b1e80e54ab9512093951b8caae0ff5a4735d09d28` |
| Repaired VIMJ source | `1a229033947c9822bdbfa4d0861c70dbae8fce621dcf769d673a8bd06c2b2a68` |
| Frozen regression test | `1bd8dcbec1edfa404723df1ecbfc496dc14eabfcbbcd37a814e90733f447a007` |
| Saved regression log | `23bbecdacb297a3955f2774cf0c5d9e12f0b74302146a01d36451ccfa3d2c1bb` |
| Saved backend benchmark log | `c8eedbe575884e3da94b74adeced5d568508b5543d1bea78c6904d08001387c4` |

The saved recovery metadata identifies the frozen baseline commit as
`6490833d178b71624d024b16046240a20a3f78a6`. The frozen test hash is unchanged in the recovery
record. The one-case results do not measure corpus resolve rate, false-positive rate, or general
repair accuracy.
