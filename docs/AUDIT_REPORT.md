# Project audit and validation report

**Project:** `selfheal-mcp-agent`
**Audit date:** 2026-10-19
**Scope:** local source and uncommitted changes, test infrastructure, subprocess
isolation, AI benchmark, GitHub automation, provider CLIs, CI/CD configuration,
dependencies, and deployment manifests. Publication is limited to a feature
branch; no pull request, merge, or deployment is performed.

## Executive assessment

The audit hardened connected-repository command execution, made the scanner's
tool bootstrap independent of repository-controlled files, added a disposable
PostgreSQL test runner, repaired test-database safety checks, and updated the
Codex benchmark and documentation. The Windows AppContainer and Job Object
path has meaningful boundary tests and the full pytest suite was run against
PostgreSQL 16.2.

The project is **not ready for unattended production repair of arbitrary
repositories**. The latest real Codex benchmark verified all 20 generated
repairs, improving the previous 8/20 result by 12 repairs. That is a promising
result on this fixed fixture set, not evidence of general reliability. Linux
Bubblewrap isolation has not been exercised; provider CLI and fixed Git
control-plane subprocesses are outside the project's AppContainer; the current
developer interpreter contains stale vulnerable packages; and no live GitHub
PR or staging deployment/rollback was performed. This report describes
observed evidence, not a security certification.

## Scope and changes

| Area | Work completed |
|---|---|
| Subprocess environment | Added centralized removal of inherited provider, database, GitHub, webhook, cloud, and other service credentials for repository-controlled commands. Git child authentication is isolated to the controlled Git operation. Error paths scrub credential-like values. |
| OS sandbox | Added a fail-closed runner in [`mcp_server/execution_sandbox.py`](../mcp_server/execution_sandbox.py). Windows uses AppContainer without network capabilities plus a Job Object for child-process, memory, and CPU-time limits; the runner also caps wall time, output, and workspace growth. Active virtualenv runtime files, including `pyvenv.cfg`, receive read-only access for sandboxed child Python. Linux requires Bubblewrap, namespaces, non-root execution, read-only runtime mounts, and resource limits. There is no native fallback. |
| Filesystem boundary | Repository content is copied into a disposable workspace. `.git`, symlinks/reparse points, common secret files/directories, and scanner cache files are excluded. The scanner's trusted tools live in a user-private cache outside connected repositories and are mounted read-only during scans. |
| Scanner bootstrap | Removed execution of repository-provided setup/install commands on the host. The scanner installs only its fixed pytest, Ruff, mypy, and pip-audit tool set, wheel-only, into an atomic private venv with a synthetic home and no inherited credentials. Setup is time-bounded. |
| Test database | Added explicit guards against the application and test URL naming the same database, requiring a marked `_test` database, and introduced [`scripts/test_with_ephemeral_postgres.py`](../scripts/test_with_ephemeral_postgres.py) for loopback-only, disposable PostgreSQL without Docker. |
| Benchmark | Ran all 20 scenarios against the real Codex CLI using model-generated patches. The report selector uses one complete run and does not aggregate overlapping partial runs. Fixture baseline and reference solutions also ran through the OS sandbox. |
| Reliability | Added strict single-file diff parsing, nonzero-context validation, private candidate application, one regression retry, report recovery, worker MCP reconnect handling, bounded fixed Git operations with timeouts and cleanup, hardened Git error redaction, and fixed an order-dependent prober assertion. |
| CI and packaging | Tightened workflow token permissions, added dependency and fixture checks, updated vulnerable Node example dependencies, and corrected Dockerfile runtime users and healthcheck forms. |

The workspace contains pre-existing user changes as well as audit changes; no
files were staged or committed. See `git status` before selecting changes to
commit.

## PostgreSQL and test results

The full suite used the repository's disposable PostgreSQL helper. It created
a fresh PostgreSQL 16.2 data directory under the system temporary directory,
bound only to `127.0.0.1`, used a generated temporary SCRAM password, and
created the `selfheal_test` database. The app URL named a different database;
the helper provisioned and tests used only `selfheal_test`. Migrations 0001
through 0007 upgraded it, then deterministic demo data was seeded. The helper
stops PostgreSQL and removes its temporary directory after the run.

Final full run on a fresh disposable PostgreSQL 16.2 cluster:
**633 passed, 3 skipped, 3 warnings** in 657.22 seconds.
The skips were Linux Bubblewrap integration on Windows, the Windows sandbox
integration requiring a virtual-environment Python interpreter, and a demo
capture test whose seeded bug now returns 404 because the fix is already
present. The warnings were `getpass` terminal-echo warnings from
three CLI login tests. A prior full run exposed one order-dependent assertion
in `test_persist_results_writes_contract_violations`; the test selected
unrelated rows from the shared test table. It now scopes its query by the
deterministic contract fingerprint. All four prober tests passed on a fresh
disposable PostgreSQL database after that fix. After the full run, a one-line
Alembic `path_separator = os` configuration fix removed a helper startup
deprecation warning; the focused PostgreSQL concurrency and GitHub 403/429
tests passed again (3 tests) after that configuration change.

