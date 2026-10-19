# Verification report

**Report date:** 2026-10-19
**Prior full local audit:** Windows 11 / Python 3.11.9 with disposable PostgreSQL 16.2
**Current publication state:** PR #18 is merged into `main`; no deployment occurred.

The dated 2026-09-29 live-system report is preserved in
[`VERIFICATION-historical-2026-09-29.md`](VERIFICATION-historical-2026-09-29.md).
It is historical evidence and is superseded by this report for the current
workspace status at the time of that audit.

## Latest merged `main` verification

PR [#18](https://github.com/Deepak20466/selfheal-mcp-agent/pull/18) merged
into `main` at
[`3ecc05417c206359154149e7fe6aec5507d56271`](https://github.com/Deepak20466/selfheal-mcp-agent/commit/3ecc05417c206359154149e7fe6aec5507d56271).
The post-merge [CI run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39204934454)
passed on Ubuntu 22.04: lint, formatting, strict mypy, dependency audits, Node
checks, benchmark fixture validation, and **634 passed, 4 skipped, 3 warnings**
in the PostgreSQL-backed suite. Its Bubblewrap install and namespace preflight
passed. The post-merge [Kubernetes CI run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39204934526)
also passed chart lint/render and recovery checks. The deploy workflow completed
with deployment steps skipped because no cloud VM was configured; no
production deployment was made.

## Current local follow-up

This follow-up changes provider CLI timeout cleanup so POSIX invocations start
in a new session and timeout cleanup signals the full process group. It also
filters common credential paths from MCP reads, writes, search output, file
listing, and repository-tree output. Windows tests cover those filters,
symlink aliases, process-tree cleanup behavior, and fail-closed sandbox setup.

On this Windows host, Ruff, formatting, strict mypy, and the complete suite
passed. The PostgreSQL helper provisioned a disposable loopback-only PostgreSQL
16.2 server. **656 passed, 7 skipped, 3 warnings in 642.46 seconds.** The
three warnings are mocked CLI login tests where `getpass` cannot control
terminal echo.

After publication, PR [#19](https://github.com/Deepak20466/selfheal-mcp-agent/pull/19)
passed Linux CI on Ubuntu 22.04: **658 passed, 5 skipped, 3 warnings in 86.66
seconds**. The new POSIX process-tree integration test ran (`tests/test_cli_process_tree.py`:
2 passed), and the Bubblewrap setup/preflight also passed. The [CI run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39209174807)
and [Kubernetes run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39209174813)
both completed successfully. This is hosted-runner integration evidence, not a
claim of kernel-level escape resistance.

The earlier full local audit and the post-merge Actions run above are evidence
for their respective commits; neither includes this unpushed follow-up change.

## Adversarial security matrix

| Scenario | Status | Evidence and remaining limit |
|---|---|---|
| Prompt injection in errors, PR text, and model output | PARTIAL | Untrusted-data delimiter and patch-guard regression tests exist; no live adversarial provider session was run. |
| Command injection through repository test commands | PARTIAL | Test commands execute only through the OS sandbox, with shell metacharacters interpreted inside that boundary. No dedicated end-to-end injection corpus was run. |
| Path traversal | PASS (bounded) | Repo path and diff-path tests reject traversal outside the repository; the benchmark includes a path-traversal repair scenario. |
| Symlink attacks | PASS (boundary checks) | Snapshot code omits symlinks/reparse points; follow-up tests cover linked secrets, linked workflow paths, and snapshot links where the host permits symlink creation. TOCTOU races during snapshot are not stress-tested. |
| Secret exposure | PARTIAL | Direct MCP reads, writes, search results, listings, and repo-tree output now filter common credential names/directories; repository command environments omit service credentials; Windows and Linux sandbox tests check host env/filesystem/network boundaries. Arbitrary secret content and provider CLI isolation remain out of scope. |
| Unsafe patches and test weakening | PASS (heuristics) | Tests reject forbidden paths, oversized diffs, deleted/disabled tests, suppression comments, and weakened coverage settings. The guard is a conservative diff heuristic, not a semantic proof. |
| Dependency confusion | PARTIAL | Scanner bootstrap uses a fixed scanner tool set and does not execute repository install scripts on the host. No malicious package registry/dependency-confusion campaign was run. |
| Privilege escalation | PARTIAL | Windows AppContainer/Job Object and Linux Bubblewrap tests verify OS boundaries and resource caps. No kernel escape, setuid, container-runtime, or cloud-identity attack was attempted. |

## Production-readiness matrix

| Boundary or workflow | Status | Evidence / blocker |
|---|---|---|
| Windows untrusted-command sandbox | PASS (tested boundary) | Full local suite passed on Windows, including AppContainer/Job Object filesystem, network, environment, process, timeout, and fail-closed tests. |
| Linux Bubblewrap sandbox and POSIX process cleanup | PASS (PR #19 CI) | Ubuntu 22.04 installed Bubblewrap and passed namespace preflight; `tests/test_cli_process_tree.py` passed both tests in the [PR CI run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39209174807). This is hosted-runner integration evidence, not a kernel-escape assessment. |
| MCP credential-path filtering | PASS (known filenames only) | Full local suite passed. Filtering does not identify secrets in arbitrary filenames or ordinary source text. |
| Provider CLI OS containment | FAIL / unverified | Codex/Claude/Gemini orchestration processes are not wrapped in the project's OS sandbox; provider-specific isolation is not independently proven. |
| MCP HTTP authentication and network policy | PASS (hosted chart/auth check; enforcement limited) | Hosted k8s CI observed 401 without a token and a non-401 MCP response with the configured token, and checked the healer-only ingress selector and MCP port. Actual CNI enforcement and egress destinations were not tested. Public TCP 443 remains broad. [Run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39215557085). |
| Prompt injection / malicious repository instructions | SKIPPED (live) | Unit-level untrusted-data and patch-guard tests exist; no adversarial live provider run was performed. |
| GitHub live repair workflow / rate limits | SKIPPED | Mock/local-bare-repository tests only. |
| Independent repository reliability | SKIPPED | The 20-case benchmark uses one synthetic repository, not three independent repositories. |
| Provider validation | PARTIAL | Codex fixed-fixture benchmark passed 20/20 previously. Claude is unauthenticated, Gemini CLI is unavailable, and live API providers were not tested. |
| Deployment, health, backup/restore, rollback | SKIPPED | Static configuration checks only; no infrastructure or production database was used. |
| Full PostgreSQL-backed suite (this branch) | PASS | Windows, isolated PostgreSQL 16.2: 667 passed, 6 skipped, 3 warnings in 782.21 seconds. No Docker or host database installation. |
| Production decision for unattended arbitrary-repository repair | **NO-GO** | Provider CLI OS containment remains unimplemented; live adversarial, multi-repository, GitHub mutation and deployment evidence is incomplete. MCP chart enforcement awaits hosted CI and live cluster validation. |

## Result matrix

| Check | Status | Result |
|---|---|---|
| Ruff lint | PASS | `python -m ruff check .` |
| Ruff formatting | PASS | `python -m ruff format --check .`; 222 files already formatted |
| Mypy | PASS | `python -m mypy core sentinel mcp_server healer`; 88 source files |
| Full pytest suite | PASS (this branch) | Windows, isolated PostgreSQL 16.2: 667 passed, 6 skipped, 3 warnings in 782.21 seconds. Merged-main CI before this branch: 634 passed, 4 skipped, 3 warnings; [run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39210118431). |
| Post-suite PostgreSQL smoke | PASS | After setting Alembic `path_separator = os`, the database helper ran migrations and 3 focused tests passed with no startup deprecation warning. |
| PostgreSQL migrations and integration | PASS | Fresh PostgreSQL 16.2 cluster; migrations 0001–0007 applied, demo seed completed, separate `selfheal_app` and `selfheal_test` databases |
| Pytest skips | SKIPPED | Linux Bubblewrap integration and venv-only Windows sandbox integration are unavailable in this base-Python Windows run; one capture case skips because its seeded bug is already fixed |
| Pytest warnings | WARN | Three `getpass` terminal echo warnings from mocked CLI login tests; no assertion failures |
| Python dependency audit | PASS | Upgraded only the isolated test venv to pip 26.2.1; `pip-audit --local` found no known vulnerabilities. The repository package is local and not published to PyPI. |
| Node example tests | PASS | `npm ci --ignore-scripts` followed by `npm test` passed 2/2; `npm audit` found 0 vulnerabilities. |
| npm audit | PASS | 0 known vulnerabilities |
| GitHub Actions | PASS | Both required PR checks passed: [CI](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39215557149) and [k8s-ci](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39215557085). |
| Hadolint | PASS | Temporary checksum-verified Hadolint 2.15.1 checked all four Dockerfiles |
| Helm and Kubernetes chart | PASS (hosted kind) | Helm lint, render, install, migration/readiness checks, MCP auth check, and recovery smoke tests passed in ephemeral kind. No production cluster was used; [run](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39215557085). |
| Render Blueprint | PASS (schema) | Official Render JSON Schema validation passed; Render CLI unavailable and no service created |
| MCP stdio protocol | PASS | Initialize/list-tools handshake; protocol 2025-11-25 and 20 tools |
| Windows sandbox boundaries | PASS | AppContainer/Job Object tests blocked outside filesystem access, loopback, inherited secrets, common credential files, and `.git`; tested process limits, timeout cleanup, and read-only trusted runtime access |
| Provider CLI OS isolation | WARN / unverified | Codex used its own read-only mode; provider CLI processes were not inside the project's AppContainer, and their filesystem-read boundary and prompt-injection behavior were not independently verified |
| Host-side Git operations | WARN / limited | Fixed direct-argv Git operations now have timeouts and filtered environments, but are not AppContainer-isolated; clone/push remain explicit network-bearing control-plane operations |
| Linux sandbox | PASS (PR #19 CI) | Ubuntu 22.04 Bubblewrap install and namespace preflight passed; Linux sandbox boundary tests and the new process-group descendant integration passed in [PR CI](https://github.com/Deepak20466/selfheal-mcp-agent/actions/runs/39209174807). |
| Node runtime in sandbox | PASS (limited) | `node -e` ran inside AppContainer |
| npm in Windows sandbox | PASS (limited) | Node and npm were staged in the workspace; `npm test` passed 2 tests. Node `--test` worker mode timed out, so the example script runs its `node:test` file directly. |
| Gitleaks current workspace | WARN | Two redacted matches in generated test bytecode containing test mock strings; no source-file matches and no suppression was added. |
| Gitleaks history | PASS | 108 commits scanned; no leaks found |
| Benchmark fixture | PASS | All 20 broken baseline tests failed; all 20 passed after applying the separate reference patch |
| Live Codex AI benchmark | PASS (fixture scope) | 20/20 diagnoses and 20/20 final repairs passed; S20 used one regression retry. This fixed benchmark does not demonstrate arbitrary-repository reliability. |
| Concurrent worker claim | PASS (targeted) | Two PostgreSQL sessions verified a second worker skips the job row locked by the first; no high-volume concurrency stress was run. |
| GitHub API rate-limit response | PASS (mocked; limited) | Mocked 403/429 PR writes surfaced without replaying the mutating request. GitHub Retry-After backoff and queued recovery are not implemented. |
| Claude Code CLI | SKIPPED | CLI present, but authentication status reported `loggedIn: false` |
| Gemini CLI | SKIPPED | `gemini` executable unavailable |
| Live API providers | SKIPPED | No API provider credentials used; mocked adapter tests ran in pytest |
| GitHub automation | PASS (mock/local; limited) | Mocked and local-bare-repository coverage for failure detection, investigation, patch generation, regression gates, duplicate prevention, retries, and rollback. GitHub 403/429 errors are surfaced without replay; Retry-After recovery and high-volume concurrent job stress remain unverified. |
| Live GitHub PR | SKIPPED | No PR was created; this publication uses a feature branch only |
| Docker build | SKIPPED | Docker was not installed and was not installed for this audit |
| Kubernetes / cloud deployment | SKIPPED | No cluster, VM, or Render service used; static validation only |

## PostgreSQL test reproduction

The helper installs PostgreSQL tooling outside the project and creates a new
loopback-only database cluster under the operating system temp directory for
each run. On PowerShell, install the helper into a temporary target and run:

```powershell
$auditVenv = Join-Path $env:TEMP "selfheal-audit-venv"
python -m venv $auditVenv
& "$auditVenv\Scripts\python.exe" -m pip install -e ".[dev]"
& "$auditVenv\Scripts\python.exe" -m pip install --target "$env:TEMP\selfheal-pgserver-audit" pgserver==0.1.4
$env:PYTHONPATH = "$env:TEMP\selfheal-pgserver-audit;$env:PYTHONPATH"
& "$auditVenv\Scripts\python.exe" scripts/test_with_ephemeral_postgres.py
```

The helper creates a random SCRAM password and a fresh `selfheal_test`
database in its temporary cluster, runs the ordinary pytest suite, then stops
the server and removes that run's temp directory. It does not require Docker or a
developer PostgreSQL service.

The final run reported:

```text
633 passed, 3 skipped, 3 warnings in 657.22s
Disposable PostgreSQL: PostgreSQL 16.2
```

Migrations 0001 through 0007 and deterministic demo seeding completed before
pytest started. The skips are the Linux-only Bubblewrap test, the Windows
integration requiring a virtual-environment Python interpreter, and a
capture demo whose seeded bug is already fixed. The warning summary consists of
three terminal echo warnings from tests exercising CLI login prompts. The
fresh environment's `pip-audit --local` reported no known vulnerabilities;
the unpublished local project itself is not available from PyPI for advisory
matching.

An earlier retained directory,
`C:\Users\K Deepak\AppData\Local\Temp\selfheal-pytest-postgres-ezjyprr3`,
contained a stale `postmaster.pid`; its recorded process was not running and
the recorded port was not listening. I left it untouched. The successful
fresh runs used unique temp directories and cleaned them up normally.

## Benchmark evidence

See [`docs/benchmark.md`](docs/benchmark.md) for the complete scenario table
and reproduction commands. Raw results are in
[`benchmarks/results/codex-full-2026-10-19-run5.json`](benchmarks/results/codex-full-2026-10-19-run5.json).

- 20 of 20 diagnosis labels matched.
- 20 of 20 final model-generated repairs passed both patch-scope and
  regression checks, an improvement of 12 repairs (+60 percentage points)
  from the previous 8/20 result.
- S20 used one regression retry, for 21 model generations total.
- Average end-to-end attempt time was 15.8649 seconds.
- CLI-reported token counts: 336,069 input, 12,544 cached input, 6,090 output.
- Dollar cost was not reported by the CLI; it is unknown, not zero.
- No scenario was skipped and no manual intervention occurred.

The model did not see or apply the reference solution. All attempts ran against
disposable copies. The final benchmark completed with exit code 0. Its result
is limited to this fixed fixture set.

## Isolation and deployment limits

Windows untrusted-command execution uses an AppContainer without network
capabilities and a Job Object. Commands run with a filtered environment,
workspace snapshot, resource limits, and no unrestricted native fallback.
The tests exercised filesystem, network, credential, process, timeout, and
cleanup boundaries. The Linux Bubblewrap implementation still requires an
integration run on a non-root Linux worker.

The Node example's sandboxed `npm test` passed using in-process request and
error handlers, without loopback sockets. The fixed scanner bootstrap runs
trusted tool installation outside the connected repository, using fixed
wheel-only packages, a private temporary home, and no inherited credentials.
Secrets in arbitrary filenames or ordinary source contents cannot be
recognized by filename exclusions.

The project AppContainer does not wrap Codex/Claude/Gemini orchestration
processes; those tools need their own network connection to the provider.
Codex's read-only setting and the Claude/Gemini tool configurations are
provider-level controls, and only Codex was used live. Fixed host-side Git
metadata and patch operations use direct argv, filtered Git environments,
and timeouts, but still parse repository data outside the OS sandbox.

The application-driven repair-to-PR workflow was not run against GitHub. The
hardening branch itself was published as PR #20, whose CI and ephemeral kind
checks passed. No production deployment, live CNI enforcement test, health
monitoring, backup/restore, or rollback was performed.

## Readiness

This project has materially stronger security and regression evidence,
but it is **not production-ready for unattended repair of arbitrary
repositories**. The fixed-set benchmark covers 20 seeded failures in one
synthetic repository, not three independent repositories. Provider CLI
filesystem and process isolation remain unverified; host-side Git control
operations are not OS-sandboxed; public HTTPS egress is not destination-filtered;
and live hostile-repository repair, provider recovery, rate-limit recovery,
deployment health, and rollback were not verified in this follow-up. The
current-tree Gitleaks matches were limited
to generated test bytecode and were not suppressed in the earlier scan.

See [`docs/AUDIT_REPORT.md`](docs/AUDIT_REPORT.md) for source changes,
security findings, integration status, and a resume-ready summary.
