# GitHub profile kit

## Repository description

An AI-assisted runtime and CI repair agent that verifies candidate patches before opening PRs.

## Topics

`ai-agents` · `self-healing` · `developer-tools` · `mcp` · `ci-cd` · `python` · `automated-testing` · `observability`

## Resume bullets

- Found and fixed two live Selfheal failures during connected-repository dogfooding: dependency-audit JSON findings were silently dropped, and a remote-verification exception could stop the worker. Added three regression tests across [`ecba78b`](https://github.com/Deepak20466/selfheal-mcp-agent/commit/ecba78b90773d965d5218dce5688fb9cef6ef503) and [`c402003`](https://github.com/Deepak20466/selfheal-mcp-agent/commit/c402003ea41b7e2d8e5283a8954519e412c314d6); the changes were engineer-directed and Claude-assisted.
- Surfaced 16 dependency CVEs across five packages in a live scan of `multi-agent-workspace`. A Claude Code attempt to repair its PyPDF2 issue did not converge; a separate manual `cryptography` floor change was merged as [PR #1](https://github.com/Deepak20466/multi-agent-workspace/pull/1), whose required CI failed on an unrelated missing `psycopg` dependency.
- Completed three controlled repair-to-PR demonstrations across independent synthetic repositories. Each repair's CI passed; as of 2026-11-07 one repair PR is merged and two remain open. See the [evidence ledger](history/RECOVERED_EVIDENCE.md).

## Interview explanation: 60 seconds

Selfheal is a Python system that receives runtime and CI failures, gathers scoped repository context
through MCP tools, and asks a configured model to propose a patch. It checks the candidate against
write-path and patch rules, runs verification in an isolated worktree, and can open a GitHub PR when
the checks pass.

The most useful real-world lesson came from running it against `multi-agent-workspace`. That exposed
a scanner bug which hid audit findings and a worker exception path that could stop processing jobs.
I fixed both and added regression tests. The AI did not repair the repository's PyPDF2 issue, and a
ShopCart pagination bug remains unfixed. I keep those outcomes visible because the synthetic demos
and the real-repository failures measure different things. The [verification report](../VERIFICATION.md)
and [recovered evidence](history/RECOVERED_EVIDENCE.md) link to the exact records.

## Interview explanation: 2 minutes

Selfheal connects application telemetry and CI failures to a repair workflow. Sentinel records the
failure, the worker uses the configured backend, and MCP tools provide repository context and
bounded patch/test operations. Candidate patches are checked for allowed paths and policy before
tests run in an isolated worktree. When the configured verification succeeds, the system can create
a GitHub PR for review.

I tested the workflow at three different levels. First, the project's own offline and hosted CI
checks cover the worker, scanner, patch boundaries, and test orchestration. Second, controlled
synthetic repositories exercised the repair-to-PR path; the three recorded PR checks passed. Third,
connected-repository dogfooding exposed two bugs in Selfheal itself. The scanner had been dropping
dependency audit results when stderr contaminated JSON, and the remote-verification worker could
stop after an uncaught GitHub exception. Both have regression tests.

The external repair evidence is mixed. A real PyPDF2 issue was not repaired within the recorded
Claude Code attempt. A manual dependency floor change for three `cryptography` advisories was
merged, but its required CI failed before reaching the changed dependency because the target
workflow lacked `psycopg`. A real ShopCart pagination issue remains unfixed. Those are the lessons I
would emphasize: tests must exercise actual tool boundaries, AI success must be separated from
human changes, and a merged PR with a failed check is not a green verification result.

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

The PyPDF2 repair attempt did not produce a working diff, and the associated worker initially stopped
after a GitHub 403. The worker failure was fixed and covered by a regression test; the PyPDF2 issue
was not. ShopCart's previous-page pagination bug was documented but not repaired. The manual
`cryptography` dependency PR merged while its required CI failed on a missing `psycopg` module in an
unrelated database setup step. These outcomes are recorded in the [evidence ledger](history/RECOVERED_EVIDENCE.md).