The database integration tests exercise migrations, transaction rollback and
savepoints, committed cross-connection operations, row claiming, persistence,
and recovery paths covered by the suite. A new two-session PostgreSQL test
confirmed that a second worker skips a job row held by the first. This checks
the `FOR UPDATE SKIP LOCKED` behavior but is not a high-volume concurrency
stress test. The tests did not connect to or modify a developer database.

An earlier retained full-suite directory,
`C:\Users\K Deepak\AppData\Local\Temp\selfheal-pytest-postgres-ezjyprr3`
contained a stale `postmaster.pid`; its recorded process was not running and
its port was not listening. I left it untouched. Successful disposable runs
use fresh directories and clean them up normally.

## AI repair benchmark

The full report and scenario-level table are in
[`docs/benchmark.md`](benchmark.md), with raw per-generation records in
[`benchmarks/results/codex-full-2026-10-19-run5.json`](../benchmarks/results/codex-full-2026-10-19-run5.json).

| Measure | Observed |
|---|---:|
| Attempts | 20/20; none skipped |
| Diagnosis labels correct | 20/20 (100%) |
| Repairs passing patch and regression gates | 20/20 (100%) |
| Improvement from previous run | +12 repairs; +60 percentage points |
| Total model generations | 21; S20 used one retry after a regression failure |
| Mean repair and verification time | 15.8649 seconds |
| Input / cached input / output tokens | 336,069 / 12,544 / 6,090 |
| CLI-reported dollar cost | Not reported; unknown, not zero |
| Manual interventions | 0 |

Each scenario's final patch was evaluated only in a disposable copy. The
single retry followed the same patch-scope and regression gates. The benchmark
used the same fixtures and tests as the previous run and exited with status 0.
The result does not qualify arbitrary-repository repairs for unattended use.

The fixture validator ran all 20 broken baseline tests (all failed), applied
the separate reference repair, and ran all 20 tests again (all passed). The
reference patch changed only `app.py`. This confirms the fixture oracle; it
does not improve or substitute for the model-generated benchmark result.

## Security isolation assessment

### Verified on this Windows host

Windows integration tests exercised denial of reads and writes outside the
workspace, loopback network access, inherited environment credentials,
common credential files, and `.git`; process-count limits and timeout cleanup
were also covered. Trusted read-only tool paths were readable while writes
were denied. The Codex fixture baseline and post-patch tests ran through this
AppContainer path. A standalone Node program ran in the sandbox.

Repository-provided test and scan commands have no native fallback. If
sandbox setup or execution fails, the command fails closed. The runner enforces process,
memory, CPU time, wall time, output size, and workspace growth limits.

### Limitations and unverified boundaries

- Linux Bubblewrap code is present but was not exercised on a non-root Linux
  worker. The Windows test suite skips this platform-specific path.
- The sandbox stages Node and npm in the workspace and uses preserve-symlinks
  options to avoid launcher traversal through the protected root of `C:`.
  The Node example's `npm test` passed 2/2 in-process route/error-handler
  checks inside AppContainer. Network access remains disabled. Node's `--test`
  worker mode timed out in AppContainer, so the example invokes its same
  `node:test` file directly.
- The provider CLI process itself is not wrapped by the project's AppContainer.
  Codex ran with its own `read-only` sandbox setting, but this audit did not
  independently verify that CLI's filesystem read boundary, process limits,
  or prompt-injection behavior. AppContainer results above apply to repository
  verification commands, not to the model orchestration process.
- Fixed Git metadata and patch operations run as direct host-side Git
  processes, with filtered environments and timeouts; they do not execute a
  caller-supplied shell string. They are not AppContainer-isolated, so hostile
  repository metadata remains a separate attack surface. Network-bearing
  clone/push operations are explicit control-plane operations.
- Secret exclusions use known filenames and directories. They cannot detect
  a secret under an arbitrary filename or inside ordinary source content.
  Do not store credentials in connected repositories.
- Repository-controlled tests run without network access. Python advisories
  use exact package/version pins (including supported lockfiles) with pip-audit
  `--no-deps`; npm advisories use a reconstructed lockfile containing package
  names and versions without resolved URLs or project config. Both use clean
  scanner-owned processes, temporary configuration, fixed public registries,
  and a timeout. Repository scripts, package sources, and build configuration
  are not run with network access. Other ecosystems without a metadata-only
  adapter are reported as skipped.
- Prompt-injection unit coverage exists, but a live adversarial model exercise
  was not run. No formal penetration test or security certification was done.

## GitHub automation

