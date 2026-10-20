# Record the provider-independent repair-to-PR trials

This demo records the GitHub Actions workflow in
[openrouter-repair.md](openrouter-repair.md). It uses a real provider response,
not a mock or prerecorded transcript. The workflow only opens a repair PR; it
never merges.

## Run only after the gates pass

1. The implementation PR is reviewed and merged, and the default-branch CI run
   passes the Bubblewrap network, filesystem, secret, and resource-limit probes.
2. The owner explicitly authorizes three disposable public repositories.
3. Each repository is allowlisted, has Actions enabled, and is configured to
   let the job-scoped GITHUB_TOKEN create PRs. Protect the default branch with
   PR-only changes and required CI checks. For this solo-maintainer demo, set
   the required approval count to zero because GitHub will not accept an
   author's approval of their own PR. The owner must still inspect each repair
   PR and explicitly authorize any merge; automatic merging stays off.
4. Copy the reviewed controller and workflow to each target's default branch.
   Add the exact repository allowlist entry and set
   `SELFHEAL_REPAIR_ENABLED=true`. Immediately before each Gemini trial, check
   AI Studio: use a Free Tier project with billing disabled and no purchased
   credits, then verify positive free quota for each model you may use. Prefer
   Gemini 3.8 Flash, with Gemini 3.5 Flash-Lite as a backup when both show
   positive free quota. Set the `GEMINI_*` quota/free-project variables described in
   [provider setup](openrouter-repair.md).
5. Add `GEMINI_API_KEY` privately at the target repository's **Settings >
   Secrets and variables > Actions > New repository secret** page. Never put
   the key in chat, source, variables, command arguments, or the recording.
   Only inference jobs receive it. Google does not expose remaining
   project-specific quota through its API-key API, so the fresh AI Studio check
   and billing-disabled project are required. Gemini candidates are tried in
   preference order only when their own free quota was confirmed.

Use only synthetic code and CI output in these public repositories. OpenRouter
transmits prompts to the selected model provider; some free providers may retain
inputs or use them for training. Do not send private code, personal data, or
secrets.

## Record the real run

For each repository, show the complete timeline in GitHub Actions:

1. Push a deliberately failing source change and regression test to a branch.
   Capture the failing CI run URL and commit SHA.
2. Show the repair workflow rejecting unauthorized origins and confirming the
   same-repository public allowlist.
3. Show the Gemini model selection and free-project/quota preflight, plus the
   structured diagnosis and patch artifacts. Keep credentials and account
   details off-screen. If Gemini has no free quota, record the stop or the
   separately verified free backup that was selected.
4. Show the separate test job on its own Linux runner, including passing
   positive/negative probes for network, filesystem, read-only tests, and
   secret separation. Show the fixed regression command and nonzero test count.
5. Show the trusted PR job rechecking the patch hash and creating the repair PR.
   Leave it open for human review.
6. If GitHub holds Actions runs from a `GITHUB_TOKEN`-created PR for approval,
   have the owner approve the workflow run. Then show the repair PR's normal CI
   passing. Capture the CI run URL and PR URL.

If a response is malformed or its hunk is stale, record the one bounded model
correction with exact selected-commit source context. If the first accepted
candidate fails regression testing, record the workflow's single regression
correction. Provider errors are not retried through a paid route. Do not hide
failures, substitute fixture output, or merge the repair.

## Recording hygiene

Use Windows Snipping Tool with Windows+Shift+R or another screen recorder.
Capture real Actions pages and terminal output, disable notifications, and close
unrelated tabs. Do not show API-key settings, secrets, personal email, billing
pages, unrelated repositories, or local paths. Review the export frame by
frame before sharing it.

## Current evidence status

The successful demo-01 evidence is retained: failed baseline
[CI run 39344920267](https://github.com/Deepak20466/selfheal-e2e-demo-01/actions/runs/39344920267),
repair workflow
[run 39344936966](https://github.com/Deepak20466/selfheal-e2e-demo-01/actions/runs/39344936966),
open repair [PR #11](https://github.com/Deepak20466/selfheal-e2e-demo-01/pull/11),
and passing PR
[CI run 39345114328](https://github.com/Deepak20466/selfheal-e2e-demo-01/actions/runs/39345114328).
Demo-02's seed is still open at
[PR #4](https://github.com/Deepak20466/selfheal-e2e-demo-02/pull/4); its latest
OpenRouter inference stopped on free quota at
[run 39351221640](https://github.com/Deepak20466/selfheal-e2e-demo-02/actions/runs/39351221640).
Demo-03's seed is still open at
[PR #4](https://github.com/Deepak20466/selfheal-e2e-demo-03/pull/4); its latest
OpenRouter attempt produced a stale patch at
[run 39351224824](https://github.com/Deepak20466/selfheal-e2e-demo-03/actions/runs/39351224824).
The Gemini Actions secret and Free Tier attestation are now configured in both
target repositories. An earlier Gemini 2.5 attempt stopped safely when Google
returned HTTP 404. The updated controller now prioritizes Gemini 3.8 Flash and
uses Gemini 3.5 Flash-Lite as the next separately quota-confirmed candidate.
That controller update and fresh quota checks must reach each target before
the two real Gemini trials; neither target currently counts as a successful
Gemini trial. The three-trial acceptance check remains incomplete.
