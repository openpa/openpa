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
    'local', 'server', or 'container'. Skips the deployment-type prompt.
    'container' binds to 0.0.0.0 with localhost URLs - pick this when running
    the installer inside a Docker / Podman container and browsing from the
    docker host via published ports.

.PARAMETER AppHost
    Public IP/domain for server deployments.

.PARAMETER NoLaunch
    Skip opening the setup wizard at the end.

.PARAMETER Unattended
    Use defaults; never prompt. With Deployment='server', requires AppHost.

.PARAMETER Reinstall
    Wipe any existing %USERPROFILE%\.openpa\venv before installing.

.PARAMETER AutoInstallPython
    Auto-install isolated Python 3.13 if missing (default: prompt;
    -Unattended installs silently).

.PARAMETER NoAutoInstallPython
    Never auto-install; print manual hints and exit when Python is missing.

.PARAMETER NoModifyPath
    Don't modify the User-scope PATH; print the manual setx instead.

.PARAMETER Channel
    Install source: 'production' (PyPI, default), 'test' (Test PyPI, for
    release-candidate validation), or 'dev' (pip install -e from the local
    checkout). 'dev' requires running this script from a clone of the
    repo; rejected when piped via iwr|iex. 'dev' works with both -Mode
    native (reuses <repo>\.venv) and -Mode docker (compose override
    bind-mounts the checkout at /src).
#>

[CmdletBinding()]
param(
    [ValidateSet('production','test','dev')] [string] $Channel = 'production',
    [ValidateSet('local','server','container')] [string] $Deployment = '',
    [string] $AppHost = '',
    [ValidateSet('','docker','native')] [string] $Mode = '',
    [switch] $NoLaunch,
    [switch] $Unattended,
    [switch] $Reinstall,
    [switch] $AutoInstallPython,
    [switch] $NoAutoInstallPython,
    [switch] $ModifyPath,
    [switch] $NoModifyPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Channel is validated by the param()'s ValidateSet attribute; reaching
# this point implies $Channel is one of production / test / dev.

# RepoRoot is the repo containing this script. Required for dev mode (we
# install from there and read templates from there). Empty when piped via
# iwr|iex — the dev-mode check below uses that to reject pipe invocation.
$RepoRoot = ''
if ($PSCommandPath) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
if ($Channel -eq 'dev') {
    if (-not $RepoRoot -or -not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
        Write-Host "ERR -Channel dev requires running install.ps1 from a checkout (not via iwr|iex)." -ForegroundColor Red
        Write-Host "    Usage: .\install\install.ps1 -Channel dev" -ForegroundColor Red
        exit 2
    }
}
# Windows PowerShell 5.1 defaults [Console]::OutputEncoding to the OEM
# code page (cp437 on US-English Windows), which mojibakes multi-byte
# UTF-8 like the em-dashes and box-drawing chars in our section headers
# when stdout is consumed by a UTF-8 reader (the Electron log viewer).
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

# Pipeline-chain operators (`&&`, `||`) and ternaries are not available in
# Windows PowerShell 5.1. We stick to explicit if/else throughout so the
# script works on the default PS that ships with Windows 10/11.

# ── logging helpers ───────────────────────────────────────────────────────

function Write-Info  { param($Msg) Write-Host "==> $Msg"      -ForegroundColor Cyan }
function Write-Ok    { param($Msg) Write-Host " OK $Msg"      -ForegroundColor Green }
function Write-Warn2 { param($Msg) Write-Host "!!! $Msg"      -ForegroundColor Yellow }
function Write-Err2  { param($Msg) Write-Host "ERR $Msg"      -ForegroundColor Red }
function Write-Step  { param($Msg) Write-Host "`n── $Msg ──"  -ForegroundColor White }
# Native executables (docker, uv, pip) write progress to stderr; PS 5.1
# wraps each line as a NativeCommandError record, which trips
# $ErrorActionPreference='Stop' on benign output. We scope it to
# 'Continue' for the duration so the *>> redirect captures everything
# into $LogFile without aborting the script.
function Invoke-NativeLogged {
    param([Parameter(Mandatory)][scriptblock]$Action)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Redirect through Out-File -Encoding utf8 so the log isn't written
        # as PS 5.1's default UTF-16 LE (which makes every byte appear
        # spaced out in any UTF-8 reader).
        & $Action 2>&1 | Out-File -FilePath $script:LogFile -Encoding utf8 -Append
    } finally { $ErrorActionPreference = $prev }
}

