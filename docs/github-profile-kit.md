# GitHub profile kit

## Repository description

An AI-assisted runtime and CI repair agent that verifies candidate patches before opening PRs.

## Topics

`ai-agents` · `self-healing` · `developer-tools` · `mcp` · `ci-cd` · `python` · `automated-testing` · `observability`

## Resume bullets

- Built Selfheal's runtime/CI incident-to-review workflow around FastAPI, PostgreSQL, MCP tools, scoped repository access, patch checks, isolated verification, and a human-reviewed PR boundary. The 41-case cohort and original 10-case selection pass leakage audits. Five Gemini attempts across two selected bugs failed before repair; a separate Groq v3 diagnostic made one successful API call, then stopped locally at the TPM budget. No real-bug repair completed; eight Gemini pilot IDs remain unattempted and no success-rate claim is supported. See the [measured report](benchmark-real-bugs.md).
- Fixed two Selfheal defects found during engineer-directed, Claude-assisted dogfooding: a scanner path dropped dependency findings when stderr corrupted JSON, and a worker path failed to catch a remote-verification exception. Added two scanner tests and one worker regression test in [`ecba78b`](https://github.com/Deepak20466/selfheal-mcp-agent/commit/ecba78b90773d965d5218dce5688fb9cef6ef503) and [`c402003`](https://github.com/Deepak20466/selfheal-mcp-agent/commit/c402003ea41b7e2d8e5283a8954519e412c314d6); these were not Selfheal-generated repairs.
- Ran three controlled synthetic repair-to-PR demonstrations with passing repair CI; as of 2026-11-08, demo-01 PR #16 is merged and demo-02 #13 / demo-03 #10 remain open. VIMJ PR #1 separately contains a Codex-assisted SQLite password-reset fix with passing backend CI; it is not a Selfheal repair. The 31-turn PyPDF2 repair did not converge, and ShopCart's pagination fix was manual. See the [evidence ledger](history/RECOVERED_EVIDENCE.md).

## Interview explanation: 60 seconds

Selfheal is a Python system that receives runtime and CI failures, gathers scoped repository context
through MCP tools, and asks a configured model to propose a patch. It checks allowed write paths and
patch policy, then runs verification in an isolated worktree before it can open a review PR. The
workflow is backed by CI and controlled synthetic demos. The separate Gemini pilot has five
recorded attempts across two bugs: two timed out and three were rejected because the adapter did not
preserve signed function-call Parts. Separately, Groq v1/v2 recorded token/context failures; v3 made
one successful API call before a local TPM-budget stop. No repair completed. I report those partial
results and make no successful real-bug repair-rate claim.

GroqCloud has an offline-tested adapter path and separate frozen configurations using the same
41-case cohort and original ten pilot IDs. The preserved v1/v2 diagnostics recorded a TPM 429 and
request-length HTTP 400; neither completed a repair. V3 made one successful API call, then stopped
locally because the conversation estimate exceeded remaining TPM. V4 adds a bounded server-reset
wait; it is offline-tested but unattempted. The v3 diagnostic took 2.438 seconds, used 1,594
reported tokens, and had unknown cost; it did not repair the bug. See the [GroqCloud runbook](groqcloud-repair.md).

The clearest lesson came from dogfooding against `multi-agent-workspace`: it exposed a scanner bug
that hid dependency findings and a worker exception path that could stop processing. I fixed both
with engineer-directed, Claude-assisted changes and regression tests; neither came from Selfheal's
repair loop. Its PyPDF2 repair attempt did not converge. ShopCart's onboarding PR also needed a
manual repair before CI ran, and its pagination bug was fixed manually (engineer-directed,
Claude-assisted) in PR #2. VIMJ PR #1 is a separate Codex-assisted repair, not a Selfheal result.
The [verification report](../VERIFICATION.md) and
[recovered evidence](history/RECOVERED_EVIDENCE.md) link to the records.

## Interview explanation: 2 minutes

Selfheal connects application telemetry and CI failures to a repair workflow. Sentinel records the
failure, the worker uses the configured backend, and MCP tools provide repository context and
bounded patch/test operations. Candidate patches are checked for allowed paths and policy before
tests run in an isolated worktree. When the configured verification succeeds, the system can create
a GitHub PR for review.

I assessed the workflow through its offline and hosted CI, three controlled synthetic repair-to-PR
trials, and connected-repository dogfooding. The three synthetic repair PR checks passed; one PR is
merged and two are still open. Dogfooding exposed two bugs in Selfheal itself: dependency-audit
results were dropped when stderr contaminated JSON, and an uncaught GitHub exception could stop a
worker. Both were fixed with engineer-directed, Claude-assisted changes and regression tests.

The external repair evidence is mixed. A 31-turn PyPDF2 repair attempt did not produce a working
diff. A separate manual dependency-floor PR for `cryptography` was merged, but its required CI
failed at an unrelated setup step because `psycopg` was missing. ShopCart's previous-page bug was
fixed manually in PR #2; its onboarding PR needed a manual repair before CI could run. The lesson is
to separate AI patches from engineer-authored fixes and to report failed checks even when a PR was
merged. Manual bug reports can now enter suggest mode, but those suggestions are labeled UNVERIFIED
and require human review.

## Tough questions

### Why trust an AI patch?

Do not trust it because a model proposed it. Check the diff scope, patch policy, and test output;
then review the PR. Selfheal's gates reduce specific failure modes, but the evidence ledger includes
failed attempts and open PRs as well as successful runs. It does not claim that tests prove every
behavior.

### How is this different from Copilot Autofix?

I have not run a head-to-head comparison, so I cannot claim better accuracy or broader coverage.
Selfheal's documented focus is an incident-to-PR workflow driven by runtime and CI signals, with
repository context, patch checks, and test execution through its own MCP-backed worker. The
comparison I can support is about this project's workflow and evidence, not a product ranking.

### Why use synthetic demos?

They let me repeat a controlled defect and check the whole trigger-to-PR path without treating one
hand-picked external repository as a success-rate sample. The demos are clearly labeled synthetic;
they show integration behavior, not how often Selfheal repairs arbitrary real bugs.

### What failed in real repositories?

The 31-turn PyPDF2 repair attempt did not produce a working diff. A GitHub 403 initially stopped
the worker; that Selfheal bug was fixed and covered by a regression test. ShopCart's pagination bug
was fixed manually (engineer-directed, Claude-assisted) in PR #2, while its Selfheal onboarding PR
needed manual repair before CI ran. The manual `cryptography` dependency PR merged while its
required CI failed on a missing `psycopg` module in an unrelated database setup step. The manual
`report-bug` path only supports UNVERIFIED suggest-mode proposals and does not mark them verified.
These outcomes are in the [evidence ledger](history/RECOVERED_EVIDENCE.md).
