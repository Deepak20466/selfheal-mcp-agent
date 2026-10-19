# Local installation and operations

Selfheal runs as four local processes backed by PostgreSQL: the target app, sentinel, MCP server, and healer/API. The Windows bootstrap below does not install Docker.

## Install on Windows

Requirements: Python 3.11 or newer, PostgreSQL 16, and PowerShell.

```powershell
# Optional: create a fresh environment if you have not already run bootstrap.
.\scripts\bootstrap.ps1
```

To let the bootstrap script create the local database role and both databases, provide the PostgreSQL superuser password through its supported `PGSUPERPASSWORD` environment variable. It installs the project into `.venv`, creates `.env` from `.env.example` when needed, and runs Alembic migrations. This prompts for the password without putting it in shell history:

```powershell
$pgCredential = Get-Credential -UserName postgres -Message "Local PostgreSQL superuser"
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pgCredential.Password)
try {
    $env:PGSUPERPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    .\scripts\bootstrap.ps1
} finally {
    Remove-Item Env:PGSUPERPASSWORD -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
}
```

Generate the application password hash, then set it and a strong random session secret in `.env`:

```powershell
.\.venv\Scripts\python.exe scripts\hash_password.py
```

Start the local processes and log in:

```powershell
.\.venv\Scripts\selfheal.exe up
.\.venv\Scripts\selfheal.exe login
.\.venv\Scripts\selfheal.exe status
```

The app API, CLI-facing healer API, and MCP server use loopback by default. `selfheal up --public` creates a public tunnel for the sentinel webhook only. Use it only when a GitHub Actions workflow needs to reach a local sentinel, and never expose the healer API. The webhook is authenticated with `HEALER_WEBHOOK_SECRET`.

## Configure providers and GitHub

Set values in the local `.env` file; never commit it or show it in a recording.

- `AI_BACKEND` selects one provider. `AI_CHAIN` and `CHAT_CHAIN` optionally configure ordered fallbacks.
- Provider choices include Claude Code, Codex, and Gemini CLIs; the Anthropic API; and Groq, Gemini, and OpenRouter APIs. **Provider CLI execution currently fails closed on Linux and other platforms** until a network-aware OS sandbox contains the provider and its descendants. Existing Bubblewrap confinement has no network and cannot safely host these CLIs.
- The historical Codex CLI benchmark ran before this gate was added and is not a current live-workflow result. Claude Code was unauthenticated, Gemini CLI was unavailable, and API calls were mock-tested in the audit. See [benchmark.md](benchmark.md) and [VERIFICATION.md](../VERIFICATION.md) for evidence.
- In production, configure a GitHub App ID, installation ID, and private key through the deployment secret manager. The client mints a short-lived installation token scoped to one repository with only the API permissions the service uses. Static `GITHUB_TOKEN` is accepted for development; the production Helm chart omits it and production code refuses PAT-only authentication. Set `GITHUB_REPO` to the intended target and restrict the app installation to approved repositories. New service branches are limited to `autofix/*` and `selfheal-onboarding/*` by default. The CI-fix worker may update an existing branch only after GitHub confirms that its exact ref is the head of an open PR in the configured repository. Protected branch rules are checked for both paths. AI-generated PRs are never merged by the service; review and merge them manually. Do not reuse a production credential for a demo repository.
- The target application's `github_repo` in `config/monitored_apps.yaml` determines where its repair PR is opened. For an isolated demo, use a disposable repository and check this value before starting the services.

Do not remove the CLI execution gate or run provider CLIs against untrusted repositories. Review the blockers in [architecture.md](architecture.md#trust-boundaries) before enabling a provider CLI backend.

## CLI reference

```powershell
selfheal up
selfheal login
selfheal status
selfheal apps
selfheal connect https://github.com/owner/repository
selfheal scan app-name
selfheal prepare app-name
selfheal errors
selfheal watch
selfheal prs
selfheal metrics
selfheal down
```

`selfheal connect` clones a repository the configured GitHub App or development token can access. `selfheal scan` reports findings. A fix can spend AI budget and create a branch/PR; inspect the finding and confirm the target repository first. Automatic merging is disabled; review and merge generated PRs manually in GitHub.

For connecting any-language apps and adding telemetry, follow [onboarding.md](onboarding.md).

## Verification commands

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe core sentinel mcp_server healer
.\.venv\Scripts\pytest.exe
```

The test suite uses `TEST_DATABASE_URL`, not `DATABASE_URL`. Keep it pointed at a throwaway database. For the full PostgreSQL reproduction, exact skips, and dated results, see [VERIFICATION.md](../VERIFICATION.md). No Docker installation is required for the local process path.

The repository also has Docker images and a Helm chart. Their Kubernetes workflow runs in GitHub Actions; this does not mean a local Kubernetes cluster or production deployment was verified. See [VERIFICATION.md](../VERIFICATION.md) for current coverage and limits.

For Helm installs, set `env.mcpAuthToken` to a random value with at least 32
characters. The chart stores it in a Kubernetes Secret and gives the same
secret to the MCP server and healer. The chart's MCP NetworkPolicy accepts
connections only from healer pods. Reserve an unused address in your cluster's
Service CIDR for `dns.filterServiceIP`, and set `dns.upstreamServiceIP` to the
CoreDNS Service ClusterIP. Workload pods use the DNS-filter Service as their
only nameserver; their egress policies allow port 53 only to that Service. The
filter forwards exact hosts from `egress.allowedHosts` and Kubernetes service
names, strips EDNS data, and refuses other external names. Among the chart
workloads, only the filter can query CoreDNS.

Healer and MCP use the `selfheal-egress` CONNECT gateway for external HTTPS.
It rejects wildcard entries, non-443 CONNECT, mismatched TLS SNI, encrypted
ClientHello, and DNS answers that include private or reserved addresses. The
gateway's NetworkPolicy permits public
IPv4 TCP 443, while its application enforces the destination host allowlist.
The effective boundary therefore depends on both gateway code and a CNI that
enforces Kubernetes NetworkPolicy. The hosted Calico workflow tests allowed
GitHub/provider destinations, DNS filtering and direct-CoreDNS denial, a
denied external host, direct-egress bypass, and unauthorized pod access.
Re-run those checks with the CNI and deployment configuration used by your
cluster; they do not prove arbitrary provider CLI isolation or kernel escape
resistance. See
[architecture.md](architecture.md#trust-boundaries) for the full limits.
Avoid placing the token in shell history or process arguments; supply it
through your secret-management workflow.