# ── unattended sanity check ──────────────────────────────────────────────

if ($Unattended -and -not $Deployment) { $Deployment = 'local' }
if ($Unattended -and $Deployment -eq 'server' -and -not $AppHost) {
    Write-Err2 "-Unattended with -Deployment server requires -AppHost"
    exit 2
}
if ($AutoInstallPython -and $NoAutoInstallPython) {
    Write-Err2 "-AutoInstallPython and -NoAutoInstallPython are mutually exclusive"
    exit 2
}
# -Unattended implies "yes" for the auto-install prompt unless overridden.
if ($Unattended -and -not $AutoInstallPython -and -not $NoAutoInstallPython) {
    $AutoInstallPython = $true
}

# ── paths ─────────────────────────────────────────────────────────────────

$OpenpaHome = if ($env:OPENPA_WORKING_DIR) { $env:OPENPA_WORKING_DIR } else { Join-Path $env:USERPROFILE '.openpa' }
$VenvDir       = Join-Path $OpenpaHome 'venv'
$EnvFile       = Join-Path $OpenpaHome '.env'
$BootstrapFile = Join-Path $OpenpaHome 'bootstrap.toml'
$LogFile       = Join-Path $OpenpaHome 'install.log'
$PidFile       = Join-Path $OpenpaHome 'install.pid'
$ServerLogFile = Join-Path $OpenpaHome 'server.log'
$BinDir        = Join-Path $OpenpaHome 'bin'
$UvExe         = Join-Path $BinDir 'uv.exe'

# Scope pip's cache under our install dir so `Remove-Item -Recurse $OpenpaHome`
# (or -Reinstall) wipes any stale index responses. Without this, pip uses
# %LOCALAPPDATA%\pip\Cache, which persists across reinstalls.
$env:PIP_CACHE_DIR = Join-Path $OpenpaHome 'pip-cache'

if (-not (Test-Path $OpenpaHome)) { New-Item -ItemType Directory -Path $OpenpaHome | Out-Null }

# Default ModifyPath: only modify User PATH for the canonical install dir
# so a staging/test install at OPENPA_WORKING_DIR=~\.openpa-test doesn't
# clobber the prod PATH entry. -ModifyPath / -NoModifyPath override.
$DefaultOpenpaHome = Join-Path $env:USERPROFILE '.openpa'
if (-not $ModifyPath -and -not $NoModifyPath) {
    if ($OpenpaHome -ieq $DefaultOpenpaHome) {
        $ModifyPath = $true
    } else {
        $NoModifyPath = $true
    }
}

# Templates fetched at install time. Production/test fetch from GitHub;
# dev reads from the checkout's install\templates\ directly. The
# $TemplateMode flag tells Get-TemplateContent which branch to use.
if ($Channel -eq 'dev') {
    $TemplateBase = Join-Path $RepoRoot 'install\templates'
    $TemplateMode = 'local'
} else {
    $TemplateBase = if ($env:OPENPA_TEMPLATE_BASE) {
        $env:OPENPA_TEMPLATE_BASE
    } else {
        'https://raw.githubusercontent.com/openpa/openpa/main/install/templates'
    }
    $TemplateMode = 'remote'
}

