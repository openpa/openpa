<#
.SYNOPSIS
    OpenPA TEST installer for Windows (PowerShell).

.DESCRIPTION
    Identical to install.ps1 except it pulls pre-release builds from
    Test PyPI (https://test.pypi.org) instead of production PyPI. Use
    this to validate a release candidate end-to-end before cutting a
    real tag.

    Heads up: this installer shares ~/.openpa with the production
    installer. Running it on a host that already has prod openpa
    installed WILL upgrade/downgrade that install to the test version.
    Set $env:OPENPA_WORKING_DIR=~/.openpa-test to keep them separate.

.EXAMPLE
    iwr -useb https://openpa.ai/install-test.ps1 | iex

.EXAMPLE
    iwr -useb https://openpa.ai/install-test.ps1 -OutFile install-test.ps1
    .\install-test.ps1 -Deployment server -AppHost 100.120.175.90

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
    Don't modify the User-scope PATH. Defaults to true for the test
    installer when OPENPA_WORKING_DIR points outside the canonical
    install dir, so a staging install never clobbers prod's PATH entry.
#>

[CmdletBinding()]
param(
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
# Windows PowerShell 5.1 defaults [Console]::OutputEncoding to the OEM
# code page (cp437 on US-English Windows), which mojibakes multi-byte
# UTF-8 like the em-dashes and box-drawing chars in our section headers
# when stdout is consumed by a UTF-8 reader (the Electron log viewer).
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

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
    try { & $Action } finally { $ErrorActionPreference = $prev }
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
if ($ModifyPath -and $NoModifyPath) {
    Write-Err2 "-ModifyPath and -NoModifyPath are mutually exclusive"
    exit 2
}
if ($Unattended -and -not $AutoInstallPython -and -not $NoAutoInstallPython) {
    $AutoInstallPython = $true
}

# ── test-pypi config ──────────────────────────────────────────────────────

# Pip index URLs used for the native install and forwarded to the docker
# build via the docker-compose .env file. Test PyPI is the primary index
# (so ``pip install openpa`` resolves the test wheel); production PyPI is
# the fallback for transitive deps that don't live on Test PyPI.
$TestPyPIIndexUrl  = 'https://test.pypi.org/simple/'
$ProdPyPIExtraUrl  = 'https://pypi.org/simple/'

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

# Scope pip's cache under our install dir. Critical for the test installer:
# rapid test-wheel iteration can otherwise trip over Test PyPI's index-page
# caching at %LOCALAPPDATA%\pip\Cache, pinning pip to an older test build.
$env:PIP_CACHE_DIR = Join-Path $OpenpaHome 'pip-cache'

# Default PATH-mod behavior for the test installer: only modify PATH when
# OPENPA_HOME is the canonical install dir, so a staging install at
# ~/.openpa-test doesn't clobber prod's PATH entry. -ModifyPath /
# -NoModifyPath override.
$DefaultOpenpaHome = Join-Path $env:USERPROFILE '.openpa'
if (-not $ModifyPath -and -not $NoModifyPath) {
    if ($OpenpaHome -ieq $DefaultOpenpaHome) {
        $ModifyPath = $true
    } else {
        $NoModifyPath = $true
    }
}

if (-not (Test-Path $OpenpaHome)) { New-Item -ItemType Directory -Path $OpenpaHome | Out-Null }

$TemplateBase = if ($env:OPENPA_TEMPLATE_BASE) {
    $env:OPENPA_TEMPLATE_BASE
} else {
    'https://raw.githubusercontent.com/openpa/openpa/main/install/templates'
}

# ── banner ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "OpenPA TEST installer" -ForegroundColor Yellow
Write-Host "Installs from $TestPyPIIndexUrl" -ForegroundColor DarkGray
Write-Host "Targets $OpenpaHome (will overwrite an existing install in this directory)" -ForegroundColor DarkGray
Write-Host "Logs: $LogFile" -ForegroundColor DarkGray
Write-Host ""

# ── detection ─────────────────────────────────────────────────────────────

Write-Step "Environment"

$Arch = $env:PROCESSOR_ARCHITECTURE
Write-Ok "OS:   Windows ($Arch)"

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

if (-not $Mode) {
    if ($HasDocker) {
        if ($Unattended) {
            $Mode = 'docker'
        } else {
            Write-Host ""
            Write-Host "How do you want to run OpenPA?" -ForegroundColor White
            Write-Host "  1) docker  - sandboxed VNC desktop with bundled Postgres + Qdrant"
            Write-Host "               (recommended; the agent gets its own GUI environment)"
            Write-Host "  2) native  - Python venv at $OpenpaHome\venv with SQLite"
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
        # Append the Test PyPI index URLs so docker-compose forwards them
        # to the Dockerfile's pip install step. These keys are only
        # written by the test installer; the prod installer leaves them
        # unset (the Dockerfile treats empty as "use default PyPI").
        $rendered += "`nOPENPA_PIP_INDEX_URL=$TestPyPIIndexUrl`nOPENPA_PIP_EXTRA_INDEX_URL=$ProdPyPIExtraUrl`n"
        $rendered | Set-Content -Path $EnvDocker -Encoding utf8

        Write-Ok "Wrote $ComposeFile + .env"
    }

    Write-Info "Pulling images (this may take a few minutes the first time)"
    Push-Location $DockerDir
    try {
        Invoke-NativeLogged { & docker compose pull --ignore-pull-failures *>> $LogFile }
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "Some images couldn't be pulled; will build locally."
        }
        Write-Info "Starting bundle"
        Invoke-NativeLogged { & docker compose up -d --build *>> $LogFile }
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

    Write-Ok "Done. Welcome to OpenPA (test build)."
    exit 0
}

# ── native install ────────────────────────────────────────────────────────

function Write-PythonManualHint {
    Write-Host ""
    Write-Host "Install options:"
    Write-Host "  winget install Python.Python.3.13"
    Write-Host "  https://www.python.org/downloads/"
    Write-Host ""
    Write-Host "Re-run this script after Python is on your PATH (or pass -Mode docker)."
}

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
        Invoke-NativeLogged { Invoke-Expression (Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1').Content *>> $LogFile }
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

function Install-PythonViaUv {
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $OpenpaHome 'python'
    $env:UV_CACHE_DIR          = Join-Path $OpenpaHome 'uv-cache'
    Write-Info "Downloading isolated Python 3.13 (this may take a minute)"
    Invoke-NativeLogged { & $UvExe python install 3.13 *>> $LogFile }
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

if (-not $Python) {
    Confirm-AutoInstallPython
    Install-UvLocally
    Install-PythonViaUv
}

# ── existing install detection ────────────────────────────────────────────

Write-Step "Install"

if ($Reinstall -and (Test-Path $VenvDir)) {
    Write-Info "Removing existing venv (-Reinstall): $VenvDir"
    Remove-Item -Recurse -Force $VenvDir
}

$VenvPip = Join-Path $VenvDir 'Scripts\pip.exe'
$VenvOpenpa = Join-Path $VenvDir 'Scripts\openpa.exe'

# Resolve the latest openpa test wheel directly from Test PyPI's simple
# index, then pip-install that URL with prod PyPI as the only resolver
# for transitive deps. See install-test.sh for the rationale - Test PyPI
# is polluted with broken stubs (e.g. uppercase 'FASTAPI-1.0.tar.gz')
# and pre-release noise that beats prod stables once --pre is enabled.
Write-Info "Locating latest openpa test wheel"
$indexHtml = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/openpa/').Content
$wheelUrls = [regex]::Matches($indexHtml, 'https://[^"]*openpa-[^"]*-py3-none-any\.whl') |
    ForEach-Object { $_.Value } | Sort-Object -Unique
$OpenpaWheelUrl = $wheelUrls |
    Sort-Object -Property @{ Expression = { ($_ -split '/')[-1] } } |
    Select-Object -Last 1
if (-not $OpenpaWheelUrl) {
    Write-Err2 "No openpa wheel found at https://test.pypi.org/simple/openpa/"
    exit 1
}
Write-Ok ("Test wheel: " + (Split-Path $OpenpaWheelUrl -Leaf))

if (Test-Path $VenvDir) {
    Write-Info "Existing install detected at $VenvDir - upgrading in place."
    Invoke-NativeLogged { & $VenvPip install --upgrade pip *>> $LogFile }
    Invoke-NativeLogged { & $VenvPip install --upgrade $OpenpaWheelUrl *>> $LogFile }
} else {
    Write-Info "Creating venv at $VenvDir"
    Invoke-NativeLogged { & $Python -m venv $VenvDir *>> $LogFile }
    Write-Info "Installing openpa from Test PyPI (this may take a few minutes)"
    Invoke-NativeLogged { & $VenvPip install --upgrade pip *>> $LogFile }
    Invoke-NativeLogged { & $VenvPip install $OpenpaWheelUrl *>> $LogFile }
}

$InstalledVersion = ''
try { $InstalledVersion = (& $VenvOpenpa version 2>$null).Split(' ')[-1] } catch {}
if (-not $InstalledVersion) { $InstalledVersion = '?' }
Write-Ok "Installed openpa $InstalledVersion (test build)"

# ── shim & PATH ───────────────────────────────────────────────────────────

if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir | Out-Null
}
$OpenpaCmd = Join-Path $BinDir 'openpa.cmd'
@"
@echo off
"%~dp0..\venv\Scripts\openpa.exe" %*
"@ | Set-Content -Path $OpenpaCmd -Encoding ascii

$OpenpaPs1 = Join-Path $BinDir 'openpa.ps1'
@'
& "$PSScriptRoot\..\venv\Scripts\openpa.exe" $args
exit $LASTEXITCODE
'@ | Set-Content -Path $OpenpaPs1 -Encoding utf8

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
    if (-not (";$($env:Path);" -like "*;$target;*")) {
        $env:Path = "$($env:Path);$target"
    }
}

if ($NoModifyPath) {
    Write-Info "Skipping PATH modification (test installer / -NoModifyPath). Add manually if needed:"
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
            Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/local.env" -OutFile $EnvFile
        }
        'container' {
            Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/container.env" -OutFile $EnvFile
        }
        'server' {
            $tmpl = Invoke-WebRequest -UseBasicParsing -Uri "$TemplateBase/server.env.tmpl"
            ($tmpl.Content -replace '__APP_HOST__', $AppHost) | Set-Content -Path $EnvFile -Encoding utf8
        }
    }
    Write-Ok "Wrote $EnvFile"
} else {
    Write-Info ".env already exists — keeping it. Edit $EnvFile if you need to."
}

# Stamp test-channel keys into .env. The upgrader reads
# OPENPA_UPGRADE_CHANNEL to decide which feed to query, and the two
# pip-index URLs to point ``openpa upgrade -y`` at Test PyPI. Each
# write is idempotent — re-running the installer doesn't accumulate
# duplicate lines.
if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_UPGRADE_CHANNEL=' -Quiet)) {
    Add-Content -Path $EnvFile -Value "`nOPENPA_UPGRADE_CHANNEL=test" -Encoding utf8
}
if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_PIP_INDEX_URL=' -Quiet)) {
    Add-Content -Path $EnvFile -Value "OPENPA_PIP_INDEX_URL=$TestPyPIIndexUrl" -Encoding utf8
}
if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_PIP_EXTRA_INDEX_URL=' -Quiet)) {
    Add-Content -Path $EnvFile -Value "OPENPA_PIP_EXTRA_INDEX_URL=$ProdPyPIExtraUrl" -Encoding utf8
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
Invoke-NativeLogged { & $VenvOpenpa db upgrade *>> $LogFile }
$Revision = '?'
try { $Revision = (& $VenvOpenpa db current 2>$null) } catch {}
Write-Ok "Database at revision $Revision"

# ── start the server ──────────────────────────────────────────────────────

Write-Step "Starting OpenPA"

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
    $proc = Start-Process -FilePath $VenvOpenpa -ArgumentList 'serve' `
        -RedirectStandardOutput $ServerLogFile -RedirectStandardError $ServerLogFile `
        -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -Path $PidFile -Encoding ascii
    Write-Ok "OpenPA started (pid $($proc.Id), logs: $ServerLogFile)"
}

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

# 'container' binds to 0.0.0.0 inside the container, but the user
# browses from the docker host (where the published port surfaces as
# localhost) - same wizard URL as 'local'.
if ($Deployment -eq 'server') {
    $WizardUrl = "http://${AppHost}:1515/#/setup"
} else {
    $WizardUrl = 'http://localhost:1515/#/setup'
}

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

Write-Ok "Done. Welcome to OpenPA (test build)."
