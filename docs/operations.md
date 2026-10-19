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
- Provider choices include Claude Code, Codex, and Gemini CLIs; the Anthropic API; and Groq, Gemini, and OpenRouter APIs.
- In the 2026-10-19 audit, Codex CLI completed the 20-scenario repair benchmark. Claude Code CLI was present but unauthenticated, Gemini CLI was unavailable, API calls were mock-tested, and Groq/OpenRouter live checks were historical rather than rerun. See [benchmark.md](benchmark.md) and [VERIFICATION.md](../VERIFICATION.md) for evidence.
- Set `GITHUB_TOKEN` to a fine-grained token restricted to the exact repository, with Metadata read, Contents read/write, Pull requests read/write, Issues read/write, and Actions read/write only when using run retry/cancel or rollback dispatch. Set `GITHUB_REPO` to the intended target and keep `AUTO_MERGE=false`. Do not reuse a production token for a demo repository.
- The target application's `github_repo` in `config/monitored_apps.yaml` determines where its repair PR is opened. For an isolated demo, use a disposable repository and check this value before starting the services.

The local provider CLI itself is not launched inside the project's AppContainer. Use only repositories you are authorized to share with that provider, and review the limitations in [architecture.md](architecture.md#trust-boundaries).

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

`selfheal connect` clones a repository the configured token can access. `selfheal scan` reports findings. A fix can spend AI budget and create a branch/PR; inspect the finding and confirm the target repository first. Auto-merge is disabled by default.

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