# Centralized template loader. Returns the raw text of a template; the
# caller writes it to disk (after rendering) or to stdout. Local mode
# reads from the checkout; remote mode does an HTTP GET.
function Get-TemplateContent {
    param([Parameter(Mandatory)][string] $Name)
    if ($script:TemplateMode -eq 'local') {
        # Templates are authored as UTF-8 (em-dashes, box-drawing chars).
        # Windows PowerShell 5.1's ``Get-Content -Raw`` defaults to the
        # system ANSI codepage (Windows-1252 on US-English Windows), which
        # mojibakes multi-byte UTF-8. Force UTF8 so the rendered output
        # matches the template byte-for-byte.
        return Get-Content -Raw -Encoding UTF8 -Path (Join-Path $script:TemplateBase $Name)
    }
    return (Invoke-WebRequest -UseBasicParsing -Uri "$script:TemplateBase/$Name").Content
}

# ── banner ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "OpenPA installer" -ForegroundColor White
Write-Host "Logs: $LogFile" -ForegroundColor DarkGray
Write-Host ""

# Channel stamp: visible ONLY when non-production. End users never see it;
# CI / maintainers / devs do.
if ($Channel -ne 'production') {
    if ($Channel -eq 'dev') {
        Write-Host "==> channel: dev (source: $RepoRoot)" -ForegroundColor DarkGray
    } else {
        Write-Host "==> channel: $Channel" -ForegroundColor DarkGray
    }
    Write-Host ""
}

# ── detection ─────────────────────────────────────────────────────────────

Write-Step "Environment"

$Arch = $env:PROCESSOR_ARCHITECTURE
Write-Ok "OS:   Windows ($Arch)"

# Locate a Python 3.13+ interpreter. The Windows launcher ``py -3.13`` is
# the most reliable lookup; fall back to whatever ``python`` resolves to.
function Find-SystemPython {
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
                        return (& py -3.13 -c "import sys; print(sys.executable)").Trim()
                    } else {
                        return (Get-Command python).Source
                    }
                }
            } catch {
                # Fall through to the next candidate.
            }
        }
    }
    return $null
}
$Python = Find-SystemPython

if ($Python) {
    Write-Ok "Python: $(& $Python --version) at $Python"
} else {
    Write-Info "Python: 3.13+ not found (will auto-install in native mode)"
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
  1) local      - bind to 127.0.0.1, only this machine can reach it
  2) server     - bind to all interfaces, reachable from other devices
  3) container  - bind to 0.0.0.0; URLs use localhost
                  (pick this if you're running this script inside a
                   container and will browse from the docker host)
