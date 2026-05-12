import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } from 'electron'
import { fileURLToPath } from 'node:url'
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import path from 'node:path'
// Pulled in lazily to keep the dev / web build (which doesn't ship
// electron-updater) functional. The require is wrapped below.
type AutoUpdaterModule = typeof import('electron-updater')

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// ── Runtime config (openpa-config.json) ─────────────────────────────────────
//
// Lives in the per-user app-data directory. The installer writes to it at
// the end of the install flow; the setup wizard updates it via IPC; the
// renderer reads it synchronously through the preload bridge so the agent
// URL is available before any module-level code runs.
//
// Defaults are intentionally permissive: agentUrl="" makes the UI prompt
// the user instead of silently pointing at a missing server.

type OpenPAConfig = {
  agentUrl: string
  deploymentType: 'local' | 'server' | ''
  autoUpdate: boolean
  channel: 'stable' | 'beta' | 'dev'
}

const CONFIG_DEFAULTS: OpenPAConfig = {
  agentUrl: '',
  deploymentType: '',
  autoUpdate: true,
  channel: 'stable',
}

function configPath(): string {
  return path.join(app.getPath('userData'), 'openpa-config.json')
}

function loadConfig(): OpenPAConfig {
  try {
    const raw = fs.readFileSync(configPath(), 'utf8')
    const parsed = JSON.parse(raw)
    return { ...CONFIG_DEFAULTS, ...parsed }
  } catch {
    return { ...CONFIG_DEFAULTS }
  }
}

function saveConfig(cfg: OpenPAConfig): void {
  const file = configPath()
  fs.mkdirSync(path.dirname(file), { recursive: true })
  // Atomic write: temp file + rename so a crashed write never leaves a
  // half-formed config behind for the next launch.
  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2), 'utf8')
  fs.renameSync(tmp, file)
}

// In-memory cache. Loaded once at app start; mutations go through
// updateConfig() so disk and memory stay in sync.
let runtimeConfig: OpenPAConfig = { ...CONFIG_DEFAULTS }

function updateConfig(patch: Partial<OpenPAConfig>): OpenPAConfig {
  runtimeConfig = { ...runtimeConfig, ...patch }
  saveConfig(runtimeConfig)
  return runtimeConfig
}

// The install script's ``.env`` is the canonical "OpenPA is installed
// on this machine" marker. We reconcile runtimeConfig.agentUrl against
// it on each launch so the user can re-trigger the first-run installer
// by deleting ~/.openpa (or wiping the contents).
function installEnvFile(): string {
  return path.join(app.getPath('home'), '.openpa', '.env')
}
function reconcileInstallStateWithDisk(): void {
  const installed = fs.existsSync(installEnvFile())
  if (!installed && runtimeConfig.agentUrl) {
    // User wiped ~/.openpa to re-run the first-run installer.
    updateConfig({ agentUrl: '', deploymentType: '' })
  } else if (installed && !runtimeConfig.agentUrl) {
    // The script was run outside the Electron app (e.g., via the CLI)
    // and we lost track of it. Adopt the default local agent URL — the
    // user can correct it later via settings if they actually pointed
    // openpa at a remote host.
    updateConfig({ agentUrl: 'http://localhost:1112', deploymentType: 'local' })
  }
}

// The built directory structure
//
// ├─┬─┬ dist
// │ │ └── index.html
// │ │
// │ ├─┬ dist-electron
// │ │ ├── main.js
// │ │ └── preload.mjs
// │
process.env.APP_ROOT = path.join(__dirname, '..')

// 🚧 Use ['ENV_NAME'] avoid vite:define plugin - Vite@2.x
export const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL']
export const MAIN_DIST = path.join(process.env.APP_ROOT, 'dist-electron')
export const RENDERER_DIST = path.join(process.env.APP_ROOT, 'dist')

process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL ? path.join(process.env.APP_ROOT, 'public') : RENDERER_DIST

let win: BrowserWindow | null
let tray: Tray | null = null
let isQuitting = false

// App version handler
ipcMain.handle('get-app-version', () => {
  return app.getVersion();
});

// Runtime config handlers. ``openpa:get-config-sync`` is the only sync IPC
// in the app — invoked once from the preload before the renderer boots so
// `window.openpa.config` is populated by the time module-level JS runs.
ipcMain.on('openpa:get-config-sync', (event) => {
  event.returnValue = runtimeConfig
})
ipcMain.handle('openpa:get-config', () => runtimeConfig)
ipcMain.handle('openpa:set-config', (_event, patch: Partial<OpenPAConfig>) => {
  return updateConfig(patch)
})

