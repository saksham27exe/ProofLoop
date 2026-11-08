<#
.SYNOPSIS
    One-time setup for ProofLoop on Windows (plain Python - no Docker).

.DESCRIPTION
    1. Finds Python 3.11+            5. Initialises the local SQLite database and sandboxes
    2. Creates .venv                 6. Checks Ollama and the selected model
    3. Installs dependencies         7. Offers (never forces) a model download
    4. Creates .env from .env.example

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -SkipModel           # never ask about downloading a model
    .\setup.ps1 -Dev                 # also install test/lint tools
    .\setup.ps1 -Python "C:\Python312\python.exe"
#>
[CmdletBinding()]
param(
    [switch]$SkipModel,
    [switch]$Dev,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "    [X]  $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  ProofLoop setup" -ForegroundColor Magenta
Write-Host "  Don't trust what an AI agent says it did. Verify what actually happened." -ForegroundColor DarkGray

# ---------------------------------------------------------------- Python
Step "Looking for Python 3.11 or newer"
$candidates = @()
if ($Python) { $candidates += , @($Python) }
if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @("py", "-3") }
if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @("python") }

$pyExe = $null; $pyArgs = @()
foreach ($cand in $candidates) {
    $exe = $cand[0]
    $extra = @($cand | Select-Object -Skip 1)
    try {
        $ErrorActionPreference = "Continue"
        $ver = & $exe @extra -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $parts = "$ver".Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
                $pyExe = $exe; $pyArgs = $extra
                Ok "Python $ver ($($cand -join ' '))"
                break
            }
            Warn "Found Python $ver via '$($cand -join ' ')' - ProofLoop needs 3.11+"
        }
    } catch { } finally { $ErrorActionPreference = "Stop" }
}
if (-not $pyExe) {
    Fail "Python 3.11+ was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and re-run."
}

if ($PSScriptRoot.Length -gt 150) {
    Warn "This folder path is $($PSScriptRoot.Length) characters long. Windows' 260-character path limit can break"
    Warn "Python packages inside .venv. If installation fails, move ProofLoop to a short path such as C:\ProofLoop."
}

# ---------------------------------------------------------------- venv
Step "Creating the virtual environment (.venv)"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Ok ".venv already exists"
} else {
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Could not create the virtual environment." }
    Ok ".venv created"
}

# ---------------------------------------------------------------- deps
Step "Installing dependencies (this can take a few minutes the first time)"
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPy -m pip install -r requirements.txt --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Dependency installation failed. See the pip output above." }
if ($Dev) {
    & $venvPy -m pip install -r requirements-dev.txt --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Fail "Dev dependency installation failed." }
}
$importOk = $false
try {
    $ErrorActionPreference = "Continue"
    & $venvPy -c "import fastapi, streamlit, sqlalchemy, pydantic_settings" 2>$null
    $importOk = ($LASTEXITCODE -eq 0)
} catch { $importOk = $false } finally { $ErrorActionPreference = "Stop" }
if (-not $importOk) { Fail "Dependencies are installed but cannot be imported (often a long-path problem - see above)." }
Ok "Dependencies installed"

# ---------------------------------------------------------------- config
Step "Preparing local configuration"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Ok "Created .env from .env.example (no secrets required)"
} else {
    Ok ".env already exists - left unchanged"
}

# ---------------------------------------------------------------- database
Step "Initialising the local database and synthetic sandboxes"
& $venvPy scripts\initialize_db.py
if ($LASTEXITCODE -ne 0) { Fail "Database initialisation failed." }

# ---------------------------------------------------------------- Ollama
Step "Checking the local LLM runtime (Ollama)"
$model = (& $venvPy -c "from app.config import get_settings; print(get_settings().ollama_model)").Trim()
$ollamaUrl = (& $venvPy -c "from app.config import get_settings; print(get_settings().ollama_url)").Trim()
$ollamaUp = $false
try {
    $version = (Invoke-RestMethod -Uri "$ollamaUrl/api/version" -TimeoutSec 3).version
    $ollamaUp = $true
    Ok "Ollama $version is running at $ollamaUrl"
} catch {
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        Warn "Ollama is installed but not running. Start the Ollama app (or run 'ollama serve')."
    } else {
        Warn "Ollama is not installed. Get it from https://ollama.com/download (optional)."
    }
    Warn "Until then ProofLoop runs in verification-only mode (deterministic rule-based interpreter)."
}

if ($ollamaUp) {
    $installed = @()
    try { $installed = @((Invoke-RestMethod -Uri "$ollamaUrl/api/tags" -TimeoutSec 5).models | ForEach-Object { $_.name }) } catch { }
    $want = if ($model.Contains(":")) { $model } else { "$model`:latest" }
    if ($installed -contains $want) {
        Ok "Model '$model' is installed"
    } elseif ($SkipModel) {
        Warn "Model '$model' is not installed (skipped). Later: .venv\Scripts\python.exe scripts\download_model.py"
    } else {
        Warn "Model '$model' is not installed. It is about 1.9 GB and needs internet access once."
        $answer = Read-Host "    Download '$model' now? [y/N]"
        if ($answer -match '^(y|yes)$') {
            & $venvPy scripts\download_model.py --model $model --yes
        } else {
            Warn "Skipped. Download later with: .venv\Scripts\python.exe scripts\download_model.py"
        }
    }
}

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host "  Start ProofLoop with:   .\run.ps1" -ForegroundColor White
Write-Host "  Then open:              http://localhost:8501  (create your admin account on first visit)" -ForegroundColor White
Write-Host ""
