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
& $Python -m benchmarks.real_bugs.run --run-file $Full --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
if ($LASTEXITCODE -ne 0) { throw "Full benchmark run failed with exit code $LASTEXITCODE." }
& $Python -m benchmarks.real_bugs.run --run-file $RepeatA --token-cap 40000 --cost-cap-usd 0 --timeout 240 --limit 20 --resume
if ($LASTEXITCODE -ne 0) { throw "First repeat run failed with exit code $LASTEXITCODE." }
& $Python -m benchmarks.real_bugs.run --run-file $RepeatB --token-cap 40000 --cost-cap-usd 0 --timeout 240 --limit 20 --resume
if ($LASTEXITCODE -ne 0) { throw "Second repeat run failed with exit code $LASTEXITCODE." }
& $Python -m benchmarks.real_bugs.score $Full $RepeatA $RepeatB --output-dir $Results
if ($LASTEXITCODE -ne 0) { throw "Scoring failed with exit code $LASTEXITCODE." }
