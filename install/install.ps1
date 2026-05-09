<#
.SYNOPSIS
    OpenPA installer for Windows (PowerShell).

.DESCRIPTION
    The Phase 2 installer. Native install only — Docker mode is detected
    and recommended, but the actual containerized bundle ships in Phase 3.

.EXAMPLE
    iwr -useb https://openpa.ai/install.ps1 | iex

.EXAMPLE
    iwr -useb https://openpa.ai/install.ps1 -OutFile install.ps1
    .\install.ps1 -Deployment server -Host 100.120.175.90

.PARAMETER Deployment
    'local' or 'server'. Skips the deployment-type prompt.

.PARAMETER AppHost
    Public IP/domain for server deployments.

.PARAMETER NoLaunch
    Skip opening the setup wizard at the end.

.PARAMETER Unattended
    Use defaults; never prompt. With Deployment='server', requires AppHost.

.PARAMETER Reinstall
    Wipe any existing %USERPROFILE%\.openpa\venv before installing.
#>

[CmdletBinding()]
param(
    [ValidateSet('local','server')] [string] $Deployment = '',
    [string] $AppHost = '',
    [ValidateSet('','docker','native')] [string] $Mode = '',
    [switch] $NoLaunch,
    [switch] $Unattended,
    [switch] $Reinstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Pipeline-chain operators (`&&`, `||`) and ternaries are not available in
# Windows PowerShell 5.1. We stick to explicit if/else throughout so the
# script works on the default PS that ships with Windows 10/11.

# ── logging helpers ───────────────────────────────────────────────────────

function Write-Info  { param($Msg) Write-Host "==> $Msg"      -ForegroundColor Cyan }
function Write-Ok    { param($Msg) Write-Host " OK $Msg"      -ForegroundColor Green }
function Write-Warn2 { param($Msg) Write-Host "!!! $Msg"      -ForegroundColor Yellow }
function Write-Err2  { param($Msg) Write-Host "ERR $Msg"      -ForegroundColor Red }
function Write-Step  { param($Msg) Write-Host "`n── $Msg ──"  -ForegroundColor White }

# ── unattended sanity check ──────────────────────────────────────────────

if ($Unattended -and -not $Deployment) { $Deployment = 'local' }
if ($Unattended -and $Deployment -eq 'server' -and -not $AppHost) {
    Write-Err2 "-Unattended with -Deployment server requires -AppHost"
    exit 2
}

# ── paths ─────────────────────────────────────────────────────────────────

$OpenpaHome = if ($env:OPENPA_WORKING_DIR) { $env:OPENPA_WORKING_DIR } else { Join-Path $env:USERPROFILE '.openpa' }
$VenvDir       = Join-Path $OpenpaHome 'venv'
$EnvFile       = Join-Path $OpenpaHome '.env'
$BootstrapFile = Join-Path $OpenpaHome 'bootstrap.toml'
$LogFile       = Join-Path $OpenpaHome 'install.log'
$PidFile       = Join-Path $OpenpaHome 'install.pid'
$ServerLogFile = Join-Path $OpenpaHome 'server.log'

if (-not (Test-Path $OpenpaHome)) { New-Item -ItemType Directory -Path $OpenpaHome | Out-Null }

# Templates fetched at install time. OPENPA_TEMPLATE_BASE override is for testing.
$TemplateBase = if ($env:OPENPA_TEMPLATE_BASE) {
    $env:OPENPA_TEMPLATE_BASE
} else {
    'https://raw.githubusercontent.com/openpa/openpa/main/install/templates'
}

# ── banner ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "OpenPA installer" -ForegroundColor White
Write-Host "Logs: $LogFile" -ForegroundColor DarkGray
Write-Host ""

# ── detection ─────────────────────────────────────────────────────────────

Write-Step "Environment"

$Arch = $env:PROCESSOR_ARCHITECTURE
Write-Ok "OS:   Windows ($Arch)"

# Locate a Python 3.13+ interpreter. The Windows launcher ``py -3.13`` is
# the most reliable lookup; fall back to whatever ``python`` resolves to.
$Python = $null
$candidates = @(
    @{ Cmd = 'py'; Args = @('-3.13','-c','import sys; print("%d.%d" % sys.version_info[:2])') },
    @{ Cmd = 'python'; Args = @('-c','import sys; print("%d.%d" % sys.version_info[:2])') }
)
foreach ($c in $candidates) {
    if (Get-Command $c.Cmd -ErrorAction SilentlyContinue) {
        try {
            $ver = & $c.Cmd @($c.Args) 2>$null
            if ($ver -match '^3\.(1[3-9]|[2-9]\d)$') {
                if ($c.Cmd -eq 'py') {
                    # Resolve the launcher to the actual exe so subprocesses
                    # don't depend on the launcher's heuristics.
                    $Python = (& py -3.13 -c "import sys; print(sys.executable)").Trim()
                } else {
                    $Python = (Get-Command python).Source
                }
                break
            }
        } catch {
            # Fall through to the next candidate.
        }
    }
}

if ($Python) {
    Write-Ok "Python: $(& $Python --version) at $Python"
} else {
    Write-Info "Python: 3.13+ not found (only required for native mode)"
}

$HasDocker = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        & docker info 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $HasDocker = $true }
    } catch {}
}
if ($HasDocker) {
    Write-Ok "Docker: detected (recommended in a future release)"
} else {
    Write-Info "Docker: not detected (or not running)"
}

