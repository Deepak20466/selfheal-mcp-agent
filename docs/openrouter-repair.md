# OpenRouter free-model repair workflow

This is an implementation guide and status record for
[`.github/workflows/repair-pr.yml`](../.github/workflows/repair-pr.yml). It
opens a review PR after a failed `CI` run. It never merges a repair. Codex,
Claude Code, and Gemini CLI execution remain fail-closed and are not used by
this workflow.

## Scope and boundaries

- Only public repositories named in `repair-allowed-repositories.txt` are
  accepted. Fork-originated CI failures and repair branches are rejected.
- The initial target is Python source with a standard-library `unittest` suite
  under `tests/`. The workflow does not install target dependencies.
- The inference job runs controller code from the repository default branch.
  It reads source and bounded CI logs as untrusted text; it does not import or
  execute the target repository. The model has no tools.
- The inference request uses one currently listed `:free` model with zero
  prompt and completion prices. Provider fallback is disabled. A failed API
  request is not retried; one additional completion is permitted only when the
  first generated patch fails the offline regression suite. Before a
  completion, the controller checks that the API key is a non-management
  OpenRouter Free-tier key with remaining free-model quota, then checks the
  live model catalog. It asks OpenRouter to return usage accounting and
  discards responses unless they confirm no BYOK route and exactly zero cost.
- The patch validator accepts at most three existing `.py` implementation
  files and 200 changed lines. It rejects test/config/workflow paths, unsafe or
  mismatched diff headers, symlinks, binary patches, credential references,
  common code-execution/network primitives, and artifacts over 64 KiB.
- Each test attempt uses a new GitHub-hosted Linux VM. Only the trusted
  controller applies the patch; repository tests run from a disposable snapshot
  with Bubblewrap, no network namespace access, a read-only test suite, a
  filtered environment, no provider key, no write-capable GitHub token,
  resource limits, and cleanup.
- The final job rechecks the artifact digest and patch policy, then uses
  `GITHUB_TOKEN` to push a repair branch and open a PR. It does not execute
  repository code. Human review and ordinary CI remain required.

OpenRouter currently documents Free-tier limits of 20 requests per minute and
50 requests per day. Limits and free model availability can change, and
requests made elsewhere on the same account share that capacity. Each failed
CI run can make at most two completion requests. When free capacity is
unavailable, the workflow stops; it never selects a paid model, changes to API
key billing, or purchases credits. The model catalog check and response-model
check both require the selected `:free` ID and zero advertised prices. A live,
unauthenticated catalog read on 2026-10-20 returned multiple `:free` models at
zero prompt and completion prices. That read did not invoke a model and does
not verify any particular account key or provider availability at call time.

Only public repository source and bounded, scrubbed CI output are sent to the
selected provider. OpenRouter says it does not train on inputs itself, but its
privacy notice says upstream model providers have different retention and
training practices. Use synthetic data for the public demonstration; do not
send private code or secrets.

## Required configuration

The workflow is dormant until the reviewed implementation is on the target
repository's default branch and its security checks pass. It also requires:

1. Add the exact public `owner/repository` to
   `repair-allowed-repositories.txt` in the default branch.
2. Add repository variable `SELFHEAL_REPAIR_ENABLED` with value `true`.
3. Add repository variable `OPENROUTER_FREE_MODEL` with a model ID ending in
   `:free`. Each inference run verifies the ID and its zero prices against
   OpenRouter's current public model catalog.
4. **Only after steps 1–3 and the hosted security checks pass, and immediately
   before the authorized disposable-repository trial, add the key as a
   repository Actions secret named `OPENROUTER_API_KEY`.** Create/use an
   OpenRouter Free account without buying credits or enabling automatic
   top-ups. Add the secret only to the authorized disposable repositories, not
   to the main project or fork workflows. Never paste the key into chat,
   workflow files, command arguments, or repository variables.

If the key is missing or malformed, is not reported as a regular Free-tier key,
has no verifiable remaining free quota, the configured model is
absent/paid/unavailable, or the response omits the exact zero cost or indicates
BYOK, inference stops without generating an artifact.

The current source repository has GitHub Actions enabled, public standard
Ubuntu runners, default workflow-token permissions set to read, and the setting
that allows Actions to create or approve pull requests disabled. The repair PR
job requests narrowly scoped write permissions, but a repository owner must
enable the create/approve setting for a live disposable-repository trial.
The workflow itself never approves or merges a PR.

## Recording a real demonstration

After the implementation is reviewed and merged, the owner can authorize three
disposable public repositories. Seed each with a small Python source bug and a
failing `unittest`, copy the reviewed controller/workflow and allowlist entry,
then configure the three repository variables and `OPENROUTER_API_KEY` secret
as described above. Push the intentionally failing baseline. Record the
following GitHub Actions sequence for each repository:

1. The baseline `CI` run fails and its run ID is captured.
2. `Secure free-model repair PR` validates the same-repository public target.
3. The inference job verifies the live `:free` catalog entry, records the
   selected model ID, and uploads a validated patch artifact.
4. A separate VM validates the artifact, passes the network/filesystem/secret
   boundary probes, and runs the regression suite offline.
5. If the first patch fails its regression suite, show the single bounded retry
   and the passing retry. No API/rate-limit error is retried.
6. The trusted PR job verifies the tested patch hash and opens a PR. Show that
   it remains unmerged, then show the normal PR CI passing.

Do not use fixtures or prerecorded model output as evidence. A passing unit
test of the controller is not a live model demonstration. Preserve the three
failed-run URLs, repair workflow run URLs, PR URLs, passing-CI run URLs, selected
model IDs, and per-account OpenRouter usage view with secrets and personal
details redacted.

## Current status

The workflow, validators, quota and pricing preflights, retry controller, and
security probes are implemented locally. Focused controller and workflow tests
pass on Windows; platform-specific Linux isolation checks skip here. The actual
Bubblewrap network and filesystem probes still need to pass on a GitHub-hosted
Linux runner. No OpenRouter key has been configured, no model completion has
been requested, and no repair-to-PR demonstration has run. The workflow must
remain disabled until fresh hosted checks pass and the review/merge gates are
satisfied.