// ── First-run installer bridge ──────────────────────────────────────────────
//
// The Installer.vue view collects answers, asks main to detect the host
// environment, then asks main to download and run install.sh / install.ps1
// with those answers. Logs stream back via ``openpa:installer:log``;
// completion is signalled with ``openpa:installer:done``. We only allow
// one install at a time — concurrent installs fight over the same
// config files and PIDs.
//
// The install script source-of-truth lives in the openpa repo, not this
// one. Downloading it at runtime keeps the two repos decoupled and makes
// sure users get the latest fixes without an Electron app update.

const INSTALLER_SCRIPT_BASE =
  process.env.OPENPA_INSTALLER_BASE ??
  'https://raw.githubusercontent.com/openpa/openpa/main/install'

// Build-time install channel — baked in by Vite at compile time. Tells
// us which flag to pass the install script and (for dev) whether to use
// the local checkout instead of downloading from GitHub.
const INSTALL_CHANNEL: 'production' | 'test' | 'dev' = __OPENPA_INSTALL_CHANNEL__

let installerProcess: ChildProcess | null = null
// Long-running ``openpa serve`` we spawn after the user clicks Continue
// to Setup Wizard (and on subsequent launches when an install is
// detected). Tracked so before-quit can tear it down cleanly.
let backendProcess: ChildProcess | null = null

// ── Backend (``openpa serve``) lifecycle ──────────────────────────────────
//
// The install script *used* to start the backend itself. We moved that
// into Electron so the install step doesn't trigger a server startup —
// and therefore doesn't create the SQLite DB at install time. The
// backend (and the DB) only come into existence when the user clicks
// "Continue to Setup Wizard", or when the app is relaunched after an
// install has already completed.

function backendPidFilePath(): string {
  return path.join(app.getPath('home'), '.openpa', 'install.pid')
}

// Resolve the actual openpa executable, bypassing the user-facing shim.
//
// Node ≥18.20 / ≥20.12 / ≥21.7 refuses to ``spawn`` ``.cmd`` / ``.bat``
// files directly without ``shell: true`` — the CVE-2024-27980 mitigation
// surfaces as ``Error: spawn EINVAL``. We dodge it by reading the
// install-time shim (``~/.openpa/bin/openpa.cmd``), pulling the
// underlying ``openpa.exe`` path out of it, and spawning that instead.
// On POSIX the shim is a symlink, which ``spawn`` follows transparently.
function openpaExePath(): string {
  const home = app.getPath('home')
  const bin = path.join(home, '.openpa', 'bin')
  if (process.platform === 'win32') {
    const shim = path.join(bin, 'openpa.cmd')
    try {
      const content = fs.readFileSync(shim, 'utf8')
      // The shim install.ps1 writes is two lines:
      //   @echo off
      //   "C:\path\to\openpa.exe" %*
      const m = content.match(/^"([^"]+)"\s*%\*/m)
      if (m && m[1]) return m[1]
    } catch { /* fall through to default below */ }
    // Default install location when the shim is missing or unparseable.
    return path.join(home, '.openpa', 'venv', 'Scripts', 'openpa.exe')
  }
  return path.join(bin, 'openpa')
}

function backendHealthUrl(): string {
  // The install script writes ``.env`` with HOST/PORT, but
  // ``http://127.0.0.1:1112`` is the documented default and matches
  // the agent URL we persist in runtimeConfig. We don't parse .env
  // here — keep this simple.
  return 'http://127.0.0.1:1112/health'
}

function isBackendHealthy(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(backendHealthUrl(), { timeout: 1500 }, (res) => {
      res.resume()
      resolve((res.statusCode ?? 0) < 400)
    })
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}

async function waitForBackendHealthy(timeoutMs = 30000): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await isBackendHealthy()) return true
    await new Promise((r) => setTimeout(r, 500))
  }
  return false
}