# ── deployment type ───────────────────────────────────────────────────────

Write-Step "Deployment"

if (-not $Deployment) {
    Write-Host @"
How will you run OpenPA?
  1) local   — bind to 127.0.0.1, only this machine can reach it
  2) server  — bind to all interfaces, reachable from other devices
"@
    while (-not $Deployment) {
        $choice = Read-Host "Choice [1]"
        if (-not $choice) { $choice = '1' }
        switch ($choice) {
            '1'      { $Deployment = 'local' }
            'local'  { $Deployment = 'local' }
            '2'      { $Deployment = 'server' }
            'server' { $Deployment = 'server' }
            default  { Write-Warn2 "Pick 1 or 2." }
        }
    }
}
Write-Ok "Deployment: $Deployment"

if ($Deployment -eq 'server' -and -not $AppHost) {
    while (-not $AppHost) {
        $AppHost = Read-Host "Public IP or domain (e.g. 100.120.175.90 or openpa.example.com)"
        if (-not $AppHost) {
            Write-Warn2 "Required for server deployment."
        } elseif ($AppHost -notmatch '^[A-Za-z0-9\.\:\-]+$') {
            Write-Warn2 "Invalid characters; use letters, digits, dot, colon, hyphen."
            $AppHost = ''
        }
    }
}
if ($AppHost) { Write-Ok "Host: $AppHost" }

# ── mode (docker vs native) ──────────────────────────────────────────────

# Default: docker if available, native otherwise. The agent runs in a
# sandboxed VNC desktop by default — opt out via -Mode native.
if (-not $Mode) {
    if ($HasDocker) {
        if ($Unattended) {
            $Mode = 'docker'
        } else {
            Write-Host ""
            Write-Host "How do you want to run OpenPA?" -ForegroundColor White
            Write-Host "  1) docker  - sandboxed VNC desktop with bundled Postgres + Qdrant"
            Write-Host "               (recommended; the agent gets its own GUI environment)"
            Write-Host "  2) native  - Python venv at %USERPROFILE%\.openpa\venv with SQLite"
            Write-Host "               (simpler, but the agent shares your desktop)"
            while (-not $Mode) {
                $choice = Read-Host "Choice [1]"
                if (-not $choice) { $choice = '1' }
                switch ($choice) {
                    '1'      { $Mode = 'docker' }
                    'docker' { $Mode = 'docker' }
                    '2'      { $Mode = 'native' }
                    'native' { $Mode = 'native' }
                    default  { Write-Warn2 "Pick 1 or 2." }
                }
            }
        }
    } else {
        $Mode = 'native'
    }
}
Write-Ok "Mode: $Mode"

if ($Mode -eq 'docker' -and -not $HasDocker) {
    Write-Err2 "Docker mode requested but Docker is not available."
    exit 1
}

# ── docker install ────────────────────────────────────────────────────────

# Random secret generator. ``RNGCryptoServiceProvider`` would be the
# pedantically-correct choice, but ``Get-Random`` with a wide enough
# alphabet is sufficient for non-crypto-grade install-time secrets that
# the user controls and can rotate.
function New-Secret {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
}

function Resolve-OpenPAVersion {
    if ($Python) {
        try {
            $v = & $Python -c "from importlib.metadata import version, PackageNotFoundError`ntry:`n    print(version('openpa'))`nexcept PackageNotFoundError:`n    pass" 2>$null
            if ($v) { return $v.Trim() }
        } catch {}
    }
    return 'main'
}

