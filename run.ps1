<#
.SYNOPSIS
    Start ProofLoop: FastAPI backend (127.0.0.1:8000) + Streamlit dashboard (127.0.0.1:8501).

.DESCRIPTION
    Checks the environment, prepares the database, starts both processes, waits until they are healthy,
    opens the dashboard and stops everything cleanly when you press Ctrl+C.
    Ollama is detected, never started or required.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -NoBrowser
    .\run.ps1 -ApiPort 8010 -UiPort 8510
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$UiPort = 8501,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "    [X]  $m" -ForegroundColor Red; exit 1 }

function Test-PortInUse([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(300) -and $client.Connected
        return $connected
    } catch { return $false } finally { $client.Close() }
}

function Wait-Http([string]$Url, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

function Stop-Tree($proc) {
    $ErrorActionPreference = "Continue"
    if ($proc -and -not $proc.HasExited) {
        & taskkill.exe /PID $proc.Id /T /F *> $null
    }
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Fail "The virtual environment is missing. Run .\setup.ps1 first." }

Write-Host ""
Write-Host "  ProofLoop" -ForegroundColor Magenta -NoNewline
Write-Host "  - independent outcome verification for AI agents" -ForegroundColor DarkGray

$env:PROOFLOOP_API_PORT = "$ApiPort"
Step "Checking the environment"
& $py scripts\check_environment.py --ui-port $UiPort
if ($LASTEXITCODE -ne 0) { Fail "Environment check failed (see above). Re-run .\setup.ps1." }

Step "Preparing the local database"
& $py scripts\initialize_db.py | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Database initialisation failed." }
Ok "Database ready"

foreach ($p in @($ApiPort, $UiPort)) {
    if (Test-PortInUse $p) { Fail "Port $p is already in use. Stop the other program or pass -ApiPort/-UiPort." }
}

$logDir = Join-Path $PSScriptRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:PROOFLOOP_API_URL = "http://127.0.0.1:$ApiPort"

$api = $null; $ui = $null
try {
    Step "Starting the backend (FastAPI)"
    $api = Start-Process -FilePath $py -PassThru -NoNewWindow `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
        -RedirectStandardOutput (Join-Path $logDir "api.out.log") -RedirectStandardError (Join-Path $logDir "api.err.log")
    if (-not (Wait-Http "http://127.0.0.1:$ApiPort/health" 45)) {
        Fail "The backend did not start. See data\logs\api.err.log"
    }
    Ok "Backend:   http://127.0.0.1:$ApiPort   (API docs: http://localhost:$ApiPort/docs)"

    Step "Starting the dashboard (Streamlit)"
    $ui = Start-Process -FilePath $py -PassThru -NoNewWindow `
        -ArgumentList @("-m", "streamlit", "run", "frontend/app.py", "--server.port", "$UiPort", "--server.address", "127.0.0.1",
                        "--server.headless", "true", "--browser.gatherUsageStats", "false") `
        -RedirectStandardOutput (Join-Path $logDir "ui.out.log") -RedirectStandardError (Join-Path $logDir "ui.err.log")
    if (-not (Wait-Http "http://127.0.0.1:$UiPort/_stcore/health" 60)) {
        Fail "The dashboard did not start. See data\logs\ui.err.log"
    }
    Ok "Dashboard: http://localhost:$UiPort"

    try {
        $v = (Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 2).version
        Ok "Ollama $v detected (local LLM interpretation available if the model is installed)"
    } catch {
        Warn "Ollama not detected - running in verification-only mode (rule-based interpretation)."
    }

    if (-not $NoBrowser) { Start-Process "http://localhost:$UiPort" | Out-Null }

    Write-Host ""
    Write-Host "  ProofLoop is running. Press Ctrl+C to stop." -ForegroundColor Green
    Write-Host "  Logs: data\logs\" -ForegroundColor DarkGray
    while (-not $api.HasExited -and -not $ui.HasExited) { Start-Sleep -Seconds 1 }
    if ($api.HasExited) { Warn "The backend stopped unexpectedly - see data\logs\api.err.log" }
    if ($ui.HasExited) { Warn "The dashboard stopped unexpectedly - see data\logs\ui.err.log" }
}
finally {
    Write-Host "`n==> Stopping ProofLoop..." -ForegroundColor Cyan
    Stop-Tree $ui
    Stop-Tree $api
    Write-Host "    Stopped." -ForegroundColor Green
}