async function startBackend(): Promise<{ ok: boolean; error?: string }> {
  // Already responding? Adopt the running instance and skip the spawn.
  if (await isBackendHealthy()) {
    return { ok: true }
  }
  // Already spawned but not healthy yet? Just wait.
  if (backendProcess && backendProcess.exitCode === null) {
    const ok = await waitForBackendHealthy()
    return ok ? { ok: true } : { ok: false, error: 'backend did not become healthy' }
  }

  const exe = openpaExePath()
  if (!fs.existsSync(exe)) {
    return { ok: false, error: `openpa executable missing at ${exe} — installer didn't finish?` }
  }

  const openpaHome = path.join(app.getPath('home'), '.openpa')
  const serverLog = path.join(openpaHome, 'server.log')
  const serverErr = path.join(openpaHome, 'server.err.log')

  let stdoutFd: number | undefined
  let stderrFd: number | undefined
  try {
    stdoutFd = fs.openSync(serverLog, 'a')
    stderrFd = fs.openSync(serverErr, 'a')
  } catch (err) {
    return { ok: false, error: `could not open server log files: ${String(err)}` }
  }

  let child: ChildProcess
  try {
    child = spawn(exe, ['serve'], {
      stdio: ['ignore', stdoutFd, stderrFd],
      windowsHide: true,
    })
  } catch (err) {
    try { if (stdoutFd) fs.closeSync(stdoutFd) } catch { /* ignore */ }
    try { if (stderrFd) fs.closeSync(stderrFd) } catch { /* ignore */ }
    return { ok: false, error: String(err) }
  }
  backendProcess = child

  // Persist the PID so before-quit can fall back to the file if we lose
  // the in-memory reference somehow.
  try { fs.writeFileSync(backendPidFilePath(), String(child.pid ?? '')) } catch { /* best-effort */ }

  child.on('exit', () => {
    if (backendProcess === child) backendProcess = null
    try { if (stdoutFd) fs.closeSync(stdoutFd) } catch { /* ignore */ }
    try { if (stderrFd) fs.closeSync(stderrFd) } catch { /* ignore */ }
  })

  const ok = await waitForBackendHealthy()
  if (!ok) {
    return { ok: false, error: `backend at ${backendHealthUrl()} did not respond after 30s` }
  }
  return { ok: true }
}

ipcMain.handle('openpa:server:start', async () => startBackend())

ipcMain.handle('openpa:installer:detect', async () => detectInstallEnvironment())

ipcMain.handle('openpa:installer:run', async (event, payload: InstallerRunPayload) => {
  if (installerProcess) {
    throw new Error('An install is already running.')
  }
  return runInstaller(event.sender, payload)
})

ipcMain.handle('openpa:installer:cancel', () => {
  if (!installerProcess) return false
  // SIGTERM gives the script a chance to clean up; if it ignores it,
  // Node will SIGKILL the orphan when the Electron app exits.
  try { installerProcess.kill('SIGTERM') } catch { /* already gone */ }
  return true
})

type InstallerEnvironment = {
  os: 'linux' | 'macos' | 'windows' | 'unknown'
  arch: string
  hasDocker: boolean
  hasPython: boolean
  pythonVersion: string
  recommendedMode: 'docker' | 'native'
  // Baked-in install channel. The renderer uses this to disable mode
  // options that aren't supported on the current channel (e.g. docker
  // is rejected under --dev because the Dockerfile would need a
  // source-COPY path; see install.sh's --dev guard).
  channel: 'production' | 'test' | 'dev'
}

type InstallerRunPayload = {
  deployment: 'local' | 'server'
  appHost?: string
  mode: 'docker' | 'native'
}

async function detectInstallEnvironment(): Promise<InstallerEnvironment> {
  // Detection runs commands that may be missing on PATH; ``runOnce`` swallows
  // non-zero exits so the UI gets a clean ``hasX = false`` rather than an
  // exception per check.
  const platform = process.platform
  const detected: InstallerEnvironment = {
    os: platform === 'linux' ? 'linux'
       : platform === 'darwin' ? 'macos'
       : platform === 'win32' ? 'windows'
       : 'unknown',
    arch: process.arch,
    hasDocker: false,
    hasPython: false,
    pythonVersion: '',
    recommendedMode: 'native',
    channel: INSTALL_CHANNEL,
  }

  const dockerVersion = await runOnce('docker', ['--version']).catch(() => null)
  if (dockerVersion) {
    // ``docker info`` confirms the daemon is reachable, not just that the
    // CLI is installed. Docker Desktop on macOS/Windows commonly has the
    // CLI on PATH while the daemon is stopped.
    const info = await runOnce('docker', ['info']).catch(() => null)
    detected.hasDocker = info !== null
  }

  // Try the most-specific Python name first to avoid picking up a system
  // 3.10 named just ``python3``. Mirrors the install script's logic.
  const pythonCandidates = platform === 'win32'
    ? [['py', ['-3.13', '-c', 'import sys;print("%d.%d" % sys.version_info[:2])']]]
    : [['python3.13', ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])']],
       ['python3',    ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])']]]
  for (const [cmd, args] of pythonCandidates as [string, string[]][]) {
    const v = await runOnce(cmd, args).catch(() => null)
    if (v && /^3\.(1[3-9]|[2-9]\d)$/.test(v.trim())) {
      detected.hasPython = true
      detected.pythonVersion = v.trim()
      break
    }
  }

  // Dev channel: native is faster to iterate on (no image build, no
  // container restart for source edits), so default to it even when
  // Docker is available. Users can still pick Docker manually in the UI.
  detected.recommendedMode =
    INSTALL_CHANNEL === 'dev' ? 'native'
      : detected.hasDocker ? 'docker'
      : 'native'
  return detected
}

