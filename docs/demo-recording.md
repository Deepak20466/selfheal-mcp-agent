# End-to-end demo recording guide

This guide describes the intended controlled runtime-failure walkthrough; it is a procedure, not a recording. **The full repair-to-PR recording is currently blocked** because provider CLI execution now fails closed until a network-aware OS sandbox exists. The existing [terminal clip](demo-terminal.mp4) is 0:50 and does not show the repair-to-PR flow.

## Required demo target

Use a separate disposable GitHub repository containing this project and its CI workflow. Do not run a deliberately failing demo against `Deepak20466/selfheal-mcp-agent` or another repository with valuable work. The portfolio repository is not the disposable target.

Before recording:

1. Get the exact disposable repository approved. Confirm GitHub Actions are enabled and that the repository can accept a branch and pull request.
2. Prepare a clean clone with a fresh local PostgreSQL app database and a separate test database. Follow [operations.md](operations.md).
3. In the demo clone, set `config/monitored_apps.yaml` so the `target_app` entry points to the disposable repository. The `github_repo` value must match the demo repository. Commit this baseline setup before the recording.
4. Configure a live, authenticated AI backend, a repository-scoped `GITHUB_TOKEN`, and `.env` values for local PostgreSQL and authentication. Do not put secrets in command arguments, source files, or screen captures.
5. Set `AUTO_MERGE=false` and confirm `selfheal apps set target_app --auto-merge off`. Start from a clean app database so old errors and PRs do not obscure this run.
6. Confirm the repo has the seeded `/trigger/zero` controlled failure, a real regression-test command, and a GitHub Actions workflow that runs those tests. Do not use a mock provider, fixture transcript, or fabricated output.

Do not run the demo until the provider CLI isolation blocker in [architecture.md](architecture.md#trust-boundaries) is resolved and the repaired boundary has been verified. The demo repository must contain no real credentials or private user data. Do not expose the healer API publicly. This runtime-trigger path needs only local sentinel capture and GitHub's normal PR checks; it does not need a public webhook tunnel.

## Prepare the demo clone

Perform setup before recording. The disposable repository's committed `config/monitored_apps.yaml` and the clone's Git remote must both point to that repository.

```powershell
$DemoRepo = "owner/disposable-repo"
$RepoDir = Join-Path $env:TEMP "selfheal-demo-target"
gh repo clone $DemoRepo $RepoDir
Set-Location $RepoDir
git remote get-url origin
# Run the secure bootstrap in operations.md, set local .env values, and authenticate the selected AI provider.
```

## Exact recording commands

Run the commands from the prepared demo clone:

```powershell
$DemoRepo = "owner/disposable-repo"
$RepoDir = Join-Path $env:TEMP "selfheal-demo-target"
Set-Location $RepoDir

# Start the four local processes and authenticate at the hidden password prompt.
.\.venv\Scripts\selfheal.exe up
.\.venv\Scripts\selfheal.exe login
.\.venv\Scripts\selfheal.exe status
.\.venv\Scripts\selfheal.exe apps set target_app --auto-merge off
```

Open a second terminal tab in the same clone and start the live job monitor before triggering the failure:

```powershell
$RepoDir = Join-Path $env:TEMP "selfheal-demo-target"
Set-Location $RepoDir
.\.venv\Scripts\selfheal.exe watch
```

In the first tab, trigger the seeded runtime error. Suppress the response body so a traceback cannot reveal local paths:

```powershell
curl.exe -sS -o NUL -w "HTTP %{http_code}" http://127.0.0.1:8001/trigger/zero
.\.venv\Scripts\selfheal.exe errors
```

Keep the monitor visible through diagnosis, patch generation, regression verification, and PR creation. When it reports the PR stage, press Ctrl+C in the monitor tab and inspect the result:

```powershell
.\.venv\Scripts\selfheal.exe prs
$PrNumber = Read-Host "Enter the PR number shown by selfheal prs"
gh pr view $PrNumber --repo $DemoRepo
gh pr checks $PrNumber --repo $DemoRepo --watch
```

Show the PR diff to confirm a regression test was added, then the completed Actions checks with a green conclusion. Do not merge. Stop local services afterward:

```powershell
.\.venv\Scripts\selfheal.exe down
```

This demonstrates a controlled runtime error followed by AI diagnosis, an automatically proposed code repair, regression-test verification, a GitHub PR, and CI on the PR. The full workflow depends on the approved demo repository and its configured credentials; these steps have not been run against the portfolio repository.

## 3–5 minute edit plan

| Time | Show |
|---|---|
| 0:00–0:20 | Project name, architecture, and the disposable target repository |
| 0:20–0:45 | `selfheal status`; point out the local processes and `AUTO_MERGE=false` |
| 0:45–1:05 | Trigger `/trigger/zero`; show the real HTTP 500 and the captured error |
| 1:05–2:25 | Show live healer progress and the root-cause explanation; keep real command output visible |
| 2:25–3:10 | Show the generated code diff and regression test, then verification result |
| 3:10–4:00 | Open the generated PR and show its successful GitHub Actions checks |
| 4:00–4:15 | Close on the limitations and link to the benchmark and audit |

If live provider or CI latency exceeds the target, preserve the real sequence and evidence. Cut only idle waiting or speed up actual captured footage; do not fabricate progress, terminal output, screenshots, or a green status.

## Recording setup

No full-flow recording has been made. This execution environment cannot control the Windows desktop or start/stop a screen recorder. The simplest local method is Windows Snipping Tool: press **Windows+Shift+R**, select the terminal/browser region, start recording, then stop with the Snipping Tool toolbar. Save the actual MP4 and review it before publishing:

- Capture the terminal and GitHub PR in 1920×1080; use a second display or record the browser segment separately if region selection cannot include both.
- Disable desktop notifications and close unrelated tabs. Do not display `.env`, password prompts, token settings, browser profiles, personal email, or local absolute paths.
- Keep `AUTO_MERGE=false`; do not merge the demo PR. Do not record real credentials or personal information.
- Review the final export frame by frame and scan terminal text before uploading. Upload the actual full-flow MP4 to the repository's `docs/` folder, then replace the short-clip note and thumbnail link in `README.md` with that real file. Do not add a placeholder or invented external URL.

### Optional failing-CI variant

For a separate CI-failure walkthrough, the repository's `scripts/break_ci_demo.py` changes a genuine assertion in a new branch and prints the manual push/PR commands. Run it only in the approved disposable clone. That CI-fix path repairs an existing PR branch; it does not create the initial PR. The primary runtime-trigger flow above is the one that shows Selfheal opening the repair PR.
