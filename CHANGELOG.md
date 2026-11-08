# Changelog

## Unreleased — 2026-11-07

### Fixed
- **Groq free-tier token budgeting**: after v2 recorded a TPM 429 and a request-length HTTP 400,
  every Groq request now budgets prompt plus completion against the current 8,000 TPM allowance and
  the 131,072-token model context. It uses provider remaining-token headers, visibly compacts only
  rediscoverable file listings and non-diagnostic test-log lines, and fails closed if essential
  evidence cannot fit. A confirmed temporary TPM 429 can retry once at the exact server delay only
  if it is bounded to 60 seconds and fits the remaining case deadline. Daily quota is never retried.
  V3 keeps the existing model, timeouts, case list, scoring, and case token cap. Offline regressions
  cover the v2 429/400, multi-turn growth, preservation of test evidence and tool IDs, deadline
  handling, and no daily-quota retry. No new provider call was made.
- **Gemini function-call history**: preserve each complete original model Part (including opaque
  thought signatures) in subsequent `generateContent` requests. Match function responses by the
  returned function name and call ID, preserving model Part/call order for sequential and parallel
  calls. Added offline regressions for the reported `run_tests` HTTP 400 and multi-call histories.
  Frozen v9 keeps the v4-v8 files, original 41 cases, and pilot IDs unchanged; both v9 leakage
  audits pass.
- **Gemini benchmark request deadline propagation**: the runner now passes the remaining case
  deadline to the Gemini HTTP client instead of allowing its fixed 120-second timeout to outlive
  the runner's case budget. HTTPX timeouts are classified at the provider-request boundary with
  their cause preserved; no automatic retry was added. Frozen v8 preserves the existing cohort and
  pilot IDs. Regression coverage checks timeout propagation and prevents requests after expiry.

### Evidence
- Five saved v5/v7/v8 attempt records cover two bugs: two request timeouts and three Gemini HTTP
  400 errors from missing thought signatures. No repair completed; eight pilot IDs remain
  unattempted. The saved score report and raw records are linked from
  `docs/benchmark-real-bugs.md`. No new Gemini request was made for the v9 fix.

### Added
- **Separate GroqCloud frozen benchmark**: preserves v1/v2 freezes and their two diagnostic
  failures, and adds v3 with the same 41 cases, original ten pilot IDs, prompts, scoring rules, and
  case limits. Full, pilot, and one-case v3 diagnostic leakage audits pass. The v3 diagnostic is
  unattempted; no repair result is claimed. See `docs/groqcloud-repair.md` for the operator-run
  command and isolated VIMJ testing workflow.
- **Mined historical bug benchmark** (`benchmarks/real_bugs/`): repository mining, flaky-case
  preparation, a production-loop provider/MCP runner, validation-policy ablation, resumable JSONL
  records, and result scoring. Offline fixture tests cover mining, preparation, and scoring. No
  provider repair result is claimed until an actual run is recorded.
- **Runtime error demo** (`examples/runtime_demo/`): three seeded FastAPI failures, app-scoped
  permissions, regression expectations, and native Bash/PowerShell scripts that wait for the
  recorded healer job outcome. Live repairs and PR outcomes are pending; none are claimed here.
- **Healer observability**: request correlation IDs, `/readyz`, Prometheus `/metrics`, and a
  bounded PostgreSQL notification wakeup queue.
- **Bounded API retries**: provider adapters retry explicit 502/503/504 responses with jitter;
  GitHub GETs retry transient HTTP and transport errors. Rate limits cool down and fall back,
  mutations are not replayed blindly, PR creation reconciles duplicate head/base requests, and
  PostgreSQL advisory locks deduplicate concurrent CI webhook deliveries by run and PR keys.

### Security and verification notes
- Historical source fixes are now kept out of prepared model workspaces; preparation snapshots the
  parent plus test-only diff before applying the scoring-only fix.
- The no-policy benchmark arm still uses the OS sandbox for mined test execution. It only skips
  Selfheal's patch-size and anti-cheating policy checks.
- Network mining, free-tier provider runs, and runtime-demo repairs are reported only when executed.
  The current provider run is pending because this environment has no provider key or PostgreSQL
  database configured. See `VERIFICATION.md` for the audit status and reproduction commands.

All notable changes to this project are summarized here. This is a
factual summary, not marketing copy — `CLAUDE.md`'s phase log is the
source of truth for how each item was built and verified; see it for
detail, caveats, and what's live-verified vs. mocked-tests-only.