function runOnce(command: string, args: string[], timeoutMs = 5000): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    let out = ''
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      try { child.kill('SIGKILL') } catch { /* ignore */ }
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { out += chunk.toString() })
    child.on('error', (err) => { clearTimeout(timer); reject(err) })
    child.on('close', (code) => {
      clearTimeout(timer)
      if (timedOut) return reject(new Error(`${command} timed out`))
      if (code !== 0) return reject(new Error(`${command} exited ${code}`))
      resolve(out.trim())
    })
  })
}

async function runInstaller(
  sender: Electron.WebContents,
  payload: InstallerRunPayload,
): Promise<{ exitCode: number }> {
  const send = (channel: string, message: unknown) => {
    if (!sender.isDestroyed()) sender.send(channel, message)
  }

  send('openpa:installer:log', { stream: 'info', line: `Detecting platform...` })
  const env = await detectInstallEnvironment()
  if (payload.mode === 'docker' && !env.hasDocker) {
    throw new Error('Docker mode selected but Docker is not available on this machine.')
  }
  if (payload.deployment === 'server' && !payload.appHost) {
    throw new Error('Server deployment requires a public host (IP or domain).')
  }

  // Resolve which install script to run.
  //
  // - Dev channel: use the local checkout's install/install.sh|ps1
  //   directly. ``--dev`` requires running from a checkout (the script
  //   refuses curl-pipe invocations), and ``npm run dev`` only ever
  //   runs out of the source tree so the path resolves to the working
  //   copy.
  // - Test / production: download from INSTALLER_SCRIPT_BASE. Re-running
  //   on every install picks up upstream fixes without an Electron app
  //   update.
  const isWindows = env.os === 'windows'
  const scriptName = isWindows ? 'install.ps1' : 'install.sh'
  let scriptPath: string
  if (INSTALL_CHANNEL === 'dev') {
    // __dirname after compile is <repo>/ui/dist-electron; the repo root
    // is two levels up. Resolve to <repo>/install/<scriptName>.
    scriptPath = path.resolve(__dirname, '..', '..', 'install', scriptName)
    if (!fs.existsSync(scriptPath)) {
      throw new Error(
        `Dev-channel install requires the local script at ${scriptPath}, ` +
        `but the file is missing. Run the Electron app from a checkout.`,
      )
    }
    send('openpa:installer:log', { stream: 'info', line: `Using local install script: ${scriptPath}` })
  } else {
    const scriptUrl = `${INSTALLER_SCRIPT_BASE}/${scriptName}`
    const scriptDir = path.join(app.getPath('userData'), 'installer')
    fs.mkdirSync(scriptDir, { recursive: true })
    scriptPath = path.join(scriptDir, scriptName)
    send('openpa:installer:log', { stream: 'info', line: `Downloading ${scriptUrl}...` })
    await downloadFile(scriptUrl, scriptPath)
    if (!isWindows) fs.chmodSync(scriptPath, 0o755)
  }

  // Build CLI args. We always pass --unattended + --no-launch so the
  // script doesn't prompt or open a browser — this UI handles both.
  const args: string[] = [
    '--deployment', payload.deployment,
    '--mode', payload.mode,
    '--unattended',
    '--no-launch',
  ]
  if (payload.appHost) args.push('--host', payload.appHost)

  // Channel routing. Dev gets the visible --dev/-Dev switch (which also
  // implies --mode native; the dev channel + docker combo is rejected
  // by the script). Test passes the hidden --channel test flag on bash;
  // on PowerShell the test channel travels via $env:OPENPA_INSTALL_CHANNEL
  // because PS can't truly hide a param() entry from Get-Help.
  if (INSTALL_CHANNEL === 'dev') {
    args.push('--dev')
  } else if (INSTALL_CHANNEL === 'test' && !isWindows) {
    args.push('--channel', 'test')
  }

  let cmd: string
  let cmdArgs: string[]
  if (isWindows) {
    cmd = 'powershell.exe'
    // -ExecutionPolicy Bypass sidesteps the user's machine policy without
    // mutating it; the bypass is scoped to this single invocation.
    const psArgs = args.flatMap((a) => {
      if (a === '--deployment') return ['-Deployment']
      if (a === '--host')       return ['-AppHost']
      if (a === '--mode')       return ['-Mode']
      if (a === '--unattended') return ['-Unattended']
      if (a === '--no-launch')  return ['-NoLaunch']
      if (a === '--dev')        return ['-Dev']
      return [a]
    })
    cmdArgs = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath, ...psArgs]
  } else {
    cmd = 'bash'
    cmdArgs = [scriptPath, ...args]
  }

  send('openpa:installer:log', {
    stream: 'info',
    line: `Running ${cmd} ${cmdArgs.join(' ')} (channel: ${INSTALL_CHANNEL})`,
  })

  return new Promise((resolve, reject) => {
    const child = spawn(cmd, cmdArgs, {
      env: {
        ...process.env,
        // Preserve any custom template/script base the user set, so
        // staging installs can be tested end-to-end from the GUI.
        OPENPA_TEMPLATE_BASE: process.env.OPENPA_TEMPLATE_BASE ?? `${INSTALLER_SCRIPT_BASE}/templates`,
        // PS test channel: -Channel was removed from param() so the only
        // way to activate it on Windows is the env var. Pass it through
        // for both bash (harmless; --channel flag wins) and PS.
        ...(INSTALL_CHANNEL === 'test' ? { OPENPA_INSTALL_CHANNEL: 'test' } : {}),
        // Tell the script the install is being driven by the Electron
        // app so it suppresses the "Wizard URL: …" handoff text. The
        // Electron app navigates to the in-window wizard itself once
        // the script reports exitCode = 0.
        OPENPA_INSTALLER_FRONTEND: 'electron',
      },
    })
    installerProcess = child

    child.stdout.on('data', (chunk: Buffer) => {
      send('openpa:installer:log', { stream: 'stdout', line: chunk.toString() })
    })
    child.stderr.on('data', (chunk: Buffer) => {
      send('openpa:installer:log', { stream: 'stderr', line: chunk.toString() })
    })

    // ``'close'`` waits until every stdio handle on the child tree
    // closes. On Windows the install script launches the openpa
    // backend via ``Start-Process``, which inherits the script's
    // stdout/stderr handles even though we redirect both to log files
    // — so ``'close'`` never fires after the script itself exits, and
    // the renderer's ``onDone`` callback never runs.
    //
    // ``'exit'`` fires as soon as the script process terminates,
    // independent of any inherited handles in detached grandchildren.
    // Log data is delivered through ``stdout.on('data')`` as it streams
    // (the last line is already flushed before the script returns), so
    // we don't lose output by switching events.
    let settled = false
    const finish = (code: number | null, err?: unknown) => {
      if (settled) return
      settled = true
      installerProcess = null
      if (err !== undefined) {
        send('openpa:installer:done', { exitCode: -1, error: String(err) })
        reject(err)
        return
      }
      const exitCode = code ?? -1
      if (exitCode === 0) {
        // Persist the agent URL the wizard should connect to. The
        // script also writes its own .env files; this just keeps the
        // Electron-side runtime config in sync so the next launch
        // routes straight to /setup.
        const host = payload.deployment === 'local' ? 'localhost' : payload.appHost!
        updateConfig({
          agentUrl: `http://${host}:1112`,
          deploymentType: payload.deployment,
        })
      }
      send('openpa:installer:done', { exitCode })
      resolve({ exitCode })
    }
    child.on('error', (err) => finish(null, err))
    child.on('exit', (code) => finish(code))
  })
}

