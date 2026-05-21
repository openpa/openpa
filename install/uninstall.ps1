<#
.SYNOPSIS
    OpenPA uninstaller for Windows (PowerShell).

.DESCRIPTION
    Removes OpenPA from this machine. Operates on two directories:

    System Dir  ($env:OPENPA_SYSTEM_DIR / default %USERPROFILE%\.openpa)
        Runtime + user content: .env, bootstrap.toml, storage\, tokens\,
        venv\, bin\, per-profile dirs, server.log, upgrade artifacts.

    Install Dir ($env:OPENPA_INSTALL_DIR / default %LOCALAPPDATA%\OpenPA)
        Install-time scratch: install.log, install.pid, docker\
        (compose bundle), pip-cache\, uv-cache\, python\.

    Detection: $InstallDir\docker\docker-compose.yml -> Docker mode;
               $SystemDir\venv -> Native mode.

    Behavior matrix:
      -Keep + native:  remove venv\, bin\, Install Dir contents.
                       Preserve $SystemDir\.env, bootstrap.toml, storage\,
                       tokens\, profile dirs.
      -Keep + docker:  docker compose down (volumes preserved); remove
                       Install Dir. Preserve System Dir.
      -Purge + native: rm -rf both $SystemDir and $InstallDir.
      -Purge + docker: docker compose down -v; rm -rf both dirs.

    The User Working Directory (server_config.user_working_dir, picked
    in the Setup Wizard) is NEVER touched directly. When it resolves
    inside the System Dir (typical default), it goes with -Purge along
    with the rest of the System Dir.

.PARAMETER Keep
    Remove install scratch + Native binaries; preserve runtime state.

.PARAMETER Purge
    Wipe both System Dir and Install Dir. For Docker installs, also
    runs ``docker compose down -v`` to destroy named volumes.

.PARAMETER SystemDir
    Override $env:OPENPA_SYSTEM_DIR for a single run.

.PARAMETER InstallDir
    Override $env:OPENPA_INSTALL_DIR for a single run.

.EXAMPLE
    .\uninstall.ps1                # interactive prompt
    .\uninstall.ps1 -Keep
    .\uninstall.ps1 -Purge
#>

[CmdletBinding()]
param(
    [switch]$Keep,
    [switch]$Purge,
    [string]$SystemDir,
    [string]$InstallDir
)

$ErrorActionPreference = 'Continue'

# Resolve System Dir and Install Dir (must match install.ps1 defaults).
if (-not $SystemDir) {
    if ($env:OPENPA_SYSTEM_DIR) {
        $SystemDir = $env:OPENPA_SYSTEM_DIR
    } else {
        $SystemDir = Join-Path $env:USERPROFILE '.openpa'
    }
}
if (-not $InstallDir) {
    if ($env:OPENPA_INSTALL_DIR) {
        $InstallDir = $env:OPENPA_INSTALL_DIR
    } else {
        $InstallDir = Join-Path $env:LOCALAPPDATA 'OpenPA'
    }
}

$systemExists  = Test-Path -LiteralPath $SystemDir
$installExists = Test-Path -LiteralPath $InstallDir
if (-not $systemExists -and -not $installExists) {
    Write-Host "Nothing to uninstall - neither $SystemDir nor $InstallDir exists."
    exit 0
}

if ($Keep -and $Purge) {
    Write-Host "Pass at most one of -Keep / -Purge." -ForegroundColor Red
    exit 2
}

# Detect kind from on-disk markers.
$ComposeFile = Join-Path $InstallDir 'docker\docker-compose.yml'
$VenvDir     = Join-Path $SystemDir 'venv'
if (Test-Path -LiteralPath $ComposeFile) {
    $Kind = 'docker'
} elseif (Test-Path -LiteralPath $VenvDir) {
    $Kind = 'native'
} elseif ($installExists) {
    # Install Dir present but no markers - partial install. Default to
    # native so the residual scratch gets cleaned up.
    $Kind = 'native'
} else {
    Write-Host "Unrecognized install layout." -ForegroundColor Red
    Write-Host "Expected $SystemDir\venv (native) or $InstallDir\docker\docker-compose.yml (docker)." -ForegroundColor Red
    exit 1
}

# Resolve mode (flag or interactive).
$Mode = ''
if ($Purge) { $Mode = 'purge' }
elseif ($Keep) { $Mode = 'keep' }

if (-not $Mode) {
    Write-Host ''
    Write-Host "Uninstall OpenPA ($Kind):"
    Write-Host "  System Dir:  $SystemDir"
    Write-Host "  Install Dir: $InstallDir"
    Write-Host ''
    Write-Host '  [k] Keep data    - remove the binaries + install scratch; preserve System Dir contents'
    Write-Host '                     (.env, bootstrap.toml, storage\, tokens\, profile dirs)'
    $purgeExtra = if ($Kind -eq 'docker') { ' (incl. Docker volumes)' } else { '' }
    Write-Host "  [p] Purge all    - delete everything OpenPA installed$purgeExtra"
    Write-Host '  [c] Cancel'
    Write-Host ''
    $ans = Read-Host -Prompt 'Choose'
    switch -Regex ($ans) {
        '^[kK]' { $Mode = 'keep' }
        '^[pP]' { $Mode = 'purge' }
        default { Write-Host 'Cancelled.'; exit 0 }
    }
}

