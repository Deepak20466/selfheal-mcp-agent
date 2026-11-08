$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$FrozenConfig = "benchmarks/real_bugs/frozen/2026-11-08-gemini-3.8-flash-v3.json"
$FrozenHash = "benchmarks/real_bugs/frozen/2026-11-08-gemini-3.8-flash-v3.sha256"
$LeakageAudit = "benchmarks/real_bugs/frozen/2026-11-08-gemini-3.8-flash-v3-leakage-check.json"
$RunId = "2026-11-08-gemini-3.8-flash-v3"
$Results = "benchmarks/real_bugs/results"
$Runs = Join-Path $Results "runs"
$Logs = Join-Path $Results "logs"
$TranscriptPath = Join-Path $Logs "$RunId.log"
$HadAppDatabaseUrl = -not [string]::IsNullOrEmpty($env:DATABASE_URL)
$OriginalAppDatabaseUrl = $env:DATABASE_URL

New-Item -ItemType Directory -Force -Path $Runs, $Logs | Out-Null
if (-not (Test-Path -LiteralPath $FrozenConfig) -or -not (Test-Path -LiteralPath $FrozenHash)) {
    throw "The committed frozen case config or hash is missing."
}
$ExpectedHash = (Get-Content -LiteralPath $FrozenHash -Raw).Trim().Split()[0].ToLowerInvariant()
$ActualHash = (Get-FileHash -LiteralPath $FrozenConfig -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualHash -ne $ExpectedHash) { throw "Frozen case config hash mismatch." }
if (-not (Test-Path -LiteralPath $LeakageAudit)) { throw "The committed leakage audit is missing." }
$AuditReport = Get-Content -LiteralPath $LeakageAudit -Raw | ConvertFrom-Json
if (-not $AuditReport.passed -or $AuditReport.config_sha256 -ne $ActualHash) {
    throw "The leakage audit did not pass for this frozen config."
}

if ([string]::IsNullOrEmpty($env:GEMINI_API_KEY)) { throw "GEMINI_API_KEY is missing." }
if ([string]::IsNullOrEmpty($env:PGPASSWORD)) { throw "PGPASSWORD is missing." }
if ($env:I_CONFIRM_GEMINI_FREE_TIER_AND_NO_BILLING -cne "yes") {
    throw "The free-tier/no-billing confirmation is missing or invalid."
}
foreach ($Name in @(
    "REAL_BUG_FREE_TIER_CONFIRMED",
    "REAL_BUG_BILLING_DISABLED",
    "GEMINI_FREE_TIER_CONFIRMED",
    "GEMINI_38_FLASH_FREE_QUOTA"
)) {
    if ([Environment]::GetEnvironmentVariable($Name, "Process") -cne "true") {
        throw "Required operator attestation is missing or invalid."
    }
}

$EncodedPassword = [Uri]::EscapeDataString($env:PGPASSWORD)
$env:REAL_BUG_DATABASE_URL = "postgresql+asyncpg://postgres:$EncodedPassword@127.0.0.1:5434/selfheal_real_bug_test"
$env:REAL_BUG_PROVIDER = "gemini_api"
$env:GEMINI_API_MODEL = "gemini-3.8-flash"
if (-not $HadAppDatabaseUrl) {
    # Used only by the runner's database-name safety check; benchmark code
    # replaces DATABASE_URL with REAL_BUG_DATABASE_URL before connecting.
    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:5434/selfheal"
}

$Full = Join-Path $Runs "$RunId-full.jsonl"
$RepeatA = Join-Path $Runs "$RunId-repeat-a.jsonl"
$RepeatB = Join-Path $Runs "$RunId-repeat-b.jsonl"
$RunSpecs = @(
    @{ Path = $Full; Limit = 0 },
    @{ Path = $RepeatA; Limit = 20 },
    @{ Path = $RepeatB; Limit = 20 }
)

function Invoke-BenchmarkRun {
    param([string]$RunFile, [int]$Limit = 0)

    $RunArgs = @(
        "-m", "benchmarks.real_bugs.run",
        "--run-file", $RunFile,
        "--case-list", $FrozenConfig,
        "--token-cap", "40000",
        "--cost-cap-usd", "0",
        "--timeout", "240",
        "--resume"
    )
    if ($Limit -gt 0) { $RunArgs += @("--limit", "$Limit") }
    # The operator must verify the live model quota before invoking this script.
    # This fresh timestamp is generated immediately before each run/resume.
    $env:GEMINI_QUOTA_CHECKED_UTC = & $Python -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))"
    & $Python @RunArgs | Out-Host
    return $LASTEXITCODE
}

$TranscriptStarted = $false
$FinalStatus = 0
try {
    Start-Transcript -LiteralPath $TranscriptPath -Append | Out-Null
    $TranscriptStarted = $true
    foreach ($Spec in $RunSpecs) {
        $RunStatus = Invoke-BenchmarkRun -RunFile $Spec.Path -Limit $Spec.Limit
        if ($RunStatus -eq 75) {
            $Inputs = @($Full, $RepeatA, $RepeatB) | Where-Object {
                Test-Path -LiteralPath $_ -PathType Leaf
            }
            if ($Inputs.Count -gt 0) {
                & $Python -m benchmarks.real_bugs.score @Inputs --output-dir $Results
                if ($LASTEXITCODE -ne 0) { throw "Partial scoring failed." }
            }
            Write-Warning "Provider stopped safely. Attempt records and partial aggregates were saved."
            $FinalStatus = 75
            break
        }
        if ($RunStatus -ne 0) { throw "Benchmark run failed with exit code $RunStatus." }
    }
    if ($FinalStatus -eq 0) {
        & $Python -m benchmarks.real_bugs.score $Full $RepeatA $RepeatB --output-dir $Results
        if ($LASTEXITCODE -ne 0) { throw "Scoring failed." }
    }
} finally {
    if ($TranscriptStarted) { Stop-Transcript | Out-Null }
    Remove-Item Env:REAL_BUG_DATABASE_URL, Env:GEMINI_QUOTA_CHECKED_UTC -ErrorAction SilentlyContinue
    if (-not $HadAppDatabaseUrl) {
        Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    } else {
        $env:DATABASE_URL = $OriginalAppDatabaseUrl
    }
    Remove-Variable EncodedPassword, OriginalAppDatabaseUrl -ErrorAction SilentlyContinue
}

exit $FinalStatus