function downloadFile(url: string, dest: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest)
    const req = https.get(url, (res) => {
      // Follow one level of redirects so GitHub's raw URL resolving via
      // a 302 doesn't trip us up.
      if (res.statusCode === 301 || res.statusCode === 302) {
        res.resume()
        const next = res.headers.location
        if (!next) return reject(new Error(`Redirect from ${url} without Location`))
        file.close()
        return downloadFile(next, dest).then(resolve, reject)
      }
      if (!res.statusCode || res.statusCode >= 400) {
        res.resume()
        return reject(new Error(`Failed to fetch ${url}: HTTP ${res.statusCode}`))
      }
      res.pipe(file)
      file.on('finish', () => file.close((err) => err ? reject(err) : resolve()))
    })
    req.on('error', (err) => {
      try { fs.unlinkSync(dest) } catch { /* not yet written */ }
      reject(err)
    })
  })
}


function createTray() {
  const icon = nativeImage.createFromPath(path.join(process.env.VITE_PUBLIC, 'tray-logo-64x64.png'))
  tray = new Tray(icon)
  tray.setToolTip('A2A Client')
  
  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show',
      click: () => {
        if (win) win.show()
      }
    },
    { type: 'separator' },
    {
      label: 'Exit',
      click: () => {
        isQuitting = true
        app.quit()
      }
    }
  ])
  
  tray.setContextMenu(contextMenu)
  
  tray.on('click', () => {
    if (win) win.show()
  })
}

