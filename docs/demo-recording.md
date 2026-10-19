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
   let the job-scoped GITHUB_TOKEN create PRs. Protect the default branch with
   PR-only changes and required CI checks. For this solo-maintainer demo, set
   the required approval count to zero because GitHub will not accept an
   author's approval of their own PR. The owner must still inspect each repair
   PR and explicitly authorize any merge; automatic merging stays off.
4. Copy the reviewed controller and workflow to the default branch. Add the
   exact repository allowlist entry and set `SELFHEAL_REPAIR_ENABLED=true` plus
   `OPENROUTER_FREE_MODEL` to a currently listed zero-price `:free` model.
   Confirm the secret list is still empty before enabling the deliberately
   failing trial.
5. Immediately before that repository's trial, add the replacement
   `OPENROUTER_API_KEY` through its Settings > Secrets and variables > Actions
   page. Never put the key in chat, source, variables, command arguments, or the
   recording. The inference controller checks documented key Free-tier
   metadata, the live catalog price, and the returned model and cost. OpenRouter
   does not expose remaining `:free` request count through its documented key
   endpoint; a 429 or provider error stops the run without retry or a repair
   PR.

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
6. If GitHub holds Actions runs from a `GITHUB_TOKEN`-created PR for approval,
   have the owner approve the workflow run. Then show the repair PR's normal CI
   passing. Capture the CI run URL and PR URL.

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

The hosted Linux sandbox probes, full CI suite, database recovery checks, and
Kubernetes checks passed on implementation commit e2795fb. The correction to
OpenRouter key metadata handling passed hosted [push CI](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39319431016),
[PR CI](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39319439813),
and [Kubernetes checks](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39319439788).
Three authorized public fixtures now exist: [demo 01](https://github.com/Deepak20466/selfheal-e2e-demo-01),
[demo 02](https://github.com/Deepak20466/selfheal-e2e-demo-02), and
[demo 03](https://github.com/Deepak20466/selfheal-e2e-demo-03). Their initial
CI passed; their `main` branches require passing `test` CI, enforce PR-only
changes, and block force pushes and deletion. Required approval count is zero
for the sole maintainer; the owner must manually review and authorize merges.
Actions has read-only default workflow permissions, only GitHub-owned
SHA-pinned actions allowed, and the narrowly scoped PR-creation setting enabled.
The selected `:free` model variable is set but the repair enablement variable is
`false`. No controller workflow or OpenRouter secret is installed in these
fixtures yet. No model completion or live repair run has occurred. The source
PR stack still needs the owner's manual review and explicit merge authorization
before the reviewed controller can be copied into the fixtures.
