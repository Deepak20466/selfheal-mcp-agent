$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Date = & $Python -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%d'))"
$Results = "benchmarks/real_bugs/results"
$Runs = Join-Path $Results "runs"
New-Item -ItemType Directory -Force -Path $Runs | Out-Null

if (!$env:REAL_BUG_PROVIDER) { throw "Set REAL_BUG_PROVIDER to groq_api or openrouter_api and configure its free-tier key." }
if (!$env:REAL_BUG_DATABASE_URL) { throw "Set REAL_BUG_DATABASE_URL to a running, disposable local PostgreSQL database ending in _test." }

& $Python -m benchmarks.real_bugs.mine
if ($LASTEXITCODE -ne 0) { throw "Historical bug mining failed with exit code $LASTEXITCODE." }
& $Python -m benchmarks.real_bugs.prepare
if ($LASTEXITCODE -ne 0) { throw "Historical bug preparation failed with exit code $LASTEXITCODE." }
$Full = Join-Path $Runs "$Date-full.jsonl"
$RepeatA = Join-Path $Runs "$Date-repeat-a.jsonl"
$RepeatB = Join-Path $Runs "$Date-repeat-b.jsonl"

function Invoke-BenchmarkRun {
    param([string]$RunFile, [int]$Limit = 0)

    $RunArgs = @("-m", "benchmarks.real_bugs.run", "--run-file", $RunFile,
        "--token-cap", "40000", "--cost-cap-usd", "0", "--timeout", "240", "--resume")
    if ($Limit -gt 0) { $RunArgs += @("--limit", "$Limit") }
    & $Python @RunArgs
    $RunStatus = $LASTEXITCODE
    if ($RunStatus -eq 75) {
        $Inputs = @($Full, $RepeatA, $RepeatB) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
        if ($Inputs.Count -gt 0) {
            & $Python -m benchmarks.real_bugs.score @Inputs --output-dir $Results
            if ($LASTEXITCODE -ne 0) { throw "Partial benchmark scoring failed with exit code $LASTEXITCODE." }
        }
        Write-Warning "Benchmark stopped on provider cooldown. Raw records and partial aggregates were saved."
        exit 75
    }
    if ($RunStatus -ne 0) { throw "Benchmark run failed with exit code $RunStatus." }
}

Invoke-BenchmarkRun -RunFile $Full
Invoke-BenchmarkRun -RunFile $RepeatA -Limit 20
Invoke-BenchmarkRun -RunFile $RepeatB -Limit 20
& $Python -m benchmarks.real_bugs.score $Full $RepeatA $RepeatB --output-dir $Results
if ($LASTEXITCODE -ne 0) { throw "Scoring failed with exit code $LASTEXITCODE." }