function createWindow() {
  win = new BrowserWindow({
    width: 1100,
    height: 750,
    resizable: true,
    autoHideMenuBar: true,
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#242424',
      symbolColor: '#ffffff',
      height: 32
    },
    icon: path.join(process.env.VITE_PUBLIC, 'logo.svg'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
      devTools: !app.isPackaged,
    },
  })

  // Test active push message to Renderer-process.
  win.webContents.on('did-finish-load', () => {
    win?.webContents.send('main-process-message', (new Date).toLocaleString())
  })

  // Handle close event to hide window instead of quitting
  win.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault()
      win?.hide()
      return false
    }
  })

  if (VITE_DEV_SERVER_URL) {
    win.loadURL(VITE_DEV_SERVER_URL)
  } else {
    win.loadFile(path.join(RENDERER_DIST, 'index.html'))
  }
}

// ── Process tree cleanup on quit ────────────────────────────────────────
//
// The install script spawns the openpa backend in the background and
// writes the backend PID to ~/.openpa/install.pid. We track:
//   - the install script's own process (``installerProcess``)
//   - the backend PID from the pidfile
// and synchronously kill both trees on ``before-quit`` so closing the
// app reliably terminates everything it started — no orphaned PowerShell
// shells, no orphaned ``openpa serve`` processes.

function killProcessTreeSync(pid: number): void {
  if (!pid || pid <= 0) return
  try {
    if (process.platform === 'win32') {
      // /T = kill the entire process tree (children + grandchildren).
      // /F = force; spawn the tool synchronously so the kill completes
      // before Electron tears down.
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
    } else {
      // POSIX: try SIGTERM on the negative PID to hit the process group;
      // fall back to a regular SIGTERM on just the PID if the group call
      // isn't available (e.g., the child wasn't started with
      // ``detached: true``).
      try { process.kill(-pid, 'SIGTERM') } catch { try { process.kill(pid, 'SIGTERM') } catch { /* gone */ } }
    }
  } catch { /* ignore — process may already be gone */ }
}

function killTrackedProcessesSync(): void {
  // 1. The backend (``openpa serve``) tracked in-memory. This is the
  //    process Electron spawned via ``startBackend``; killing it
  //    directly takes the openpa.exe + uvicorn worker tree with it.
  if (backendProcess && backendProcess.pid) {
    killProcessTreeSync(backendProcess.pid)
    backendProcess = null
  }

  // 2. Fallback: the PID file (written by either the install script or
  //    ``startBackend``). Kills any backend we might have lost the
  //    in-memory reference to (e.g., the user wiped/restored
  //    ~/.openpa between launches).
  const serverPidFile = path.join(app.getPath('home'), '.openpa', 'install.pid')
  try {
    const pid = parseInt(fs.readFileSync(serverPidFile, 'utf8').trim(), 10)
    if (pid > 0) killProcessTreeSync(pid)
    try { fs.unlinkSync(serverPidFile) } catch { /* ignore */ }
  } catch { /* pid file missing — no install yet, or already cleaned */ }

  // 3. The install script itself, in case the user quit mid-install.
  if (installerProcess && installerProcess.pid) {
    killProcessTreeSync(installerProcess.pid)
    installerProcess = null
  }
}