The GitHub automation suite uses mocks and a local bare repository. It covers
failure detection, investigation, patch generation, regression gates,
duplicate prevention, retry behavior, and rollback handling. Workflow
permissions were reviewed and set explicitly to least privilege; `actionlint`
validated workflow syntax. No live workflow dispatch, PR creation, merge, or
external rollback was attempted. Publication is limited to a feature branch;
it does not create a PR. Live PR creation remains unverified and requires
separate authorization. The
mocked GitHub 403/429 tests confirmed rate-limit responses fail without
replaying the mutating PR request. The client does not implement Retry-After
backoff or queued recovery for GitHub rate limits, and high-volume concurrent
job stress was not exercised.

## Provider integration status

| Provider | Status in this audit | Evidence / blocker |
|---|---|---|
| Codex CLI | PASS for authentication, invocation, parsing, and benchmark execution | Codex CLI 0.162 alpha used the existing authenticated login for 20 scenarios and 21 model generations. All final patches passed scope and regression checks. Repair quality improved from 8/20 to 20/20 on this fixture set. |
| Claude Code CLI | SKIPPED | CLI installed; `claude auth status` reported `loggedIn: false`. Earlier real PRs remain historical evidence, not a live validation in this audit. |
| Gemini CLI | SKIPPED | `gemini` executable unavailable. |
| Anthropic API / other HTTP providers | SKIPPED live | Adapter paths and failures are covered by mocked tests; this audit did not use live API credentials. |

## CI/CD and deployment validation

| Check | Result | Evidence and limits |
|---|---|---|
| Ruff lint and formatting | PASS | `python -m ruff check .`; `python -m ruff format --check .` |
| Mypy | PASS | `python -m mypy core sentinel mcp_server healer`; 87 source files |
| Python dependency audit | PASS in fresh venv; WARN in global interpreter | A clean temporary venv installed with `.[dev]` and `pip-audit --local` found no known vulnerabilities; the unpublished local project is skipped because it has no PyPI release to match. The active global interpreter audit found 91 advisory entries across 8 installed packages, including stale project transitive packages and unrelated tooling; that interpreter was not upgraded. The project does not commit a lockfile for transitive dependencies. |
| Node tests and audit | PASS | `npm test` passed 2/2 inside AppContainer; `npm audit` found 0 vulnerabilities. The tests use in-process handlers and no sockets. |
| GitHub Actions | PASS (static) | Temporary actionlint 1.7.12 binary passed all workflow YAML. Jobs were not dispatched on GitHub. |
| Dockerfiles | PASS (static) | Temporary Hadolint 2.15.1 passed all four Dockerfiles. No Docker installation or image build was performed. |
| Helm | PASS (static) | Helm 4.3.1 strict lint passed; render produced 14 Kubernetes objects and rendered YAML parsed. No cluster/API server was used. |
| Render Blueprint | PASS (schema) | `render.yaml` conforms to the official Render JSON Schema using Draft 2020-12 validation. Render CLI was unavailable; no cloud resource was created. |
| MCP protocol | PASS | Real stdio initialize/list-tools handshake returned protocol version 2025-11-25 and 20 tool names. No database-backed repair action was invoked. |
| Secret scan | WARN (working tree); PASS (history) | The redacted Gitleaks working-tree scan found two matches in generated test `.pyc` files, not source or credential files. Gitleaks history scan checked 108 commits and found no leaks. No suppression was added. |
| Kubernetes/deploy/rollback | SKIPPED | No local Docker, Kubernetes cluster, VM, or Render deployment was used. Static manifests and rollback workflow logic were reviewed; live health, logging/metrics, migrations, and rollback remain unverified. |

## Other audit findings and residual risks

- Filesystem copy rules reject symlink/reparse traversal and omit common
  secret locations, but arbitrary source content can still contain secrets.
- `search_code` validates the pattern and result limit and passes the search
  pattern as a separate `git grep -e` argument, preventing option injection.
- Controlled Git operations use child-only auth configuration; repository
  test commands receive scrubbed environments. Errors redact credential-like
  strings before logging.
- Workflow permissions are explicit and read-only by default; release
  publication is the intentional write-scoped exception. No external action
  was run.
- Retry/reconnect behavior has unit coverage; a live MCP outage/recovery and
  GitHub API recovery run were not performed.
- The project does not pin every transitive Python runtime dependency in a
  lockfile. A clean audit today does not guarantee future resolver output.

## Resume-ready summary

Built a Python/FastAPI self-healing platform using MCP, PostgreSQL,
OpenTelemetry, GitHub Actions, and pluggable coding-agent CLIs. Hardened
repository command execution with a fail-closed Windows AppContainer sandbox,
credential-filtered subprocess environments, and a private scanner tool
cache. Added disposable PostgreSQL verification, worker reconnection and
GitHub workflow checks, and measured a 20-scenario live Codex benchmark
(20/20 diagnoses correct; 20/20 repairs passed regression gates). Documented
cross-platform isolation, CI, provider, and deployment limits. Publication
pushes a feature branch only; no PR, merge, or deployment is performed.
