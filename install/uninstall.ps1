<#
.SYNOPSIS
    OpenPA uninstaller for Windows (PowerShell).

.DESCRIPTION
    Removes OpenPA's install and runtime artifacts from the System Directory.
    The User Working Directory (the user's documents / agent CWD) is NEVER
    touched, regardless of mode.

    Detects the install kind from disk:
      - Docker:  $SystemDir\docker\docker-compose.yml present
      - Native:  $SystemDir\venv\ present

    Behavior per (mode, kind):
      -Keep + native: remove venv\, bin\, pip-cache\, install.pid, install.log.
                      Preserves .env, bootstrap.toml, storage\, tokens\.
      -Keep + docker: docker compose down (volumes preserved); remove docker\,
                      install.pid, install.log. Preserves .env, storage\, tokens\.
      -Purge + native: remove the entire System Dir.
      -Purge + docker: docker compose down -v (volumes destroyed), remove System Dir.

.PARAMETER Keep
    Remove binaries only; preserve .env, bootstrap.toml, storage\, tokens\.

.PARAMETER Purge
    Wipe the System Directory entirely. For Docker installs, also
    runs ``docker compose down -v`` to destroy named volumes.

.PARAMETER SystemDir
    Override $env:OPENPA_SYSTEM_DIR for a single run.

.EXAMPLE
    .\uninstall.ps1                # interactive prompt
    .\uninstall.ps1 -Keep
    .\uninstall.ps1 -Purge
#>

[CmdletBinding()]
param(
    [switch]$Keep,
    [switch]$Purge,
    [string]$SystemDir
)

$ErrorActionPreference = 'Continue'

# Resolve System Dir (must match install.ps1 default).
if (-not $SystemDir) {
    if ($env:OPENPA_SYSTEM_DIR) {
        $SystemDir = $env:OPENPA_SYSTEM_DIR
    } else {
        $SystemDir = Join-Path $env:LOCALAPPDATA 'OpenPA'
    }
}

if (-not (Test-Path -LiteralPath $SystemDir)) {
    Write-Host "Nothing to uninstall — System Directory not found at $SystemDir"
    exit 0
}

if ($Keep -and $Purge) {
    Write-Host "Pass at most one of -Keep / -Purge." -ForegroundColor Red
    exit 2
}

# Detect kind. Docker wins if both markers exist.
$ComposeFile = Join-Path $SystemDir 'docker\docker-compose.yml'
$VenvDir = Join-Path $SystemDir 'venv'
if (Test-Path -LiteralPath $ComposeFile) {
    $Kind = 'docker'
} elseif (Test-Path -LiteralPath $VenvDir) {
    $Kind = 'native'
} else {
    Write-Host "Unrecognized install layout at $SystemDir." -ForegroundColor Red
    Write-Host "Expected $SystemDir\venv (native) or $SystemDir\docker\docker-compose.yml (docker)." -ForegroundColor Red
    exit 1
}

# Resolve mode.
$Mode = ''
if ($Purge) { $Mode = 'purge' }
elseif ($Keep) { $Mode = 'keep' }

if (-not $Mode) {
    Write-Host ''
    Write-Host "Uninstall OpenPA ($Kind) from:"
    Write-Host "  $SystemDir"
    Write-Host ''
    Write-Host '  [k] Keep data   — remove the binaries; preserve .env, storage, tokens'
    $purgeExtra = if ($Kind -eq 'docker') { ' (incl. Docker volumes)' } else { '' }
    Write-Host "  [p] Purge all   — delete the System Directory$purgeExtra"
    Write-Host '  [c] Cancel'
    Write-Host ''
    $ans = Read-Host -Prompt 'Choose'
    switch -Regex ($ans) {
        '^[kK]' { $Mode = 'keep' }
        '^[pP]' { $Mode = 'purge' }
        default { Write-Host 'Cancelled.'; exit 0 }
    }
}

# Pre-purge guard: refuse if the User Working Directory resolves inside the
# System Dir. Only runs when sqlite3.exe is available on PATH.
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
        if ($UserDir) {
            try {
                $expUser = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($UserDir))
                $expSys  = [System.IO.Path]::GetFullPath($SystemDir)
                if ($expUser -ieq $expSys -or $expUser.StartsWith($expSys + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                    Write-Host "Refusing to purge: User Working Directory ($UserDir) is inside the System Dir." -ForegroundColor Red
                    Write-Host "Move your data out of $SystemDir (or pick a different User Working Directory) first." -ForegroundColor Red
                    exit 1
                }
            } catch {
                # Path normalization failed — skip the guard rather than refusing.
            }
        }
    }
}

# Stop any backend process tracked via install.pid.
$PidFile = Join-Path $SystemDir 'install.pid'
if (Test-Path -LiteralPath $PidFile) {
    try {
        # NOTE: PowerShell has an automatic variable named $pid — use a non-clashing name.
        $childPid = [int](Get-Content -LiteralPath $PidFile -ErrorAction Stop | Select-Object -First 1)
        if ($childPid -gt 0) {
            try {
                Stop-Process -Id $childPid -Force -ErrorAction Stop
                Wait-Process -Id $childPid -Timeout 5 -ErrorAction SilentlyContinue
            } catch {
                # Process gone, no permission, or some other transient failure —
                # we'll proceed with file removal either way.
            }
        }
    } catch {
        # PID file unreadable or non-numeric — ignore.
    }
}

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
    if ($dockerUp) {
        $DockerDir = Join-Path $SystemDir 'docker'
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
    } else {
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

if ($Mode -eq 'purge') {
    # Defense against a stray empty or root path.
    $resolved = $SystemDir.TrimEnd('\','/')
    if (-not $resolved -or $resolved -match '^[A-Za-z]:[\\/]?$') {
        Write-Host "Refusing to purge a root-like path: '$SystemDir'" -ForegroundColor Red
        exit 1
    }
    Remove-Item -LiteralPath $SystemDir -Recurse -Force -ErrorAction Stop
    Write-Host "Removed $SystemDir."
    if ($UserDir) {
        Write-Host "User Working Directory preserved at: $UserDir"
    }
} else {
    # Keep mode: only the binaries and ephemeral state.
    $toRemove = @(
        'venv',
        'bin',
        'pip-cache',
        'uv-cache',
        'python',
        'install.pid',
        'server.pid',
        'install.log',
        'server.log',
        'server.err.log',
        '.upgrade.lock',
        '.upgrade.status.json',
        'upgrade.log',
        'upgrade-detached.log'
    )
    foreach ($name in $toRemove) {
        $p = Join-Path $SystemDir $name
        if (Test-Path -LiteralPath $p) {
            Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if ($Kind -eq 'docker') {
        # Drop the compose bundle so a reinstall regenerates it against the
        # current templates. Volumes were preserved by ``compose down``.
        Remove-Item -LiteralPath (Join-Path $SystemDir 'docker') -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Kept data in $SystemDir (.env, bootstrap.toml, storage\, tokens\, profile dirs)."
}

Write-Host 'Uninstall complete.'