// Quit when all windows are closed
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
    win = null
  }
})

app.on('before-quit', () => {
  isQuitting = true
  killTrackedProcessesSync()
})

// ── Auto-update (electron-updater + GitHub Releases) ────────────────────────
//
// The updater config (``publish:`` in electron-builder.json5) tells the
// runtime where to look for ``latest.yml`` and the binaries. Here we just
// drive the lifecycle:
//
//   - On app start (when packaged + autoUpdate=true), kick off an
//     ``checkForUpdates`` call. electron-updater handles HTTPS, signature
//     verification, and the disk cache.
//   - As the updater progresses, forward events to the renderer via
//     ``openpa:updater:status`` so the UpdateBanner can render. We never
//     auto-quit; the renderer asks the user before installing.
//   - ``openpa:updater:install`` quits and applies on user confirmation.
//
// Calling these from a dev launch (where the app isn't packaged) is a
// no-op — electron-updater refuses to update an unpackaged process,
// which is what we want.

let updater: AutoUpdaterModule['autoUpdater'] | null = null

function setupAutoUpdater(): void {
  if (!app.isPackaged) return  // dev launches don't auto-update
  try {
    // Require lazily so a missing optional dep doesn't break the
    // whole main process (e.g., during a CI test run with deps stripped).
    const mod: AutoUpdaterModule = require('electron-updater')
    updater = mod.autoUpdater
  } catch (err) {
    console.warn('[main] electron-updater unavailable, skipping auto-update:', err)
    return
  }

  updater.autoDownload = false           // we ask the user first
  updater.autoInstallOnAppQuit = true     // staged install on next quit

  const send = (status: string, payload: Record<string, unknown> = {}) => {
    // BrowserWindow may not exist yet on the very first event; in that
    // case we drop. The renderer queries ``openpa:updater:status`` on
    // mount via ``check`` and gets the latest known state.
    if (win && !win.isDestroyed()) {
      win.webContents.send('openpa:updater:status', { status, ...payload })
    }
  }

  updater.on('checking-for-update',    () => send('checking'))
  updater.on('update-available',       (info) => send('available', { info }))
  updater.on('update-not-available',   (info) => send('up_to_date', { info }))
  updater.on('error',                  (err)  => send('error', { error: String(err) }))
  updater.on('download-progress',      (prog) => send('downloading', { progress: prog }))
  updater.on('update-downloaded',      (info) => send('ready', { info }))

  if (runtimeConfig.autoUpdate !== false) {
    updater.checkForUpdates().catch((err) => {
      console.warn('[main] initial update check failed:', err)
    })
  }
}

ipcMain.handle('openpa:updater:check', async () => {
  if (!updater) return { status: 'unavailable' }
  try {
    const result = await updater.checkForUpdates()
    if (result?.updateInfo) {
      return { status: 'available', info: result.updateInfo }
    }
    return { status: 'up_to_date' }
  } catch (err) {
    return { status: 'error', error: String(err) }
  }
})

ipcMain.handle('openpa:updater:download', async () => {
  if (!updater) return { ok: false, error: 'updater unavailable' }
  try {
    await updater.downloadUpdate()
    return { ok: true }
  } catch (err) {
    return { ok: false, error: String(err) }
  }
})

ipcMain.handle('openpa:updater:install', () => {
  if (!updater) return { ok: false, error: 'updater unavailable' }
  // ``isSilent=false, isForceRunAfter=true`` → quit + install + relaunch.
  setImmediate(() => updater!.quitAndInstall(false, true))
  return { ok: true }
})

app.whenReady().then(() => {
  // Load the persisted config before the window opens so the preload's
  // sync IPC returns a populated value on the very first request.
  runtimeConfig = loadConfig();
  // Reconcile with ~/.openpa/.env so a deleted install dir re-triggers
  // the first-run installer instead of falling through to a broken UI.
  reconcileInstallStateWithDisk();
  createTray();
  createWindow();
  setupAutoUpdater();
  // Subsequent launches: if the install has already completed, fire the
  // backend up so the chat / profile-selector views have a server to
  // talk to. First-run launches skip this — the backend (and the
  // SQLite DB) only spawn after the user clicks Continue in the
  // installer flow.
  if (fs.existsSync(installEnvFile())) {
    void startBackend()
  }
})