"@
    while (-not $Deployment) {
        $choice = Read-Host "Choice [1]"
        if (-not $choice) { $choice = '1' }
        switch ($choice) {
            '1'         { $Deployment = 'local' }
            'local'     { $Deployment = 'local' }
            '2'         { $Deployment = 'server' }
            'server'    { $Deployment = 'server' }
            '3'         { $Deployment = 'container' }
            'container' { $Deployment = 'container' }
            default     { Write-Warn2 "Pick 1, 2, or 3." }
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
        Get-TemplateContent 'docker-compose.yml.tmpl' | Set-Content -Path $ComposeFile -Encoding utf8

        Write-Info "Writing $EnvDocker (secrets, do not commit)"
        $rendered = (Get-TemplateContent 'docker.env.tmpl') `
            -replace '__OPENPA_VERSION__', $OpenpaVer `
            -replace '__OPENPA_UI_REF__',  $UiRef `
            -replace '__APP_URL__',        $DockerAppUrl `
            -replace '__CORS_ALLOWED_ORIGINS__', $DockerCors `
            -replace '__SETUP_WIZARD_ENV__', $DockerWizardEnv `
            -replace '__PG_PASSWORD__',    $PgPwd `
            -replace '__VNC_PASSWORD__',   $VncPwd
        $rendered | Set-Content -Path $EnvDocker -Encoding utf8

        # Test channel: forward Test PyPI indices into the Dockerfile build
        # via the compose .env. Prod leaves both unset (Dockerfile treats
        # empty as default PyPI). Dev uses ``-e /src`` via the override
        # file below, so pip index overrides don't apply.
        if ($Channel -eq 'test') {
            Add-Content -Path $EnvDocker -Value 'OPENPA_PIP_INDEX_URL=https://test.pypi.org/simple/' -Encoding utf8
            Add-Content -Path $EnvDocker -Value 'OPENPA_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/' -Encoding utf8
        }

        # Dev channel: emit a docker-compose.override.yml that points the
        # build context at the local checkout, switches the pip install
        # to ``-e /src``, and bind-mounts the checkout for runtime
        # imports. Compose auto-merges this when running from $DockerDir.
        # Backslashes are converted to forward slashes — Docker Desktop
        # accepts either, and forward slashes keep the YAML readable.
        if ($Channel -eq 'dev') {
            $OverrideFile = Join-Path $DockerDir 'docker-compose.override.yml'
            $RepoRootCompose = $RepoRoot -replace '\\', '/'
            Write-Info "Writing $OverrideFile (bind-mounts $RepoRoot at /src)"
            $overrideRendered = (Get-TemplateContent 'docker-compose.override.yml.tmpl') `
                -replace '__REPO_ROOT__', $RepoRootCompose
            $overrideRendered | Set-Content -Path $OverrideFile -Encoding utf8
        }

        Write-Ok "Wrote $ComposeFile + .env"
    }

    Write-Info "Pulling images (this may take a few minutes the first time)"
    Push-Location $DockerDir
    try {
        Invoke-NativeLogged { & docker compose pull --ignore-pull-failures }
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "Some images couldn't be pulled; will build locally."
        }
        Write-Info "Starting bundle"
        Invoke-NativeLogged { & docker compose up -d --build }
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

    # Suppress the human-handoff block when the Electron app is driving —
    # it navigates to the in-window wizard via vue-router and a "Wizard
    # URL: ..." instruction would mislead the user.
    $WizardUrl = "http://${HealthHost}:1515/#/setup"
    if ($env:OPENPA_INSTALLER_FRONTEND -ne 'electron') {
        Write-Step "Setup wizard"

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
    }

    if ($Channel -eq 'test') {
        Write-Ok "Done. Welcome to OpenPA (test build)."
    } else {
        Write-Ok "Done. Welcome to OpenPA."
    }
    exit 0
}

# ── native install ────────────────────────────────────────────────────────

# (Reaching here implies Mode = native. The Docker path exited above.)

function Write-PythonManualHint {
    Write-Host ""
    Write-Host "Install options:"
    Write-Host "  winget install Python.Python.3.13"
    Write-Host "  https://www.python.org/downloads/"
    Write-Host ""
    Write-Host "Re-run this script after Python is on your PATH (or pass -Mode docker)."
}

# Decide whether to auto-install Python. Honors -AutoInstallPython /
# -NoAutoInstallPython / -Unattended; otherwise prompts (default Yes).
function Confirm-AutoInstallPython {
    if ($NoAutoInstallPython) {
        Write-Err2 "Python 3.13 or newer is required for native mode but was not found."
        Write-PythonManualHint
        exit 1
    }
    if ($AutoInstallPython) { return }
    Write-Host ""
    Write-Host "OpenPA can install an isolated Python 3.13 just for itself"
    Write-Host "(~70 MB downloaded into $OpenpaHome\python, no admin needed; system"
    Write-Host "Python is left untouched)."
    Write-Host ""
    while ($true) {
        $choice = Read-Host "Install isolated Python 3.13 now? [Y/n]"
        if (-not $choice) { $choice = 'y' }
        switch ($choice.ToLower()) {
            'y'   { return }
            'yes' { return }
            'n'   {
                Write-Err2 "Aborted: Python 3.13 is required for native mode."
                Write-PythonManualHint
                exit 1
            }
            'no'  {
                Write-Err2 "Aborted: Python 3.13 is required for native mode."
                Write-PythonManualHint
                exit 1
            }
            default { Write-Warn2 "Please answer y or n." }
        }
    }
}