# Probe the User Working Directory (best-effort; only when sqlite3.exe is
# on PATH). Used only for the post-purge "preserved at..." message.
$UserDir = $null
if ($Mode -eq 'purge') {
    $sqlite = Get-Command sqlite3.exe -ErrorAction SilentlyContinue
    if ($sqlite) {
        $dbPath = Join-Path $SystemDir 'storage\openpa.db'
        if (Test-Path -LiteralPath $dbPath) {
            try {
                $UserDir = & $sqlite.Source $dbPath "select value from server_config where key='user_working_dir'" 2>$null
                $UserDir = ($UserDir | Select-Object -First 1)
            } catch {
                $UserDir = $null
            }
        }
    }
}

# Stop any backend tracked via install.pid (install session) or server.pid
# (long-running server). PowerShell's $pid is automatic - use a non-clashing name.
function Stop-OpenpaProcess([string]$pidFilePath) {
    if (-not (Test-Path -LiteralPath $pidFilePath)) { return }
    try {
        $childPid = [int](Get-Content -LiteralPath $pidFilePath -ErrorAction Stop | Select-Object -First 1)
        if ($childPid -gt 0) {
            try {
                Stop-Process -Id $childPid -Force -ErrorAction Stop
                Wait-Process -Id $childPid -Timeout 5 -ErrorAction SilentlyContinue
            } catch {
                # Process already gone, no permission, or transient failure - proceed.
            }
        }
    } catch {
        # PID file unreadable or non-numeric - ignore.
    }
}
Stop-OpenpaProcess (Join-Path $InstallDir 'install.pid')
Stop-OpenpaProcess (Join-Path $SystemDir 'server.pid')

# Docker container/volume cleanup.
if ($Kind -eq 'docker') {
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    $dockerUp = $false
    if ($docker) {
        try {
            & $docker.Source info 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $dockerUp = $true }
        } catch { $dockerUp = $false }
    }
    if ($dockerUp -and (Test-Path -LiteralPath (Join-Path $InstallDir 'docker'))) {
        $DockerDir = Join-Path $InstallDir 'docker'
        Push-Location -LiteralPath $DockerDir
        try {
            if ($Mode -eq 'purge') {
                Write-Host 'Stopping containers and removing volumes...'
                & $docker.Source compose -p openpa down -v --remove-orphans
            } else {
                Write-Host 'Stopping containers (volumes preserved)...'
                & $docker.Source compose -p openpa down --remove-orphans
            }
        } finally {
            Pop-Location
        }
    } elseif (-not $dockerUp) {
        Write-Host "Warning: Docker daemon unreachable; containers/volumes left running." -ForegroundColor Yellow
        Write-Host "         Run 'docker compose -p openpa down -v' manually when Docker is available." -ForegroundColor Yellow
    }
}

# Remove the bin entry from User-scope PATH. We match $BinDir exactly to
# avoid clobbering unrelated PATH entries.
$BinDir = Join-Path $SystemDir 'bin'
try {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath) {
        $entries = $userPath -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ine $BinDir.TrimEnd('\')) }
        $newPath = ($entries -join ';').TrimEnd(';')
        if ($newPath -ne $userPath) {
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Write-Host "Removed $BinDir from User PATH."
        }
    }
} catch {
    Write-Host "Warning: failed to clean User PATH ($_)." -ForegroundColor Yellow
}

function Test-RootLikePath([string]$path) {
    $resolved = $path.TrimEnd('\','/')
    return (-not $resolved -or $resolved -match '^[A-Za-z]:[\\/]?$')
}

if ($Mode -eq 'purge') {
    if (Test-RootLikePath $SystemDir) {
        Write-Host "Refusing to purge a root-like System Dir: '$SystemDir'" -ForegroundColor Red
        exit 1
    }
    if (Test-RootLikePath $InstallDir) {
        Write-Host "Refusing to purge a root-like Install Dir: '$InstallDir'" -ForegroundColor Red
        exit 1
    }
    if (Test-Path -LiteralPath $SystemDir) {
        Remove-Item -LiteralPath $SystemDir -Recurse -Force -ErrorAction Continue
        Write-Host "Removed $SystemDir."
    }
    if (Test-Path -LiteralPath $InstallDir) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Continue
        Write-Host "Removed $InstallDir."
    }
    if ($UserDir -and ($UserDir -notlike "$SystemDir*")) {
        Write-Host "User Working Directory preserved at: $UserDir"
    }
} else {
    # Keep mode: remove venv + bin + install scratch; preserve runtime state.
    $toRemoveFromSystem = @(
        'venv',
        'bin',
        'server.pid'
    )
    foreach ($name in $toRemoveFromSystem) {
        $p = Join-Path $SystemDir $name
        if (Test-Path -LiteralPath $p) {
            Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # Wipe the Install Dir wholesale - it's all install scratch.
    if (Test-Path -LiteralPath $InstallDir) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Removed install scratch at $InstallDir."
    }
    Write-Host "Kept data in $SystemDir (.env, bootstrap.toml, storage\, tokens\, profile dirs)."
}

Write-Host 'Uninstall complete.'