## v1.1.0 — 2026-09-29

### Added
- **Monorepo support** (`core/repo_connect.py:find_subprojects`/
  `select_subproject`): detects nested sub-projects up to 2 levels below a
  connected repo's root and scopes a connect/scan/fix to one of them
  (`selfheal scan <app> --path <subdir>`), with no schema or sandbox
  change needed.
- **Remote-verification fix mode** (`healer/remote_verify.py`): for a
  connected app whose manifest names a heavy/ML dependency (torch/
  tensorflow/transformers/chromadb/CUDA-class — never installed locally,
  by standing project rule), opens a fix PR verified by the connected
  repo's own GitHub Actions CI instead of a local test run. Clearly
  labeled, never auto-merged.
- **Remote-verify PR CI polling + retry** (`healer/remote_ci_poll.py`,
  new): a background poller (same shape as the auto-merge poller) reads a
  remote-verify PR's real GitHub check-run status via the API — no
  webhook required — and pushes one retry fix to the same branch on a
  real CI failure, capped at 2 total attempts per PR. Mocked-tests-only;
  never run against a real connected repo's real CI failure.
- **`selfheal prepare`** (`core/repo_health_check.py`): a static, read-only
  readiness checklist for a connected repo (tests, CI, test config, heavy
  deps) that never executes the app's own code.
- **Onboarding via `--onboard`** (`healer/onboarding_prepare.py`): spends
  AI budget on one PR adding only what `prepare` found missing, verified
  locally when possible and never auto-merged.
- **Suggest mode via `--suggest`** (`healer/suggest_mode.py`): the
  system's weakest-confidence tier — an unverified suggestion PR for a
  repo where no other fix path applies, clearly labeled.
- **`selfheal audit`** (`core/repo_audit.py`): a health verdict across
  every repo the configured `gh`/GitHub token can see, using the
  tree/contents API rather than cloning, so it scales cheaply.
- **GitHub Copilot Chat MCP interop** (`.vscode/mcp.json`): exposes this
  project's MCP tools to GitHub Copilot Chat in VS Code alongside the
  existing Claude Code wiring.
- **AI-chain dedup fix**: `AI_CHAIN`/`CHAT_CHAIN` now dedupe, keeping
  first-occurrence order, instead of letting a repeated backend name skew
  chain-exhaustion/cooldown logic.
- **Global credential scrubbing fix**: logs are scrubbed of credentials
  consistently across code paths that previously missed it; stuck
  onboarding jobs are also fixed as part of the same change.
- **Docker images + Kubernetes-tested CI** (`deploy/docker/
  Dockerfile.{app,sentinel,mcp,healer}`, `deploy/helm/selfheal/`,
  `.github/workflows/k8s-ci.yml`): the 4 pods are containerized and
  deployed to a real `kind` cluster via Helm on every push, in GitHub
  Actions only — Docker/kind/Helm/kubectl are never installed or run on a
  developer's own machine, per this project's own standing safety
  convention. Proves real infra-level self-healing (a killed pod process
  gets recreated by Kubernetes) in addition to this project's own
  application-level AI healing.

### Fixed
- Kubernetes liveness/restart test: `kill -9 1` doesn't work inside a
  container (PID 1 handling); the test now kills the actual `uvicorn`
  process and tolerates the resulting expected exit code from `kubectl
  exec`.
- The demo migration Job now seeds the demo dataset itself — root cause of
  an earlier k8s-ci smoke-test failure.
- `scripts/verify_all.py`'s Docker check was stale (asserted "no Docker
  files anywhere in repo", left over from before Docker/Kubernetes support
  existed) — replaced with a check that the expected Docker/Helm/k8s-ci
  files are present and the latest `k8s-ci.yml` GitHub Actions run is
  green.

## v1.0.0 — 2026-09-29

Initial terminal-only v1.0 release: the full 8-phase SPEC.md build (self-
healing detection, MCP tool server, runtime + CI healing, a terminal
`selfheal` CLI, pluggable AI backends including free Claude Code/Codex/
Gemini CLIs and paid API backends with fallback chains, per-app auto-merge,
a privacy/secret-scrubbing guard on every tool result reaching an AI
backend, and PyPI packaging) — see `CLAUDE.md`'s phase log for the full
history and `VERIFICATION.md` for what was verified live vs. mocked-tests-
only at that point.
