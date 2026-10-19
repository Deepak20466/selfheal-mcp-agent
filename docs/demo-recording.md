# Record the OpenRouter repair-to-PR demonstration

This demo records the GitHub Actions workflow in
[openrouter-repair.md](openrouter-repair.md). It does not use the provider CLI,
a mock response, or a prerecorded transcript. The workflow only opens a repair
PR; it never merges.

## Run only after the gates pass

1. The implementation PR is reviewed and merged, and the default-branch CI run
   passes the Bubblewrap network, filesystem, secret, and resource-limit probes.
2. The owner explicitly authorizes three disposable public repositories.
3. Each repository is allowlisted, has Actions enabled, and is configured to
   let the job-scoped GITHUB_TOKEN create PRs. Leave automatic merging off.
4. The owner adds OPENROUTER_API_KEY through each disposable repository's
   Settings > Secrets and variables > Actions page. Do this only after the
   security checks pass and the repositories are authorized. Never put the key
   in chat, source, variables, command arguments, or the recording.
5. Set SELFHEAL_REPAIR_ENABLED=true and OPENROUTER_FREE_MODEL to an available
   model ID ending in :free. The inference controller checks the key's current
   free-model quota, the live catalog price, and the returned model and cost.
   If any check fails, it stops without a repair PR.

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
3. Show the exact :free model ID, zero-price catalog preflight, and generated
   patch artifact. Keep credentials and account details off-screen.
4. Show the separate test job on its own Linux runner, including passing
   positive/negative probes for network, filesystem, read-only tests, and
   secret separation. Show the fixed regression command and nonzero test count.
5. Show the trusted PR job rechecking the patch hash and creating the repair PR.
   Leave it open for human review.
6. Show the repair PR's normal CI passing. Capture the CI run URL and PR URL.

If the first candidate fails its regression suite, record the single correction
attempt and the final passing result. Rate-limit and inference errors are not
retried. Do not hide failures, substitute fixture output, or merge the repair.

## Recording hygiene

Use Windows Snipping Tool with Windows+Shift+R or another screen recorder.
Capture real Actions pages and terminal output, disable notifications, and close
unrelated tabs. Do not show API-key settings, secrets, personal email, billing
pages, unrelated repositories, or local paths. Review the export frame by
frame before sharing it.

## Current evidence status

No model completion has been requested, no key has been configured, no live
repair run or recording exists, and no disposable test repositories have been
created. The workflow is disabled. The hosted Linux sandbox probes and fresh
GitHub Actions checks are still required before the owner is asked to authorize
the three repositories.