# Download a private copy of `uv` into $OpenpaHome\bin so we can manage
# Python and venv installs without touching system tools.
function Install-UvLocally {
    if (Test-Path $UvExe) {
        Write-Info "uv already installed at $UvExe"
        return
    }
    Write-Info "Installing uv into $BinDir"
    if (-not (Test-Path $BinDir)) {
        New-Item -ItemType Directory -Path $BinDir | Out-Null
    }
    $env:UV_INSTALL_DIR        = $BinDir
    $env:UV_UNMANAGED_INSTALL  = $BinDir
    $env:INSTALLER_NO_MODIFY_PATH = '1'
    try {
        # Run uv's official installer in a fresh powershell.exe so it gets a
        # clean environment. Inline ``Invoke-Expression`` would force the
        # upstream script through our ``Set-StrictMode -Version Latest``
        # (which faults on its uninitialised ``$LASTEXITCODE`` read) and our
        # ``$ErrorActionPreference='Stop'``. The child inherits our env
        # vars (UV_INSTALL_DIR, UV_UNMANAGED_INSTALL, INSTALLER_NO_MODIFY_PATH)
        # so the binary still lands in $BinDir.
        Invoke-NativeLogged {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://astral.sh/uv/install.ps1' | iex"
        }
        if ($LASTEXITCODE -ne 0) { throw "uv installer exited with code $LASTEXITCODE" }
    } catch {
        Write-Err2 "Failed to download uv (the Python installer)."
        Write-Host ""
        Write-Host "Possible causes: no internet, corporate TLS interception, or astral.sh"
        Write-Host "is blocked. Set HTTPS_PROXY if you're behind a proxy, or install"
        Write-Host "Python manually."
        Write-PythonManualHint
        exit 1
    }
    if (-not (Test-Path $UvExe)) {
        Write-Err2 "uv installer ran but $UvExe is missing - see $LogFile."
        exit 1
    }
    Write-Ok "Installed uv at $UvExe"
}

# Use uv to download an isolated Python 3.13 into $OpenpaHome\python and
# return its absolute path via $script:Python. The cache and install dir
# are scoped to OpenpaHome so removing the dir cleans everything.
function Install-PythonViaUv {
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $OpenpaHome 'python'
    $env:UV_CACHE_DIR          = Join-Path $OpenpaHome 'uv-cache'
    Write-Info "Downloading isolated Python 3.13 (this may take a minute)"
    Invoke-NativeLogged { & $UvExe python install 3.13 }
    if ($LASTEXITCODE -ne 0) {
        Write-Err2 "uv failed to install Python 3.13 - see $LogFile."
        exit 1
    }
    $found = (& $UvExe python find 3.13 2>$null).Trim()
    if (-not $found -or -not (Test-Path $found)) {
        Write-Err2 "Python install completed but the interpreter could not be located."
        exit 1
    }
    $script:Python = $found
    Write-Ok "Python: $(& $Python --version) at $Python (isolated)"
}

# Dev channel reuses the developer's local .venv (managed by uv) for the
# install, so we don't need a separate Python here. Skip the prompt and
# the isolated-Python bootstrap entirely.
if (-not $Python -and $Channel -ne 'dev') {
    Confirm-AutoInstallPython
    Install-UvLocally
    Install-PythonViaUv
}

# ── existing install detection ────────────────────────────────────────────

Write-Step "Install"

