# Three-minute Selfheal interview walkthrough

Use this walkthrough to explain the implementation and its evidence without making a live provider
request or implying a successful real-repository autonomous repair.

## 0:00–0:45 — Explain the architecture

Open the Mermaid diagram in [README.md](../README.md). Describe the flow: Sentinel captures runtime
or CI failures, the healer coordinates a configured model and scoped MCP tools, patch policy checks
bound candidate changes, and verification runs in an isolated worktree before a review PR can be
opened. Point out that PR review is the human decision boundary; Selfheal does not merge or deploy.

## 0:45–1:30 — Show the safeguards

Open [the security boundaries](architecture.md) and the MCP code tools. Explain the repository
allowlist, path confinement, secret scrubbing, bounded test execution, patch checks, and OS sandbox
used for untrusted commands. State the limits plainly: tests do not prove a kernel escape cannot
occur, and a passing test suite does not prove every behavior is correct.

## 1:30–2:15 — Show reproducible offline checks

From the repository root, run:

```powershell
python -m cli.main --help
python -m pytest --noconftest tests/test_healer_api_adapters.py tests/test_real_bug_benchmark.py -q
```

The focused suite covers provider serialization, budget enforcement, failure handling, and benchmark
selection/scoring. Show the current PR #49 CI and Kubernetes CI status links from GitHub; mention
the latest verified commit shown in the PR rather than reading a stale local SHA.

## 2:15–3:00 — Present the measured outcomes

Open [the real-bug report](benchmark-real-bugs.md). Separate the historical 20/20 fixture result and
synthetic repair-to-PR demos from the real benchmark. The Gemini pilot has five preserved failed
attempts across two bugs and no completed repair. Groq v3 has one diagnostic attempt: one successful
provider response, then a local token-budget stop; no repair completed. Its v4 reset-wait change is
offline-tested but has not been exercised against Groq.

For VIMJ, point to [PR #1](https://github.com/Deepak20466/vimj-studio-os-saas/pull/1) as a
Codex-assisted SQLite password-reset fix with 30 passing backend tests and passing PR CI. Be clear
that Selfheal did not produce it and that the SQLite-specific reproduction was not confirmed against
the live PostgreSQL deployment. A separate Selfheal fee-balance evaluation is preserved as two
failed attempts: the first exposed an MCP tracer recursion bug, and after that fix the second stopped
at a local Groq token-budget check. Neither produced a patch; the latest `read_file` context compaction
has offline coverage only and has not had live verification.

## Close

Describe Selfheal as a tested repair-workflow prototype with strong boundaries and incomplete
autonomous real-world evidence. Do not claim a successful autonomous VIMJ repair or generalized
repair reliability until a fresh, operator-authorized free-tier run completes and its patch passes
the target tests, full suite, sandbox, and review workflow.
