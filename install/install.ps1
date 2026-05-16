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
    'local', 'server', or 'custom'. Skips the deployment-type prompt.
    'custom' exposes advanced fields (listen host, public URL, allowed
    origins, wizard preset) so you can configure unusual setups —
    running inside a container, behind a reverse proxy, etc. 'container'
    is accepted as a deprecated alias for 'custom' with container
    defaults.

.PARAMETER AppHost
    Public IP/domain for server deployments.

.PARAMETER ListenHost
    (custom deployment) Override HOST in .env.

.PARAMETER PublicUrl
    (custom deployment) Override APP_URL in .env.

.PARAMETER AllowedOrigins
    (custom deployment) Override CORS_ALLOWED_ORIGINS in .env.

.PARAMETER WizardPreset
    (custom deployment) Override SETUP_WIZARD_ENV in .env.

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

.PARAMETER Version
    Explicit openpa version to install (e.g. '0.1.9' for production,
    '0.1.9.dev3' for test). Validated against the channel shape
    (production = X.Y.Z, test = X.Y.Z.devN). When -ElectronVersion is
    also given, must additionally match that line (production = exact,
    test = same X.Y.Z).

.PARAMETER ElectronVersion
    Build version of the OpenPA desktop app driving this install (e.g.
    '0.1.9'). Forwarded by the Electron main process so the openpa
    package pins to the same line; CLI users typically don't set this.

.NOTES
    Service selection (database backend, vector store backend, …) is no
    longer made at install time. The Setup Wizard now lets you pick each
    backing service's deployment mode (Docker / Native / External)
    per-service, independent of how OpenPA itself is installed.
#>

[CmdletBinding()]
param(
    [ValidateSet('production','test','dev')] [string] $Channel = 'production',
    [ValidateSet('local','server','custom','container','')] [string] $Deployment = '',
    [string] $AppHost = '',
    [string] $ListenHost = '',
    [string] $PublicUrl = '',
    [string] $AllowedOrigins = '',
    [string] $WizardPreset = '',
    [ValidateSet('','docker','native')] [string] $Mode = '',
    [switch] $NoLaunch,
    [switch] $Unattended,
    [switch] $Reinstall,
    [switch] $AutoInstallPython,
    [switch] $NoAutoInstallPython,
    [switch] $ModifyPath,
    [switch] $NoModifyPath,
    [string] $Version = '',
    [string] $ElectronVersion = ''
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

# ── -Version validation ───────────────────────────────────────────────────
#
# Two layers of check, mirroring install.sh:
#   1. Channel-shape — production = X.Y.Z, test = X.Y.Z.devN. Dev ignores
#      -Version (editable install).
#   2. Electron-line — only when -ElectronVersion is also provided.
#      Production: exact match. Test: same X.Y.Z, .devN suffix.
#
# The error messages are the strings the OpenPA desktop app surfaces in
# its install log; they name the Electron build that's rejecting the spec.
if ($Version -and $Channel -eq 'dev') {
    Write-Host "!!! -Version is ignored on dev channel (editable install)." -ForegroundColor Yellow
    $Version = ''
}
if ($Version) {
    switch ($Channel) {
        'production' {
            if ($Version -notmatch '^\d+\.\d+\.\d+$') {
                Write-Host "ERR Invalid version: '$Version' does not look like a production release (expected X.Y.Z)." -ForegroundColor Red
                exit 2
            }
        }
        'test' {
            if ($Version -notmatch '^\d+\.\d+\.\d+\.dev\d+$') {
                Write-Host "ERR Invalid version: '$Version' does not look like a test prerelease (expected X.Y.Z.devN)." -ForegroundColor Red
                exit 2
            }
        }
    }
}
if ($Version -and $ElectronVersion) {
    switch ($Channel) {
        'production' {
            if ($Version -ne $ElectronVersion) {
                Write-Host "ERR Invalid version: '$Version' is not a valid production release for this Electron build (v$ElectronVersion). Production requires an exact version match — use the in-app update flow to install a different version." -ForegroundColor Red
                exit 2
            }
        }
        'test' {
            # Anchor on the literal dot — ``0.1.9`` must not accidentally
            # match a ``0.1.91.devN`` wheel.
            $prefix = "$ElectronVersion.dev"
            if (-not ($Version.StartsWith($prefix)) -or $Version -eq $prefix) {
                Write-Host "ERR Invalid version: '$Version' is not a valid test release for this Electron build (v$ElectronVersion). Test channel accepts only $ElectronVersion.devN prereleases." -ForegroundColor Red
                exit 2
            }
        }
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
        # spaced out in any UTF-8 reader). The ForEach-Object unwraps
        # stderr lines that PS 5.1 wraps as NativeCommandError
        # ErrorRecords — otherwise each benign stderr line (docker/pip/uv
        # progress) gets the full "At C:\...:line char:N" error decoration
        # in the log even on successful runs.
        & $Action 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            } else {
                $_
            }
        } | Out-File -FilePath $script:LogFile -Encoding utf8 -Append
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

# ── catalog ───────────────────────────────────────────────────────────────
#
# Source the generated _catalog.ps1 so the prompts and rendered .env
# files share their labels / descriptions / rules with install.sh and
# the Setup Wizard. Dev installs read from the checkout; remote
# installs download it next to the install scripts on GitHub.
if ($Channel -eq 'dev') {
    $CatalogPath = Join-Path $RepoRoot 'install\_catalog.ps1'
    . $CatalogPath
} else {
    $CatalogBase = if ($env:OPENPA_CATALOG_BASE) {
        $env:OPENPA_CATALOG_BASE
    } else {
        'https://raw.githubusercontent.com/openpa/openpa/main/install'
    }
    $CatalogTmp = Join-Path $OpenpaHome '_catalog.ps1'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$CatalogBase/_catalog.ps1" -OutFile $CatalogTmp
    } catch {
        Write-Err2 "Failed to fetch install catalog from $CatalogBase/_catalog.ps1"
        exit 1
    }
    . $CatalogTmp
}