if ($Channel -eq 'dev') {
    # Dev channel: reuse the developer's local .venv (managed by
    # ``uv sync`` from <repo>\pyproject.toml) instead of building a
    # parallel venv at $OpenpaHome\venv. The dev already has openpa +
    # every transitive dep installed in editable mode; reinstalling
    # them into a separate venv takes minutes for no benefit.
    if ($Reinstall) {
        Write-Warn2 "-Reinstall has no effect in dev mode (the dev .venv is shared; refusing to wipe)."
    }
    $UiBundle = Join-Path $RepoRoot 'app\static\ui'
    if (-not (Test-Path $UiBundle)) {
        Write-Warn2 "Dev: $UiBundle is empty."
        Write-Warn2 "Run scripts\build_ui.sh once so the SPA listener can start."
    }
    $VenvDir    = Join-Path $RepoRoot '.venv'
    $VenvPip    = Join-Path $VenvDir 'Scripts\pip.exe'
    $VenvOpenpa = Join-Path $VenvDir 'Scripts\openpa.exe'
    if (-not (Test-Path $VenvOpenpa)) {
        Write-Err2 "Dev mode expects $VenvOpenpa to exist."
        Write-Err2 "Run 'uv sync' from $RepoRoot first, then re-run this installer."
        exit 1
    }
    Write-Info "Reusing dev .venv at $VenvDir (no pip install)"
    $InstalledVersion = ''
    try { $InstalledVersion = (& $VenvOpenpa version 2>$null).Split(' ')[-1] } catch {}
    if (-not $InstalledVersion) { $InstalledVersion = '?' }
    Write-Ok "Using openpa $InstalledVersion from dev .venv"
} else {
    if ($Reinstall -and (Test-Path $VenvDir)) {
        Write-Info "Removing existing venv (-Reinstall): $VenvDir"
        Remove-Item -Recurse -Force $VenvDir
    }

    $VenvPip = Join-Path $VenvDir 'Scripts\pip.exe'
    $VenvOpenpa = Join-Path $VenvDir 'Scripts\openpa.exe'

    # Resolve the spec passed to ``pip install``. Test pins a direct
    # wheel URL (Test PyPI's simple index is polluted, see the lengthy
    # rationale in the bash installer); production uses the bare
    # package name.
    $InstallSpec = $null
    $InstallSourceLabel = ''
    switch ($Channel) {
        'production' {
            $InstallSpec = 'openpa'
            $InstallSourceLabel = 'PyPI'
        }
        'test' {
            Write-Info "Locating latest openpa test wheel"
            # PS 5.1 has no built-in HTML parser; regex the simple-index page.
            $indexBody = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/openpa/').Content
            $wheelUrls = [regex]::Matches($indexBody, 'https://[^"]*openpa-[^"]*-py3-none-any\.whl') |
                ForEach-Object { $_.Value } |
                Select-Object -Unique |
                Sort-Object -Property @{ Expression = { Split-Path $_ -Leaf } }
            $InstallSpec = $wheelUrls | Select-Object -Last 1
            if (-not $InstallSpec) {
                Write-Err2 "No openpa wheel found at https://test.pypi.org/simple/openpa/"
                exit 1
            }
            Write-Ok ("Test wheel: " + (Split-Path $InstallSpec -Leaf))
            $InstallSourceLabel = 'Test PyPI'
        }
    }

    if (Test-Path $VenvDir) {
        Write-Info "Existing install detected at $VenvDir — upgrading in place."
        Invoke-NativeLogged { & $VenvPip install --upgrade pip }
        Invoke-NativeLogged { & $VenvPip install --upgrade $InstallSpec }
    } else {
        Write-Info "Creating venv at $VenvDir"
        Invoke-NativeLogged { & $Python -m venv $VenvDir }
        Write-Info "Installing openpa from $InstallSourceLabel (this may take a few minutes)"
        Invoke-NativeLogged { & $VenvPip install --upgrade pip }
        Invoke-NativeLogged { & $VenvPip install $InstallSpec }
    }

    $InstalledVersion = ''
    try { $InstalledVersion = (& $VenvOpenpa version 2>$null).Split(' ')[-1] } catch {}
    if (-not $InstalledVersion) { $InstalledVersion = '?' }
    Write-Ok "Installed openpa $InstalledVersion"
}

# ── shim & PATH ───────────────────────────────────────────────────────────

# Drop a small wrapper into $BinDir so a single, stable path on PATH
# points at the venv's openpa.exe even after re-installs. In dev mode
# the target lives in $RepoRoot\.venv, not $OpenpaHome\venv, so we emit
# the absolute path of $VenvOpenpa rather than a relative ``..\venv``
# walk that would dangle.
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir | Out-Null
}
$OpenpaCmd = Join-Path $BinDir 'openpa.cmd'
@"
@echo off
"$VenvOpenpa" %*
"@ | Set-Content -Path $OpenpaCmd -Encoding ascii