if ($Mode -eq 'docker') {
    Write-Step "Docker install"

    $DockerDir = Join-Path $OpenpaHome 'docker'
    if (-not (Test-Path $DockerDir)) { New-Item -ItemType Directory -Path $DockerDir | Out-Null }

    $ComposeFile = Join-Path $DockerDir 'docker-compose.yml'
    $EnvDocker   = Join-Path $DockerDir '.env'

    if ((Test-Path $ComposeFile) -and (Test-Path $EnvDocker) -and (-not $Reinstall)) {
        Write-Info "Existing Docker bundle detected at $DockerDir - reusing config."
    } else {
        $VncPwd      = New-Secret
        $PgPwd       = New-Secret
        $OpenpaVer   = Resolve-OpenPAVersion
        $UiRef       = if ($env:OPENPA_UI_REF) { $env:OPENPA_UI_REF } else { 'main' }

        if ($Deployment -eq 'local') {
            $DockerAppUrl    = 'http://localhost:1112'
            $DockerCors      = 'http://localhost:1515,http://127.0.0.1:1515'
            $DockerWizardEnv = 'local'
        } else {
            $DockerAppUrl    = "http://${AppHost}:1112"
            $DockerCors      = "http://${AppHost}:1515,http://localhost:1515"
            $DockerWizardEnv = 'server'
        }

        Write-Info "Fetching docker-compose template"
        Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/docker-compose.yml.tmpl" -OutFile $ComposeFile

        Write-Info "Writing $EnvDocker (secrets, do not commit)"
        $tmpl = Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/docker.env.tmpl"
        $rendered = $tmpl.Content `
            -replace '__OPENPA_VERSION__', $OpenpaVer `
            -replace '__OPENPA_UI_REF__',  $UiRef `
            -replace '__APP_URL__',        $DockerAppUrl `
            -replace '__CORS_ALLOWED_ORIGINS__', $DockerCors `
            -replace '__SETUP_WIZARD_ENV__', $DockerWizardEnv `
            -replace '__PG_PASSWORD__',    $PgPwd `
            -replace '__VNC_PASSWORD__',   $VncPwd
        $rendered | Set-Content -Path $EnvDocker -Encoding utf8

        Write-Ok "Wrote $ComposeFile + .env"
    }

    Write-Info "Pulling images (this may take a few minutes the first time)"
    Push-Location $DockerDir
    try {
        & docker compose pull --ignore-pull-failures *>> $LogFile
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "Some images couldn't be pulled; will build locally."
        }
        Write-Info "Starting bundle"
        & docker compose up -d --build *>> $LogFile
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose up failed (see $LogFile)"
        }
    } finally {
        Pop-Location
    }

    if ($Deployment -eq 'local') {
        $HealthHost = 'localhost'
    } else {
        $HealthHost = $AppHost
    }
    $HealthUrl = "http://${HealthHost}:1112/health"
    Write-Info "Waiting for backend at $HealthUrl ..."
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2 | Out-Null
            Write-Ok "Backend is up"
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }

    Write-Step "Setup wizard"

    $WizardUrl = "http://${HealthHost}:1515/#/setup"
    $NoVncUrl  = "http://${HealthHost}:6080/vnc.html"
    $StoredVnc = (Get-Content $EnvDocker | Where-Object { $_ -like 'VNC_PASSWORD=*' } | Select-Object -First 1) -replace '^VNC_PASSWORD=', ''

    Write-Host @"
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

  Wizard URL  : $WizardUrl
  Backend     : http://${HealthHost}:1112
  Desktop     : $NoVncUrl
  VNC password (saved to $EnvDocker):
    $StoredVnc

  Stop:    cd $DockerDir; docker compose down
  Logs:    cd $DockerDir; docker compose logs -f openpa
  Restart: cd $DockerDir; docker compose restart openpa

"@

    if (-not $NoLaunch -and -not $Unattended) {
        Start-Process $WizardUrl
    }

    Write-Ok "Done. Welcome to OpenPA."
    exit 0
}

# ── native install ────────────────────────────────────────────────────────

# (Reaching here implies Mode = native. The Docker path exited above.)
if (-not $Python) {
    Write-Err2 "Python 3.13 or newer is required for native mode but was not found."
    Write-Host ""
    Write-Host "Install options:"
    Write-Host "  winget install Python.Python.3.13"
    Write-Host "  https://www.python.org/downloads/"
    Write-Host ""
    Write-Host "Re-run this script after Python is on your PATH (or pass -Mode docker)."
    exit 1
}

# ── existing install detection ────────────────────────────────────────────

Write-Step "Install"

if ($Reinstall -and (Test-Path $VenvDir)) {
    Write-Info "Removing existing venv (-Reinstall): $VenvDir"
    Remove-Item -Recurse -Force $VenvDir
}

$VenvPip = Join-Path $VenvDir 'Scripts\pip.exe'
$VenvOpa = Join-Path $VenvDir 'Scripts\opa.exe'

if (Test-Path $VenvDir) {
    Write-Info "Existing install detected at $VenvDir — upgrading in place."
    & $VenvPip install --upgrade pip *>> $LogFile
    & $VenvPip install --upgrade 'openpa[server]' *>> $LogFile
} else {
    Write-Info "Creating venv at $VenvDir"
    & $Python -m venv $VenvDir *>> $LogFile
    Write-Info "Installing openpa[server] from PyPI (this may take a few minutes)"
    & $VenvPip install --upgrade pip *>> $LogFile
    & $VenvPip install 'openpa[server]' *>> $LogFile
}

$InstalledVersion = ''
try { $InstalledVersion = (& $VenvOpa version 2>$null).Split(' ')[-1] } catch {}
if (-not $InstalledVersion) { $InstalledVersion = '?' }
Write-Ok "Installed openpa $InstalledVersion"

# ── env file ──────────────────────────────────────────────────────────────

if (-not (Test-Path $EnvFile)) {
    Write-Info "Generating $EnvFile"
    if ($Deployment -eq 'local') {
        Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/local.env" -OutFile $EnvFile
    } else {
        $tmpl = Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/server.env.tmpl"
        # __APP_HOST__ is the only placeholder; the user-provided host gets
        # substituted as-is (it was validated above).
        ($tmpl.Content -replace '__APP_HOST__', $AppHost) | Set-Content -Path $EnvFile -Encoding utf8
    }
    Write-Ok "Wrote $EnvFile"
} else {
    Write-Info ".env already exists — keeping it. Edit $EnvFile if you need to."
}

# ── bootstrap.toml (DB selection) ─────────────────────────────────────────

if (-not (Test-Path $BootstrapFile)) {
    Write-Info "Generating $BootstrapFile (SQLite, the recommended default)"
    @"
# Database selection. SQLite is the recommended default for native
# installs; switch to "postgres" via the setup wizard if you want a
# multi-process or networked DB.
db_provider = "sqlite"
"@ | Set-Content -Path $BootstrapFile -Encoding utf8
    Write-Ok "Wrote $BootstrapFile"
}

# ── migrate ───────────────────────────────────────────────────────────────

Write-Info "Migrating database to current schema"
& $VenvOpa db upgrade *>> $LogFile
$Revision = '?'
try { $Revision = (& $VenvOpa db current 2>$null) } catch {}
Write-Ok "Database at revision $Revision"

# ── start the server ──────────────────────────────────────────────────────

Write-Step "Starting OpenPA"

# Load .env into the process so HOST/PORT propagate to the child without
# us needing a TOML parser. Keys that look like KEY=VALUE are honored;
# anything else is ignored.
$ParsedEnv = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
        $idx = $line.IndexOf('=')
        $k = $line.Substring(0, $idx).Trim()
        $v = $line.Substring($idx + 1).Trim()
        $ParsedEnv[$k] = $v
        Set-Item -Path "Env:$k" -Value $v
    }
}

$ServerHost = if ($ParsedEnv.ContainsKey('HOST')) { $ParsedEnv['HOST'] } else { '127.0.0.1' }
$ServerPort = if ($ParsedEnv.ContainsKey('PORT')) { $ParsedEnv['PORT'] } else { '1112' }

$running = $false
if (Test-Path $PidFile) {
    $existingPid = Get-Content $PidFile | Select-Object -First 1
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        Write-Info "OpenPA is already running (pid $existingPid)."
        $running = $true
    }
}

if (-not $running) {
    $proc = Start-Process -FilePath $VenvOpa -ArgumentList 'serve' `
        -RedirectStandardOutput $ServerLogFile -RedirectStandardError $ServerLogFile `
        -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -Path $PidFile -Encoding ascii
    Write-Ok "OpenPA started (pid $($proc.Id), logs: $ServerLogFile)"
}

# Wait briefly for the HTTP listener so the wizard URL doesn't 404.
$healthUrl = "http://${ServerHost}:${ServerPort}/health"
for ($i = 0; $i -lt 10; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2 | Out-Null
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}

# ── wizard handoff ────────────────────────────────────────────────────────

Write-Step "Setup wizard"

if ($Deployment -eq 'local') {
    $WizardUrl = 'http://localhost:1515/#/setup'
} else {
    $WizardUrl = "http://${AppHost}:1515/#/setup"
}

Write-Host @"
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

  Wizard URL: $WizardUrl
  Backend:    http://${ServerHost}:${ServerPort}
  Stop:       Stop-Process -Id (Get-Content $PidFile)
  Re-open:    & "$VenvOpa" serve

"@

if (-not $NoLaunch -and -not $Unattended) {
    Start-Process $WizardUrl
}

Write-Ok "Done. Welcome to OpenPA."
