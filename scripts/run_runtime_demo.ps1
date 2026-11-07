param(
    [ValidateSet("profile", "pagination", "quantity")]
    [string]$Bug = "profile",
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir
$Python = Join-Path $RootDir ".venv\Scripts\python.exe"
$Selfheal = Join-Path $RootDir ".venv\Scripts\selfheal.exe"
if (!(Test-Path $Python) -or !(Test-Path $Selfheal)) {
    throw "Install the project into .venv first (see docs/operations.md)."
}

& $Selfheal up
if ($LASTEXITCODE -ne 0) { throw "selfheal up failed with exit code $LASTEXITCODE" }
$PreviousToken = $env:SENTINEL_INGEST_TOKEN
$StdoutLog = Join-Path $env:TEMP "selfheal-runtime-demo.out.log"
$StderrLog = Join-Path $env:TEMP "selfheal-runtime-demo.err.log"
$DemoProcess = $null
try {
    $env:SENTINEL_INGEST_TOKEN = & $Python -c "import secrets; print(secrets.token_urlsafe(32))"
    & $Python scripts\sync_monitored_apps.py
    if ($LASTEXITCODE -ne 0) { throw "Monitored-app synchronization failed." }
    $StartedAt = & $Python -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())"
    $DemoProcess = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "examples.runtime_demo.main:app", "--host", "127.0.0.1", "--port", "8110") `
        -WorkingDirectory $RootDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
    & $Python examples\runtime_demo\trigger.py $Bug
    if ($LASTEXITCODE -ne 0) { throw "Runtime trigger failed." }
    & $Python scripts\wait_runtime_demo_job.py --since $StartedAt --timeout $TimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "No successful PR job was reported; see the healer logs." }
    Write-Host "Demo app stopped. Selfheal service processes remain under 'selfheal down' control."
}
finally {
    if ($DemoProcess -and !$DemoProcess.HasExited) {
        Stop-Process -Id $DemoProcess.Id -Force
    }
    if ($null -eq $PreviousToken) { Remove-Item Env:SENTINEL_INGEST_TOKEN -ErrorAction SilentlyContinue }
    else { $env:SENTINEL_INGEST_TOKEN = $PreviousToken }
}