$OpenpaPs1 = Join-Path $BinDir 'openpa.ps1'
@"
& '$VenvOpenpa' `$args
exit `$LASTEXITCODE
"@ | Set-Content -Path $OpenpaPs1 -Encoding utf8

Write-Ok "Wrote shim $OpenpaCmd (-> venv\Scripts\openpa.exe)"

function Add-OpenPAPathEntry {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $target  = $BinDir
    $exists  = $false
    if ($current) {
        foreach ($entry in ($current -split ';')) {
            if ($entry.TrimEnd('\') -ieq $target.TrimEnd('\')) {
                $exists = $true
                break
            }
        }
    }
    if (-not $exists) {
        $newPath = if ($current) { "$current;$target" } else { $target }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Ok "Added $target to your User PATH"
    } else {
        Write-Info "$target is already on your User PATH"
    }
    # Make the current session see the change too.
    if (-not (";$($env:Path);" -like "*;$target;*")) {
        $env:Path = "$($env:Path);$target"
    }
}

if ($NoModifyPath) {
    Write-Info "Skipping PATH modification (-NoModifyPath). Add manually:"
    Write-Host '    $current = [Environment]::GetEnvironmentVariable(''Path'', ''User'')'
    Write-Host "    [Environment]::SetEnvironmentVariable('Path', `$current + ';$BinDir', 'User')"
} else {
    Add-OpenPAPathEntry
}

# ── env file ──────────────────────────────────────────────────────────────

if (-not (Test-Path $EnvFile)) {
    Write-Info "Generating $EnvFile"
    switch ($Deployment) {
        'local' {
            Get-TemplateContent 'local.env' | Set-Content -Path $EnvFile -Encoding utf8
        }
        'container' {
            Get-TemplateContent 'container.env' | Set-Content -Path $EnvFile -Encoding utf8
        }
        'server' {
            # __APP_HOST__ is the only placeholder; the user-provided host
            # gets substituted as-is (validated above).
            ((Get-TemplateContent 'server.env.tmpl') -replace '__APP_HOST__', $AppHost) |
                Set-Content -Path $EnvFile -Encoding utf8
        }
    }
    Write-Ok "Wrote $EnvFile"
} else {
    Write-Info ".env already exists — keeping it. Edit $EnvFile if you need to."
}

# Stamp the channel-specific keys into .env so the running app's upgrader
# reads them via the .env loader. Each write is idempotent (skipped if
# the key is already present) so re-runs don't accumulate duplicates.
switch ($Channel) {
    'production' {
        if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_UPGRADE_CHANNEL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value "`nOPENPA_UPGRADE_CHANNEL=production" -Encoding utf8
        }
    }
    'test' {
        if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_UPGRADE_CHANNEL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value "`nOPENPA_UPGRADE_CHANNEL=test" -Encoding utf8
        }
        if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_PIP_INDEX_URL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value 'OPENPA_PIP_INDEX_URL=https://test.pypi.org/simple/' -Encoding utf8
        }
        if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_PIP_EXTRA_INDEX_URL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value 'OPENPA_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/' -Encoding utf8
        }
    }
    'dev' {
        # Deliberately leave OPENPA_UPGRADE_CHANNEL unset. get_channel()
        # defaults to "production" when missing (app/upgrade/channel.py),
        # which is harmless: ``openpa upgrade`` from a dev editable install
        # is a footgun anyway and the dev path is to ``git pull``.
    }
}

# ── bootstrap.toml (DB selection) ─────────────────────────────────────────

# Skip the default-SQLite bootstrap.toml when the Electron app is driving
# — the Setup Wizard will write the file once the user picks a backend,
# and the backend boots in deferred-storage mode until then so no DB is
# materialised under ~/.openpa/storage before the user has chosen.
# Native installs (curl | sh, no Electron) get the SQLite default here,
# matching the legacy behavior; the wizard can still flip them to
# Postgres on first setup.
if ($env:OPENPA_INSTALLER_FRONTEND -ne 'electron') {
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
}