# ── container alias ───────────────────────────────────────────────────────
#
# ``container`` was a separate deployment in earlier installs; it's now
# a narrow case of ``custom`` (listen on 0.0.0.0, URLs at localhost).
# Accept the old name for one release as an alias so existing scripts
# don't break, and warn so users migrate to --Deployment custom.
if ($Deployment -eq 'container') {
    Write-Warn2 "-Deployment container is deprecated; using -Deployment custom with container defaults."
    $Deployment = 'custom'
    if (-not $ListenHost)   { $ListenHost   = '0.0.0.0' }
    if (-not $PublicUrl)    { $PublicUrl    = 'http://localhost:1112' }
    if (-not $WizardPreset) { $WizardPreset = 'local' }
}

# ── banner ────────────────────────────────────────────────────────────────

# Single-quoted here-string: every char ($, backtick, apostrophe) is literal.
$Logo = @'
                            xrjjjjjjjjjrrxc
                      xrjfffjrx        jxjjfffjx
                   rjffj1                     jjfjr:
                rjfjj         *W8%%%%%%8&*       /ffjx
              rffj        M%%%BBBBBB@@@B%%%BB&      jjjj1
            jjjr       o%%%BB@@@@@@@@@@@BBB%%%B@8     rjjr
           jfx       C8%%B@@@@@@@@@@@BBBBBB%%%%%B@&     cjjr
         jfj      LJOB%B@@@@@@@@@BBBB%%%%%%88888%8BBQ     rjj
        jjx     xUYpBBB@@@@a|lllI;:,"^',tb&88888888%8C     ujj\
      :rjn     UYXq%%BB@v;;;:::,""^`'..      \M&&&&8%&YU    njjx
      jjj    cYXzZB%BB/,,""""^^``''.            CWW&8BmYU    xjr1
   $8%WU    YXzzcW%B8:^^`````'''..               ;*MW&*czY    :jj\
   &8&8%%M UzccvQ%%%>'''';UQ0Q].          ...     .*MWBXczXO   xjn
   &8&&8%B8cvvvu*%%k ...fX:```xu'      .-QZZ0U^    f#M%QvccX    rjn
   r8&W&&8%Wnuunp%8k                   11....iX;   ?*#8muvvvc   rjr
   fL&WWW&88%vxxn&%8>                              ]o*%Jnnunu    rj
  xfx&WMMWW&8%mrju888Y                             Ja*Wrxxxxn    ujn
  jjOM&MMMMMW&8*fff0WWWWv                        ;dka%zrrrrrrn   0jr
  rj  MW###MMWW&8ntt/j*M######Xl             ILbbbbo#ffjjjjjjr    jf
  fj   MM***##MMM8\\\(|00Um#*aaaaooooaahhhkkkbbbk#Q|//tttttfff    jj
  jj    M#*o****##ZoMbqmOLJYzuxjftjYwkaooabZYzYLmoQ|\\///////t    jj
  rfz    0#ooooooob@%%BB*bdpwmO0LCUYXzcccczzXUJLW%BQ||\\\\\||t   Ojj
  nfr    1)ooaaahhq@%%%%BBBBBBB@@BBBBBBBBBB%&WM*oaWhBM|((((((Y   nfn
   jjJ   )11roaahhx@%%%BBBBBB%%888&&&WWWMM##*oohdpZMW8Bn))))(    jj
   rjr    (1111111|@%%%%BBB%888&&&WWWMM##X0Uhahbw0hoMW8%f111|   rjr
   jjj     11111111B%%BBBB%%88&&&WWMM##*L0wZbhkwZQka#M&8B(1(    rfn
    zfr    |{{1{{{{uB%%%%%88&&&WWMMM##****M*akpmmQbho#W&%J1    jjn
     rfx    ){{{{{{1W%%%%88&&&WWWM###**oooahhdwwqLQha*M&88    nfr
      jjr    |{{{{{{Z%8%%8&&WWWMM###**oooaahkpqph){hho#W&%d  rfj
       rfr    :1{{{{}#%88&&WWWMM##**oooaaahkdqphY{[qha*M&%* jjj:
        rjr:    1{{{{f%88&WWMMM##***oaaaaahbddh#{{}XahoM&%orfr
         rfjx     {{{}f%8&WMM##***ooaaahhhkddao1{{{c*ha*W%rfr
           ffj\     ){}Z8&WM##***oaaaahhhkbbaM{{1(  aah*WMtx
            fffr       {0&WM#**ooahhhhhkkbkoW)1     a#hoW&
              fjfj\      o&W#*ooaahhhhhkkk*#b       fu*#&a
                :fffr     hWW#oaahhhhkkkoMa      nffffZh$
                   \jffjj   kMMoahhhhho#k    ujffjj
                       rjffffjjJoM#M*d rxjfjfjj1
                            Onjjjffffjjjr\
'@
Write-Host $Logo
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
    Write-Host "How will you run OpenPA?"
    $idx = 0
    foreach ($id in $script:DeploymentIds) {
        $idx++
        $entry = $script:Deployments[$id]
        Write-Host ("  {0}) {1,-20} - {2}" -f $idx, $entry.Label, $entry.Description)
    }
    while (-not $Deployment) {
        $choice = Read-Host "Choice [1]"
        if (-not $choice) { $choice = '1' }
        # Resolve numeric or name choice against $script:DeploymentIds.
        $i = 0
        foreach ($id in $script:DeploymentIds) {
            $i++
            if ($choice -eq "$i" -or $choice -eq $id) {
                $Deployment = $id
                break
            }
        }
        if (-not $Deployment) {
            Write-Warn2 "Pick a number 1-$($script:DeploymentIds.Count) or a deployment id."
        }
    }
}
if (-not ($script:DeploymentIds -contains $Deployment)) {
    Write-Err2 "Unknown deployment: $Deployment (must be one of: $($script:DeploymentIds -join ', '))"
    exit 2
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

# ── custom-deployment fields ─────────────────────────────────────────────

# When the user picks ``custom`` walk the advanced-field array from the
# catalog and prompt for each one with its plain-English question +
# hint. Already-set values (from -ListenHost / -PublicUrl / etc., or
# from the deprecated container-deployment alias above) skip the
# prompt. In -Unattended the catalog default is used silently.
$CustomValues = @{}
if ($Deployment -eq 'custom') {
    Write-Host ""
    Write-Info "Custom deployment - answer a few questions about how OpenPA should be reached."
    $paramFor = @{
        'listen_host'     = 'ListenHost'
        'public_url'      = 'PublicUrl'
        'allowed_origins' = 'AllowedOrigins'
        'wizard_preset'   = 'WizardPreset'
    }
    foreach ($key in $script:CustomFieldIds) {
        $field = $script:CustomFields[$key]
        $paramName = $paramFor[$key]
        $current = (Get-Variable -Name $paramName -Scope Script -ErrorAction SilentlyContinue).Value
        if (-not $current) {
            $current = (Get-Variable -Name $paramName -ErrorAction SilentlyContinue).Value
        }
        if ($current) {
            $CustomValues[$key] = $current
            Write-Ok "$key`: $current"
            continue
        }
        if ($Unattended) {
            $CustomValues[$key] = $field.Default
            Write-Ok "$key`: $($field.Default) (default)"
            continue
        }
        Write-Host ""
        Write-Host $field.Prompt -ForegroundColor White
        Write-Host $field.Hint   -ForegroundColor DarkGray
        if ($field.Choices.Count -gt 0) {
            Write-Host ("Choices: " + ($field.Choices -join ', ')) -ForegroundColor DarkGray
        }
        while ($true) {
            $answer = Read-Host "  [$($field.Default)]"
            if (-not $answer) { $answer = $field.Default }
            if ($field.Choices.Count -gt 0 -and -not ($field.Choices -contains $answer)) {
                Write-Warn2 "Pick one of: $($field.Choices -join ', ')"
                continue
            }
            $CustomValues[$key] = $answer
            break
        }
    }
    # Sensible fallback for allowed_origins: derive from the public URL
    # plus the localhost variants the SPA listener serves on. Saves
    # operators from constructing a CORS list by hand for the typical
    # browse-via-localhost flow.
    if (-not $CustomValues['allowed_origins']) {
        $CustomValues['allowed_origins'] = "$($CustomValues['public_url']),http://localhost:1515,http://127.0.0.1:1515"
    }
}

# ── mode (docker vs native) ──────────────────────────────────────────────

# Default: docker if available, native otherwise. Labels and descriptions
# come from the catalog ($script:ModeIds / $script:Modes) so the install
# scripts and the Setup Wizard show identical text.
if (-not $Mode) {
    if ($HasDocker) {
        if ($Unattended) {
            $Mode = 'docker'
        } else {
            Write-Host ""
            Write-Host "How do you want to run OpenPA?" -ForegroundColor White
            $idx = 0
            foreach ($id in $script:ModeIds) {
                $idx++
                $entry = $script:Modes[$id]
                Write-Host ("  {0}) {1,-8} - {2}" -f $idx, $entry.Label, $entry.Description)
                if ($entry.Hint) {
                    Write-Host ("               $($entry.Hint)") -ForegroundColor DarkGray
                }
            }
            while (-not $Mode) {
                $choice = Read-Host "Choice [1]"
                if (-not $choice) { $choice = '1' }
                $i = 0
                foreach ($id in $script:ModeIds) {
                    $i++
                    if ($choice -eq "$i" -or $choice -eq $id) {
                        $Mode = $id
                        break
                    }
                }
                if (-not $Mode) {
                    Write-Warn2 "Pick a number 1-$($script:ModeIds.Count) or a mode id."
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
    # Pick the OPENPA_VERSION tag to bake into the rendered docker .env.
    # Channel-driven:
    #   production → -Version → -ElectronVersion → latest stable from PyPI JSON
    #   test       → -Version → highest .devN matching -ElectronVersion line
    #                from Test PyPI's simple index
    #   dev        → literal 'dev'; the docker-compose.override.yml
    #                rebuilds locally and re-tags so the value is only
    #                a cosmetic image label
    # Hard-fails on network errors for prod / test — the previous
    # 'main' fallback silently mis-tagged the image and masked the
    # failure behind ``docker compose up --build``.

    # Explicit -Version short-circuits all lookups. Already validated
    # against the channel shape (and Electron line, when applicable) by
    # the top-level guards.
    if ($Version -and $Channel -ne 'dev') {
        Write-Ok "Using openpa==$Version (-Version)"
        return $Version
    }

    switch ($Channel) {
        'production' {
            # When the Electron app drives the install, pin to its
            # build version. Electron + openpa wheel are released
            # together under the same tag; drifting the backend off
            # the Electron line silently desyncs tray-menu / taskbar
            # features (which live in the Electron main process).
            if ($ElectronVersion) {
                Write-Ok "Pinning openpa==$ElectronVersion (Electron build version)"
                return $ElectronVersion
            }
            Write-Info "Resolving latest openpa version from PyPI"
            try {
                $body = (Invoke-WebRequest -UseBasicParsing -Uri 'https://pypi.org/pypi/openpa/json').Content
                $v = ($body | ConvertFrom-Json).info.version
            } catch {
                Write-Err2 "Failed to resolve openpa version from https://pypi.org/pypi/openpa/json : $_"
                exit 1
            }
            if (-not $v) {
                Write-Err2 "PyPI JSON returned no version for openpa"
                exit 1
            }
            Write-Ok "Resolved openpa==$v from PyPI"
            return $v.Trim()
        }
        'test' {
            Write-Info "Resolving latest openpa pre-release from Test PyPI"
            try {
                $indexBody = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/openpa/').Content
            } catch {
                Write-Err2 "Failed to fetch https://test.pypi.org/simple/openpa/ : $_"
                exit 1
            }
            # Same regex shape as the native test installer below — match
            # the wheel URL, then strip out the version segment from the
            # filename (``openpa-<version>-py3-none-any.whl``).
            #
            # When -ElectronVersion is set, only consider wheels in that
            # dev line; cross-line picks would silently desync UI from
            # backend on the user's Electron build.
            $wheelNames = [regex]::Matches($indexBody, 'openpa-([^"/]+)-py3-none-any\.whl') |
                ForEach-Object { $_.Groups[1].Value } |
                Select-Object -Unique |
                Sort-Object
            if ($ElectronVersion) {
                $prefix = "$ElectronVersion.dev"
                $wheelNames = $wheelNames | Where-Object { $_ -like "$prefix*" -and $_ -match '^\d+\.\d+\.\d+\.dev\d+$' }
            }
            $v = $wheelNames | Select-Object -Last 1
            if (-not $v) {
                if ($ElectronVersion) {
                    Write-Err2 "No openpa wheel matching $ElectronVersion.devN found at https://test.pypi.org/simple/openpa/ — has a test prerelease been published for this Electron build?"
                } else {
                    Write-Err2 "No openpa wheel found at https://test.pypi.org/simple/openpa/"
                }
                exit 1
            }
            Write-Ok "Resolved openpa==$v from Test PyPI"
            return $v.Trim()
        }
        'dev' {
            # Dev mode rebuilds via docker-compose.override.yml; the tag is
            # only the local image label.
            return 'dev'
        }
    }
    Write-Err2 "Unknown channel: $Channel"
    exit 1
}

if ($Mode -eq 'docker') {
    Write-Step "Docker install"

    $DockerDir = Join-Path $OpenpaHome 'docker'
    if (-not (Test-Path $DockerDir)) { New-Item -ItemType Directory -Path $DockerDir | Out-Null }

    $ComposeFile = Join-Path $DockerDir 'docker-compose.yml'
    $EnvDocker   = Join-Path $DockerDir '.env'

    # Sidecar services (postgres / qdrant / chroma) are no longer
    # provisioned here — the Setup Wizard activates each one on demand
    # via its own per-service deployment-mode picker.
    #
    # Bundle regeneration is unconditional. Channel-dependent fields
    # (OPENPA_VERSION, OPENPA_UPGRADE_CHANNEL, OPENPA_PIP_INDEX_URL) all
    # drift if the .env is reused across runs, and a previous-channel
    # docker-compose.override.yml silently keeps a stale ``build:``
    # context alive on dev→test/prod switches. Regenerating from
    # templates on every run is the only way to keep state honest;
    # VNC_PASSWORD is the only true secret here and the installer
    # surfaces it at the end of the run, so re-rolling it is cheap.
    if (Test-Path $ComposeFile) {
        Write-Info "Regenerating $DockerDir config (templates re-render every run)"
    }

    $VncPwd      = New-Secret
    $OpenpaVer   = Resolve-OpenPAVersion

    switch ($Deployment) {
        'local' {
            $DockerAppUrl    = 'http://localhost:1112'
            $DockerCors      = 'http://localhost:1515,http://127.0.0.1:1515'
            $DockerWizardEnv = 'local'
        }
        'server' {
            $DockerAppUrl    = "http://${AppHost}:1112"
            $DockerCors      = "http://${AppHost}:1515,http://localhost:1515"
            $DockerWizardEnv = 'server'
        }
        'custom' {
            $DockerAppUrl    = $CustomValues['public_url']
            $DockerCors      = $CustomValues['allowed_origins']
            $DockerWizardEnv = $CustomValues['wizard_preset']
        }
    }

    # Dev channel: open CORS so ``npm run dev`` (Vite at localhost:5173)
    # and other ad-hoc dev origins can hit the API without preflight
    # failures. Production and test installs keep the locked-down list.
    if ($Channel -eq 'dev') {
        $DockerCors = '*'
    }

    Write-Info "Fetching docker-compose template"
    Get-TemplateContent 'docker-compose.yml.tmpl' | Set-Content -Path $ComposeFile -Encoding utf8

    Write-Info "Writing $EnvDocker (secrets, do not commit)"
    $rendered = (Get-TemplateContent 'docker.env.tmpl') `
        -replace '__OPENPA_VERSION__', $OpenpaVer `
        -replace '__APP_URL__',        $DockerAppUrl `
        -replace '__CORS_ALLOWED_ORIGINS__', $DockerCors `
        -replace '__SETUP_WIZARD_ENV__', $DockerWizardEnv `
        -replace '__INSTALL_MODE__',   $Mode `
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

    # Stamp the channel into docker.env so the running container's
    # upgrader and feature installer see it. Without this, dev images
    # fall back to ``production`` semantics at runtime — which makes
    # ``pip_spec()`` pin to ``openpa==<version>`` and look up PyPI
    # for a release that may not be published yet during release prep.
    Add-Content -Path $EnvDocker -Value "OPENPA_UPGRADE_CHANNEL=$Channel" -Encoding utf8

    # Dev channel: emit a docker-compose.override.yml that points the
    # build context at the local checkout, switches the pip install
    # to ``-e /src``, and bind-mounts the checkout for runtime
    # imports. Compose auto-merges this when running from $DockerDir.
    # Backslashes are converted to forward slashes — Docker Desktop
    # accepts either, and forward slashes keep the YAML readable.
    #
    # Non-dev channels must remove any previously-written override:
    # a stale dev override silently re-adds a local ``build:`` context
    # that wins over the pulled image.
    $OverrideFile = Join-Path $DockerDir 'docker-compose.override.yml'
    if ($Channel -eq 'dev') {
        $RepoRootCompose = $RepoRoot -replace '\\', '/'
        Write-Info "Writing $OverrideFile (bind-mounts $RepoRoot at /src)"
        $overrideRendered = (Get-TemplateContent 'docker-compose.override.yml.tmpl') `
            -replace '__REPO_ROOT__', $RepoRootCompose
        $overrideRendered | Set-Content -Path $OverrideFile -Encoding utf8
    } elseif (Test-Path $OverrideFile) {
        Write-Info "Removing stale docker-compose.override.yml (dev-only)"
        Remove-Item -Path $OverrideFile -Force
    }

    Write-Ok "Wrote $ComposeFile + .env"

    # Per-channel pull / build strategy:
    #   production / test → pull the pre-built image from Docker Hub
    #                       and refuse to fall back to a local build.
    #                       The main compose template has no ``build:``
    #                       section, so a missing tag fails hard at
    #                       ``compose pull`` with a clear error rather
    #                       than silently rebuilding off the user's
    #                       checkout.
    #   dev               → the docker-compose.override.yml re-adds a
    #                       ``build:`` section pointing at the local
    #                       checkout. ``compose pull`` is best-effort
    #                       (the dev image isn't published), and
    #                       ``compose up --build`` forces a rebuild
    #                       so host-side edits land in the image.
    Push-Location $DockerDir
    try {
        if ($Channel -eq 'dev') {
            Write-Info "Pulling sidecar images (openpa image is built locally for dev)"
            Invoke-NativeLogged { & docker compose pull --ignore-pull-failures }
            if ($LASTEXITCODE -ne 0) {
                Write-Warn2 "Some images couldn't be pulled; will build locally."
            }
            Write-Info "Building openpa image and starting bundle"
            Invoke-NativeLogged { & docker compose up -d --build }
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose up failed (see $LogFile)"
            }
        } else {
            Write-Info "Pulling openpa/openpa-desktop:$OpenpaVer and sidecar images from Docker Hub"
            Invoke-NativeLogged { & docker compose pull }
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose pull failed — openpa/openpa-desktop:$OpenpaVer may not be published yet (see $LogFile)"
            }
            Write-Info "Starting bundle"
            Invoke-NativeLogged { & docker compose up -d }
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose up failed (see $LogFile)"
            }
        }
    } finally {
        Pop-Location
    }

    if ($Deployment -eq 'local') {
        $HealthHost = 'localhost'
    } elseif ($Deployment -eq 'custom') {
        # http://foo.bar:1112 → foo.bar
        $HealthHost = $CustomValues['public_url']
        if ($HealthHost -match '^https?://([^:/]+)') { $HealthHost = $Matches[1] }
        if (-not $HealthHost) { $HealthHost = 'localhost' }
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

    # Top-level marker so the Electron app's reconcileInstallStateWithDisk()
    # sees a Docker install as "installed". The real compose config lives in
    # $DockerDir/.env; this file is intentionally minimal. Rewritten on
    # every run to match the unconditional bundle regeneration above.
    Write-Info "Writing $EnvFile (install marker)"
    @"
# OpenPA Docker install marker. Compose config: $EnvDocker
INSTALL_MODE=docker
"@ | Set-Content -Path $EnvFile -Encoding utf8

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
    #
    # The bare ``openpa`` install is intentionally thin — it ships only
    # the core deps the server + Setup Wizard + SQLite need. Optional
    # feature groups (vector embedding, vector stores, browser, channels,
    # LLM SDKs, postgres) are installed on demand by ``app/features/``
    # when the user enables them in the wizard. The Docker desktop image
    # pre-bakes ``openpa[all]`` instead (see ``Dockerfile.desktop``).
    $InstallSpec = $null
    $InstallSourceLabel = ''
    switch ($Channel) {
        'production' {
            # Pin order: -Version → -ElectronVersion → bare ``openpa``
            # (CLI users running install.ps1 directly).
            if ($Version) {
                $InstallSpec = "openpa==$Version"
            } elseif ($ElectronVersion) {
                $InstallSpec = "openpa==$ElectronVersion"
            } else {
                $InstallSpec = 'openpa'
            }
            $InstallSourceLabel = 'PyPI'
        }
        'test' {
            # PS 5.1 has no built-in HTML parser; regex the simple-index page.
            $indexBody = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/openpa/').Content
            if ($Version) {
                Write-Info "Locating openpa==$Version wheel"
                # Anchor on the exact version segment so 0.1.9 doesn't
                # match a 0.1.91 wheel.
                $pattern = "https://[^`"]*openpa-" + [regex]::Escape($Version) + "-py3-none-any\.whl"
                $wheelUrls = [regex]::Matches($indexBody, $pattern) |
                    ForEach-Object { $_.Value } |
                    Select-Object -Unique
            } elseif ($ElectronVersion) {
                Write-Info "Locating latest openpa test wheel for line $ElectronVersion.dev*"
                $pattern = "https://[^`"]*openpa-" + [regex]::Escape($ElectronVersion) + "\.dev\d+-py3-none-any\.whl"
                $wheelUrls = [regex]::Matches($indexBody, $pattern) |
                    ForEach-Object { $_.Value } |
                    Select-Object -Unique |
                    Sort-Object -Property @{ Expression = { Split-Path $_ -Leaf } }
            } else {
                Write-Info "Locating latest openpa test wheel"
                $wheelUrls = [regex]::Matches($indexBody, 'https://[^"]*openpa-[^"]*-py3-none-any\.whl') |
                    ForEach-Object { $_.Value } |
                    Select-Object -Unique |
                    Sort-Object -Property @{ Expression = { Split-Path $_ -Leaf } }
            }
            $InstallSpec = $wheelUrls | Select-Object -Last 1
            if (-not $InstallSpec) {
                if ($Version) {
                    Write-Err2 "No openpa-$Version-py3-none-any.whl found at https://test.pypi.org/simple/openpa/ — has this version been published?"
                } elseif ($ElectronVersion) {
                    Write-Err2 "No openpa-$ElectronVersion.dev*-py3-none-any.whl found at https://test.pypi.org/simple/openpa/ — has a test prerelease been published for this Electron build?"
                } else {
                    Write-Err2 "No openpa wheel found at https://test.pypi.org/simple/openpa/"
                }
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
        'server' {
            # __APP_HOST__ is the only placeholder; the user-provided host
            # gets substituted as-is (validated above).
            ((Get-TemplateContent 'server.env.tmpl') -replace '__APP_HOST__', $AppHost) |
                Set-Content -Path $EnvFile -Encoding utf8
        }
        'custom' {
            # All four advanced fields come from $CustomValues (filled
            # either from -ListenHost/etc. params, the catalog defaults
            # in -Unattended, or interactive prompts). They're already
            # validated for the wizard_preset choice list and otherwise
            # copy-pasted by the operator.
            $rendered = (Get-TemplateContent 'custom.env.tmpl') `
                -replace '__LISTEN_HOST__',         $CustomValues['listen_host'] `
                -replace '__PUBLIC_URL__',          $CustomValues['public_url'] `
                -replace '__CORS_ALLOWED_ORIGINS__', $CustomValues['allowed_origins'] `
                -replace '__SETUP_WIZARD_ENV__',    $CustomValues['wizard_preset']
            $rendered | Set-Content -Path $EnvFile -Encoding utf8
        }
    }
    # Stamp the install mode so the backend's mode-rule filter knows which
    # service modes to expose in the wizard. Native installs land here too;
    # docker installs stamp INSTALL_MODE into docker.env above.
    if (-not (Select-String -Path $EnvFile -Pattern '^INSTALL_MODE=' -Quiet)) {
        Add-Content -Path $EnvFile -Value "INSTALL_MODE=$Mode" -Encoding utf8
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
        # Stamp the channel so the feature installer's ``pip_spec()`` knows
        # to skip the ``==<version>`` pin (the editable install in /src or
        # the developer's checkout already satisfies the requirement, and
        # pinning to a release that hasn't been published to PyPI yet
        # fails at install time). ``openpa upgrade`` itself is still a
        # footgun in dev — the dev path is ``git pull`` — but the
        # upgrader's no-op behavior on dev is preferable to it silently
        # treating dev as production.
        if (-not (Select-String -Path $EnvFile -Pattern '^OPENPA_UPGRADE_CHANNEL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value "`nOPENPA_UPGRADE_CHANNEL=dev" -Encoding utf8
        }
        # Replace the static template's locked-down CORS list with ``*``
        # so ``npm run dev`` (Vite at localhost:5173) and other ad-hoc dev
        # origins work without preflight failures. Idempotent: skip if
        # already wildcarded so re-runs don't churn the file. ``-Encoding
        # utf8`` is critical on both read and write — Windows PowerShell
        # 5.1's default ``Get-Content`` decodes BOM-less UTF-8 as cp1252,
        # which would corrupt non-ASCII characters (em-dashes, etc.) on
        # roundtrip.
        if (-not (Select-String -Path $EnvFile -Pattern '^CORS_ALLOWED_ORIGINS=\*$' -Quiet)) {
            $filtered = Get-Content -Path $EnvFile -Encoding utf8 | Where-Object { $_ -notmatch '^CORS_ALLOWED_ORIGINS=' }
            Set-Content -Path $EnvFile -Value $filtered -Encoding utf8
            Add-Content -Path $EnvFile -Value 'CORS_ALLOWED_ORIGINS=*' -Encoding utf8
        }
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

# 'custom' installs honour the public URL the user provided; 'server'
# uses the host from -AppHost; 'local' is loopback. Custom URLs may
# include a path/scheme; we swap the port to 1515 (the wizard SPA port)
# while preserving the hostname.
if ($Deployment -eq 'server') {
    $WizardUrl = "http://${AppHost}:1515/#/setup"
} elseif ($Deployment -eq 'custom') {
    $CustomHostPart = $CustomValues['public_url']
    if ($CustomHostPart -match '^https?://([^:/]+)') { $CustomHostPart = $Matches[1] } else { $CustomHostPart = 'localhost' }
    $WizardUrl = "http://${CustomHostPart}:1515/#/setup"
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
