# Provider-independent repair workflow

The controller in [`scripts/openrouter_repair.py`](../scripts/openrouter_repair.py)
uses a shared prompt, structured response parser, strict patch policy, and
`RepairProvider` adapter contract. The workflow in
[`.github/workflows/repair-pr.yml`](../.github/workflows/repair-pr.yml) reads
untrusted source and bounded CI logs, generates a candidate, tests it in an
offline sandbox, and opens a review PR. It never merges.

## Provider order and free-only gates

1. **Google Gemini API:** use `gemini-3.8-flash` first when its AI Studio
   project quota was just confirmed positive, followed by
   `gemini-3.5-flash-lite`, then any separately confirmed-free legacy Gemini
   fallback (`gemini-2.5-pro`, `gemini-2.5-flash`, or
   `gemini-2.5-flash-lite`). Gemini model
   inaccessibility and free quota/rate-limit responses skip only that model.
   Before inference,
   the controller requires the owner-set free-project and model-quota flags
   and checks the live model list for `generateContent` support. It does not
   send an inference request to a model whose quota flag is false.
2. **Groq GPT-OSS 120B:** optional. It is considered only when the dedicated
   key is present and `GROQ_FREE_PLAN_CONFIRMED=true` plus
   `GROQ_GPT_OSS_120B_FREE_QUOTA=true`. It checks the live model list and uses
   the on-demand API with JSON Schema output and no tools. The flag must only
   be set after the Groq account dashboard confirms the Free plan, positive
   quota, and no paid billing. The Groq [model
   page](https://console.groq.com/docs/model/openai/gpt-oss-120b) lists a token
   price for this model on its Developer plan, so do not enable this adapter
   on a billed account.
3. **OpenRouter:** retained as a final optional route. The controller verifies
   a regular Free-tier key, the exact configured `:free` model, an active
   endpoint with zero input/output prices, and structured-output support. It
   disables provider routing fallbacks and discards any completion unless
   OpenRouter reports the exact model, zero cost, and `is_byok=false`.

Google's [model guide](https://ai.google.dev/gemini-api/docs/models) lists
`gemini-3.8-flash` and `gemini-3.5-flash-lite` as supported exact API model
IDs. Its [pricing page](https://ai.google.dev/gemini-api/docs/pricing) lists
Free Tier input/output for both. The model guide currently limits 2.5 model
access to projects with prior active use. The [rate-limit
guide](https://ai.google.dev/gemini-api/docs/rate-limits) says limits are
project-specific and visible in AI Studio; an API-key-only caller cannot query
the remaining quota. For that reason, the workflow requires an operator check
immediately before each trial. Use a Google Cloud
project with billing disabled and no purchased/prepaid credits; that is the
cost boundary that prevents a request from switching to paid billing. In
Google AI Studio, confirm the project says **Free Tier** and that the desired
model has positive quota. Google applies limits per project, across its keys.

Set these repository **variables** in each disposable target repository after
checking the project:

- `GEMINI_FREE_TIER_CONFIRMED=true` only for a Free Tier project with billing
  disabled and no purchased credits.
- `GEMINI_QUOTA_CHECKED_UTC=YYYY-MM-DDTHH:MM:SSZ` with the current UTC time
  immediately after checking AI Studio. The controller rejects a missing,
  malformed, future, or more than 15-minute-old confirmation.
- `GEMINI_25_PRO_FREE_QUOTA=true` only when the AI Studio view shows usable
  free quota for `gemini-2.5-pro`; otherwise set it to `false`.
- `GEMINI_25_FLASH_FREE_QUOTA=true` only when the AI Studio view shows usable
  free quota for `gemini-2.5-flash`; otherwise set it to `false`.
- `GEMINI_38_FLASH_FREE_QUOTA=true` only when the AI Studio view shows usable
  free quota for `gemini-3.8-flash`; otherwise set it to `false`.
- `GEMINI_35_FLASH_LITE_FREE_QUOTA=true` only when the AI Studio view shows
  usable free quota for `gemini-3.5-flash-lite`; otherwise set it to `false`.
- `GEMINI_25_FLASH_LITE_FREE_QUOTA=true` only when the AI Studio view shows
  usable free quota for `gemini-2.5-flash-lite`; otherwise set it to `false`.

The model flags are a fresh operator confirmation, not a claim that Google
exposes remaining quota through the API. If a model returns a quota, access, or
not-found response, the controller moves only to the next separately
confirmed-free model. With billing disabled, the call cannot use a paid Gemini
tier. It stops safely if no confirmed-free model is available. Update the
timestamp immediately before triggering each repair trial; it is passed only
to the inference jobs and expires after 15 minutes.

## Private credentials

The inference job runs inside each target repository, so add
`GEMINI_API_KEY` as a **repository Actions secret** in both
`Deepak20466/selfheal-e2e-demo-02` and
`Deepak20466/selfheal-e2e-demo-03`:

**Repository → Settings → Secrets and variables → Actions → New repository
secret**

Add it after the reviewed controller update is on that repository's default
branch and immediately before its authorized trial. Do not put it in chat,
source, a workflow file, a variable, command arguments, or the selfheal source
repository. The workflow maps provider secrets only to `infer-initial` and
`infer-retry`; neither isolated test job nor the PR creation job receives a
provider key. Never add the key to demo-01 for these Gemini trials.

Groq credentials, if separately approved and verified, use the same private
Actions secret mechanism with the name `GROQ_API_KEY`. `OPENROUTER_API_KEY`
continues to be a target-repository Actions secret. Do not add either key to
untrusted test jobs.

## Output, retries, and isolation

Each provider receives the same bounded, scrubbed source and failure text and
has no tools. Gemini, Groq, and OpenRouter are asked for JSON Schema output
with exactly two string properties: `diagnosis` and `patch`. A local parser
rejects duplicate, missing, extra, or wrongly typed fields before patch
validation. The diagnosis and selected model are recorded in a short-lived
artifact without storing raw provider output.

The controller corrects a malformed structured response or a stale/inapplicable
patch at most once. For stale hunks, it adds exact numbered lines from the
selected commit as untrusted context. Security-policy rejections are terminal.
The workflow still permits at most one separate regression-test correction.
All candidates continue through the existing strict path, secret, hunk,
size, and changed-line checks. A separate GitHub-hosted VM applies the
validated candidate to a disposable snapshot and runs tests with Bubblewrap,
no network, no provider key, no write-capable token, and read-only test files.
The PR job revalidates the tested digest and patch policy and requires normal
CI and human review.

Only public synthetic fixture data is sent to providers. Google's pricing
documentation says Free Tier inputs and outputs may be used to improve Google
products; do not send private or sensitive code.

## Retained trial evidence and current blockers

- **demo-01 remains successful evidence:** failed baseline
  [CI run 39344920267](https://github.com/Deepak20466/selfheal-e2e-demo-01/actions/runs/39344920267),
  repair workflow
  [secure Gemini repair run 39377817751](https://github.com/Deepak20466/selfheal-e2e-demo-01/actions/runs/39377817751),
  valid review [PR #16](https://github.com/Deepak20466/selfheal-e2e-demo-01/pull/16),
  and passing PR [CI run 39377964370](https://github.com/Deepak20466/selfheal-e2e-demo-01/actions/runs/39377964370).
  The earlier comment-only Demo-01 PR #11 was invalid, is closed, and is not
  evidence of a repair.
- **demo-02 remains an intentionally failing fixture:** seed
  [PR #4](https://github.com/Deepak20466/selfheal-e2e-demo-02/pull/4).
  Its current OpenRouter attempt
  [run 39351221640](https://github.com/Deepak20466/selfheal-e2e-demo-02/actions/runs/39351221640)
  stopped before inference because no safe free-quota retry window was
  available.
- **demo-03 remains an intentionally failing fixture:** seed
  [PR #4](https://github.com/Deepak20466/selfheal-e2e-demo-03/pull/4).
  Its current OpenRouter attempt
  [run 39351224824](https://github.com/Deepak20466/selfheal-e2e-demo-03/actions/runs/39351224824)
  generated a patch that did not apply to the selected commit.

The current Gemini credential is not present in either target repository's
Actions secret list, and this controller change has not yet run against Gemini.
Those two trials and the original three-trial acceptance check remain pending.