# ── migrate ───────────────────────────────────────────────────────────────

# Skip Alembic's ``upgrade head`` (and therefore creating the SQLite DB
# file) when the Electron app is driving — the app starts the backend
# only after the user clicks "Continue to Setup Wizard", and the backend
# is what eventually creates the DB. Keeping this here means a stray
# ~/.openpa/storage/openpa.db never shows up between the installer
# finishing and the user choosing to continue.
if ($env:OPENPA_INSTALLER_FRONTEND -ne 'electron') {
    Write-Info "Migrating database to current schema"
    Invoke-NativeLogged { & $VenvOpenpa db upgrade }
    $Revision = '?'
    try { $Revision = (& $VenvOpenpa db current 2>$null) } catch {}
    Write-Ok "Database at revision $Revision"
}

# ── start the server ──────────────────────────────────────────────────────

# Skip starting ``openpa serve`` when the Electron app is driving —
# the app spawns the backend itself once the user clicks Continue, and
# that's also when the SQLite DB is created.
if ($env:OPENPA_INSTALLER_FRONTEND -ne 'electron') {
    Write-Step "Starting OpenPA"

    # Load .env into the process so HOST/PORT propagate to the child
    # without us needing a TOML parser. Keys that look like KEY=VALUE
    # are honored; anything else is ignored.
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

    # Detect a server that's already bound to the port without going
    # through this installer (typical dev case: ``uv run openpa serve``
    # in a separate terminal). Starting a second openpa would just
    # collide on the bind, so treat it as already-running and skip the
    # spawn.
    if (-not $running) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://${ServerHost}:${ServerPort}/health" -TimeoutSec 2 | Out-Null
            Write-Info "OpenPA is already responding at http://${ServerHost}:${ServerPort} — skipping server start."
            $running = $true
        } catch { }
    }

    if (-not $running) {
        # Start-Process refuses identical stdout / stderr paths on PS
        # 5.1, so write stderr to a sibling file. They're both rotated
        # together via ``rm ~/.openpa/server*.log`` if the user wants a
        # clean slate.
        $ServerErrFile = Join-Path $OpenpaHome 'server.err.log'
        $proc = Start-Process -FilePath $VenvOpenpa -ArgumentList 'serve' `
            -RedirectStandardOutput $ServerLogFile -RedirectStandardError $ServerErrFile `
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
}

# ── wizard handoff ────────────────────────────────────────────────────────

# 'container' binds to 0.0.0.0 inside the container, but the user
# browses from the docker host (where the published port surfaces as
# localhost) - same wizard URL as 'local'.
if ($Deployment -eq 'server') {
    $WizardUrl = "http://${AppHost}:1515/#/setup"
} else {
    $WizardUrl = 'http://localhost:1515/#/setup'
}

# Suppress the human-handoff block when the Electron app is driving —
# it navigates to the in-window wizard via vue-router and a "Wizard
# URL: ..." instruction would mislead the user.
if ($env:OPENPA_INSTALLER_FRONTEND -ne 'electron') {
    Write-Step "Setup wizard"

    Write-Host @"
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

  Wizard URL: $WizardUrl
  Backend:    http://${ServerHost}:${ServerPort}
  Stop:       Stop-Process -Id (Get-Content $PidFile)
  Re-open:    openpa serve   (or: & "$VenvOpenpa" serve)

  Tip: open a NEW terminal so ``openpa`` is on PATH (already-open
       shells won't see the User PATH update).

"@

    if (-not $NoLaunch -and -not $Unattended) {
        Start-Process $WizardUrl
    }
}

switch ($Channel) {
    'test' { Write-Ok "Done. Welcome to OpenPA (test build)." }
    'dev'  { Write-Ok "Done. Welcome to OpenPA (dev install from $RepoRoot)." }
    default { Write-Ok "Done. Welcome to OpenPA." }
}
