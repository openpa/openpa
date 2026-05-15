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
  deploymentType: 'local' | 'server' | 'custom' | ''
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
//
// Docker installs also stamp a marker into ~/.openpa/docker/.env (the
// compose env file). We treat that as a fallback so users whose Docker
// install predates the top-level marker fix don't get bounced back to
// the wizard on app upgrade.
function installMarkerExists(): boolean {
  const home = app.getPath('home')
  return (
    fs.existsSync(path.join(home, '.openpa', '.env')) ||
    fs.existsSync(path.join(home, '.openpa', 'docker', '.env'))
  )
}
function reconcileInstallStateWithDisk(): void {
  const installed = installMarkerExists()
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

type WindowKind = 'main' | 'settings' | 'vnc'
const windows = new Set<BrowserWindow>()
const windowKinds = new WeakMap<BrowserWindow, WindowKind>()
// Most recently focused non-VNC window. Used by tray "Show", the
// second-instance fallback, and by `installer` / `backend-upgrade` IPC
// senders that need to know which window initiated them.
let mainWin: BrowserWindow | null = null
let tray: Tray | null = null
// Populated by fetchCapabilities() once the backend is reachable. Drives
// the conditional "Open VNC Desktop" entry in the tray / jumplist / dock.
let installMode: 'docker' | 'native' | null = null

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
  const next = updateConfig(patch)
  if (patch.agentUrl !== undefined) {
    // Backend host changed — its install_mode may have flipped, so the
    // VNC tray/jumplist entry may need to appear or disappear.
    void fetchCapabilities()
  }
  return next
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
// Short-lived ``openpa upgrade --yes`` child spawned by the in-app
// upgrade flow. At most one in flight; tracked so before-quit can kill
// it (the runner's lock-file recovery will roll back on next launch).
let upgradeProcess: ChildProcess | null = null

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

// ── In-app backend upgrade ──────────────────────────────────────────────
//
// Runs ``openpa upgrade --yes`` as a subprocess and streams its output
// to the renderer so the UpdateBanner can show a live progress modal
// instead of telling the user to open a terminal. Matches the manual
// CLI flow exactly:
//
//   - The backend stays running during the upgrade. The Python runner's
//     ``_wait_for_health`` step probes /health on the (still-running)
//     old backend; if /health doesn't answer the runner rolls back.
//     This is the same contract the CLI relies on.
//   - On successful exit we restart the backend so the new wheel's code
//     gets imported. The shell holds the subprocess handle for its
//     lifetime; without a restart, the user would keep running the old
//     code until they quit the app.
//   - On failure, the runner has already restored the backup and pip-
//     installed the previous version. We leave the (old) backend
//     running and surface the failure to the renderer.
ipcMain.handle('openpa:backend-upgrade:apply', async (event) => {
  return runBackendUpgrade(event.sender)
})

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
  // Baked-in install channel. The renderer uses this for display
  // (badge in the welcome step) and as one of the inputs to
  // ``recommendInstallMode``; the recommended mode itself is no
  // longer computed here.
  channel: 'production' | 'test' | 'dev'
}

type InstallerRunPayload = {
  deployment: 'local' | 'server' | 'custom'
  appHost?: string
  mode: 'docker' | 'native'
  /** Advanced .env overrides for the `custom` deployment. Keys mirror
   *  install/catalog.toml's deployments.custom.advanced_fields[].key. */
  customFields?: {
    listen_host?: string
    public_url?: string
    allowed_origins?: string
    wizard_preset?: string
  }
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

  // Recommendation is no longer computed here — the renderer derives
  // it from install/catalog.toml's [modes] table via
  // ``recommendInstallMode`` so install.sh, install.ps1, and the
  // Setup Wizard agree on the default. Detection only reports raw
  // capabilities (hasDocker, hasPython); the recommendation falls out
  // from the catalog's mode order + requires.
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
  //   directly. ``--channel dev`` requires running from a checkout (the
  //   script refuses curl-pipe invocations), and ``npm run dev`` only
  //   ever runs out of the source tree so the path resolves to the
  //   working copy.
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
  // Forward custom-deployment overrides so the install scripts skip
  // their interactive prompts. Empty values are dropped so the scripts'
  // catalog defaults take effect for fields the user didn't fill in.
  if (payload.deployment === 'custom' && payload.customFields) {
    const cf = payload.customFields
    if (cf.listen_host)     args.push('--listen-host',     cf.listen_host)
    if (cf.public_url)      args.push('--public-url',      cf.public_url)
    if (cf.allowed_origins) args.push('--allowed-origins', cf.allowed_origins)
    if (cf.wizard_preset)   args.push('--wizard-preset',   cf.wizard_preset)
  }

  // Channel routing — both shells accept --channel / -Channel. Production
  // is the default in both installers, so only forward non-production
  // channels to keep the spawn args minimal.
  if (INSTALL_CHANNEL !== 'production') {
    args.push('--channel', INSTALL_CHANNEL)
  }

  let cmd: string
  let cmdArgs: string[]
  if (isWindows) {
    cmd = 'powershell.exe'
    // -ExecutionPolicy Bypass sidesteps the user's machine policy without
    // mutating it; the bypass is scoped to this single invocation.
    const psArgs = args.flatMap((a) => {
      if (a === '--deployment')      return ['-Deployment']
      if (a === '--host')            return ['-AppHost']
      if (a === '--mode')            return ['-Mode']
      if (a === '--unattended')      return ['-Unattended']
      if (a === '--no-launch')       return ['-NoLaunch']
      if (a === '--channel')         return ['-Channel']
      if (a === '--listen-host')     return ['-ListenHost']
      if (a === '--public-url')      return ['-PublicUrl']
      if (a === '--allowed-origins') return ['-AllowedOrigins']
      if (a === '--wizard-preset')   return ['-WizardPreset']
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
        // Resolve the agent URL the renderer should connect to next.
        //   - local : loopback
        //   - server: the public host the user typed
        //   - custom: the public URL the user gave (or localhost if blank)
        let resolvedAgentUrl: string
        if (payload.deployment === 'local') {
          resolvedAgentUrl = 'http://localhost:1112'
        } else if (payload.deployment === 'custom') {
          const pub = payload.customFields?.public_url ?? ''
          // Drop the path component, keep the scheme+host+port. Falls back
          // to localhost:1112 when the operator left public_url empty.
          if (pub) {
            try {
              const u = new URL(pub)
              resolvedAgentUrl = `${u.protocol}//${u.host}`
            } catch {
              resolvedAgentUrl = 'http://localhost:1112'
            }
          } else {
            resolvedAgentUrl = 'http://localhost:1112'
          }
        } else {
          resolvedAgentUrl = `http://${payload.appHost!}:1112`
        }
        updateConfig({
          agentUrl: resolvedAgentUrl,
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

// ── Backend upgrade implementation ──────────────────────────────────────
//
// Spawns ``openpa upgrade --yes`` and forwards its stdout/stderr to the
// renderer line-by-line. The renderer mirrors the events into a modal
// log view. Three IPC channels are used:
//
//   openpa:backend-upgrade:status — phase transitions ('starting',
//                                   'upgrading', 'restarting')
//   openpa:backend-upgrade:log    — every raw line of upgrade output
//   openpa:backend-upgrade:done   — terminal result + exit code
//
// Reuses the existing log buffering pattern from runInstaller so the
// renderer can subscribe with the same shape as the installer bridge.

async function runBackendUpgrade(
  sender: Electron.WebContents,
): Promise<{ exitCode: number; ok: boolean; error?: string }> {
  if (upgradeProcess) {
    throw new Error('An upgrade is already running.')
  }

  const send = (channel: string, message: unknown) => {
    if (!sender.isDestroyed()) sender.send(channel, message)
  }

  const exe = openpaExePath()
  if (!fs.existsSync(exe)) {
    const error = `openpa executable missing at ${exe}`
    send('openpa:backend-upgrade:done', { exitCode: -1, ok: false, error })
    return { exitCode: -1, ok: false, error }
  }

  send('openpa:backend-upgrade:status', { phase: 'starting' })
  send('openpa:backend-upgrade:log', {
    stream: 'info',
    line: `$ ${exe} upgrade --yes`,
  })

  return new Promise((resolve) => {
    let child: ChildProcess
    try {
      child = spawn(exe, ['upgrade', '--yes'], {
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (err) {
      const error = String(err)
      send('openpa:backend-upgrade:done', { exitCode: -1, ok: false, error })
      resolve({ exitCode: -1, ok: false, error })
      return
    }
    upgradeProcess = child
    send('openpa:backend-upgrade:status', { phase: 'upgrading' })

    const forward = (stream: 'stdout' | 'stderr') => (chunk: Buffer) => {
      // The Python runner prints one event per line; preserve that
      // boundary for the renderer instead of re-buffering by chunk.
      const text = chunk.toString()
      for (const line of text.split(/\r?\n/)) {
        if (line.length > 0) {
          send('openpa:backend-upgrade:log', { stream, line })
        }
      }
    }
    child.stdout?.on('data', forward('stdout'))
    child.stderr?.on('data', forward('stderr'))

    let settled = false
    const finish = async (code: number | null, err?: unknown) => {
      if (settled) return
      settled = true
      upgradeProcess = null

      if (err !== undefined) {
        const error = String(err)
        send('openpa:backend-upgrade:done', { exitCode: -1, ok: false, error })
        resolve({ exitCode: -1, ok: false, error })
        return
      }

      const exitCode = code ?? -1
      if (exitCode !== 0) {
        // The Python runner already restored the backup and pip-
        // installed the previous version. The old backend is still
        // running; nothing to restart.
        send('openpa:backend-upgrade:done', { exitCode, ok: false })
        resolve({ exitCode, ok: false })
        return
      }

      // Success path: restart the backend so the new wheel is loaded.
      send('openpa:backend-upgrade:status', { phase: 'restarting' })
      if (backendProcess && backendProcess.pid) {
        killProcessTreeSync(backendProcess.pid)
        backendProcess = null
      }
      // Give the OS a beat to release the listen port before we respawn;
      // SO_REUSEADDR isn't set on Windows by default and a too-fast
      // restart can hit EADDRINUSE.
      await new Promise((r) => setTimeout(r, 1000))
      const restart = await startBackend()
      if (!restart.ok) {
        send('openpa:backend-upgrade:done', {
          exitCode,
          ok: false,
          error: `backend failed to restart: ${restart.error ?? 'unknown'}`,
        })
        resolve({ exitCode, ok: false, error: restart.error })
        return
      }
      send('openpa:backend-upgrade:done', { exitCode, ok: true })
      void fetchCapabilities()
      resolve({ exitCode, ok: true })
    }
    child.on('error', (e) => { void finish(null, e) })
    child.on('exit', (code) => { void finish(code) })
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


// ── Multi-window: tray, jumplist, dock, and the window factory ──────────
//
// One process, one tray icon, many BrowserWindows. The tray menu, the
// Windows taskbar jumplist, and the macOS dock menu all surface the
// same three actions (VNC entry conditional on the backend being the
// `openpa/openpa-desktop` Docker image). Each click opens a fresh
// independent window — repeat clicks intentionally do NOT focus an
// existing window. The single-instance lock + the ``second-instance``
// handler ensure jumplist re-launches dispatch into the existing
// process rather than spawning a duplicate (which would yield a second
// tray icon).

function broadcastToAppWindows(channel: string, payload: unknown): void {
  for (const w of windows) {
    if (w.isDestroyed()) continue
    // VNC windows have no preload and no contextBridge, so IPC channels
    // would land in a renderer that can't decode them. Skip.
    if (windowKinds.get(w) === 'vnc') continue
    w.webContents.send(channel, payload)
  }
}

function focusMostRecentAppWindow(): void {
  const focusOne = (w: BrowserWindow): void => {
    if (w.isMinimized()) w.restore()
    if (!w.isVisible()) w.show()
    w.focus()
  }
  if (mainWin && !mainWin.isDestroyed()) { focusOne(mainWin); return }
  for (const w of windows) {
    if (w.isDestroyed()) continue
    if (windowKinds.get(w) === 'vnc') continue
    focusOne(w)
    return
  }
  createAppWindow('main')
}

function vncUrlFromAgentUrl(agentUrl: string): string | null {
  if (!agentUrl) return null
  try {
    const u = new URL(agentUrl)
    // noVNC is served from the same host as the agent, but on the
    // docker-compose default port 6080. (See NOVNC_PORT:-6080 in
    // install/templates/docker-compose.yml.tmpl.)
    u.port = '6080'
    u.pathname = '/vnc.html'
    u.search = ''
    u.hash = ''
    // ``autoconnect`` skips noVNC's connect splash; ``resize=remote`` is
    // standard UX polish for a windowed viewer.
    return `${u.toString()}?autoconnect=1&resize=remote`
  } catch {
    return null
  }
}

function fetchCapabilities(): Promise<void> {
  return new Promise<void>((resolve) => {
    const finish = () => {
      rebuildTrayMenu()
      rebuildJumpList()
      rebuildDockMenu()
      resolve()
    }
    if (!runtimeConfig.agentUrl) {
      installMode = null
      finish()
      return
    }
    let url: URL
    try {
      url = new URL('/api/services/capabilities', runtimeConfig.agentUrl)
    } catch {
      finish()
      return
    }
    const client = url.protocol === 'https:' ? https : http
    const req = client.get(url.toString(), { timeout: 4000 }, (res) => {
      if ((res.statusCode ?? 0) >= 400) { res.resume(); finish(); return }
      let body = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => { body += chunk })
      res.on('end', () => {
        try {
          const parsed = JSON.parse(body) as { install_mode?: 'docker' | 'native' | null }
          installMode = parsed.install_mode ?? null
        } catch { /* leave installMode unchanged on parse failure */ }
        finish()
      })
    })
    // Network errors or timeouts: keep last-known installMode to avoid
    // flickering the VNC entry off during transient outages.
    req.on('error', () => finish())
    req.on('timeout', () => { req.destroy(); finish() })
  })
}

// ── Windows taskbar jumplist ────────────────────────────────────────────
//
// Each task uses ``--open=<target>`` so the ``second-instance`` handler
// in the original process knows which window to spawn. Using
// ``setJumpList`` (rather than ``setUserTasks``) gives us an explicit
// category list, so Windows shows ONLY our entries and doesn't stitch
// in a default "launch via installed shortcut" task derived from the
// AppUserModelID.

function rebuildJumpList(): void {
  if (process.platform !== 'win32') return
  // In dev, ``process.execPath`` is node_modules/electron/dist/electron.exe;
  // running it with no args lands on default_app.asar's "Electron is
  // running" page. Pass the project root (the directory containing
  // package.json) so electron.exe boots our own main entry. In packaged
  // mode, ``process.execPath`` is "OpenPA App.exe" and runs the app
  // on its own — no leading args needed.
  const baseArg = app.isPackaged ? '' : (process.env.APP_ROOT ?? '')
  const iconPath = path.join(process.env.VITE_PUBLIC, 'logo.ico')
  const mkTask = (target: WindowKind, title: string, description: string): Electron.JumpListItem => {
    const argParts: string[] = []
    if (baseArg) argParts.push(`"${baseArg}"`)
    argParts.push(`--open=${target}`)
    return {
      type: 'task',
      program: process.execPath,
      args: argParts.join(' '),
      iconPath,
      iconIndex: 0,
      title,
      description,
    }
  }

  const items: Electron.JumpListItem[] = []
  if (runtimeConfig.agentUrl && installMode === 'docker' && vncUrlFromAgentUrl(runtimeConfig.agentUrl)) {
    items.push(mkTask('vnc', 'Open VNC Desktop', 'Open the OpenPA desktop VNC viewer'))
  }
  if (runtimeConfig.agentUrl) {
    items.push(mkTask('main', 'Open Main Page', 'Open a new OpenPA chat window'))
    items.push(mkTask('settings', 'Open Settings', 'Open the OpenPA settings window'))
  } else {
    // Pre-install: at least give the user a way to relaunch the wizard.
    items.push(mkTask('main', 'Open OpenPA', 'Open the OpenPA application'))
  }

  app.setJumpList([{ type: 'tasks', items }])
}

function rebuildTrayMenu(): void {
  if (!tray) return
  const items: Electron.MenuItemConstructorOptions[] = []
  if (runtimeConfig.agentUrl && installMode === 'docker' && vncUrlFromAgentUrl(runtimeConfig.agentUrl)) {
    items.push({ label: 'Open VNC Desktop', click: () => { createAppWindow('vnc') } })
  }
  if (runtimeConfig.agentUrl) {
    items.push({ label: 'Open Main Page', click: () => { createAppWindow('main') } })
    items.push({ label: 'Open Settings',  click: () => { createAppWindow('settings') } })
    items.push({ type: 'separator' })
  }
  items.push({ label: 'Show', click: () => focusMostRecentAppWindow() })
  items.push({ label: 'Exit', click: () => { app.quit() } })
  tray.setContextMenu(Menu.buildFromTemplate(items))
}

function rebuildDockMenu(): void {
  if (process.platform !== 'darwin') return
  const dock = app.dock
  if (!dock) return
  const items: Electron.MenuItemConstructorOptions[] = []
  if (runtimeConfig.agentUrl && installMode === 'docker' && vncUrlFromAgentUrl(runtimeConfig.agentUrl)) {
    items.push({ label: 'Open VNC Desktop', click: () => { createAppWindow('vnc') } })
  }
  if (runtimeConfig.agentUrl) {
    items.push({ label: 'Open Main Page', click: () => { createAppWindow('main') } })
    items.push({ label: 'Open Settings',  click: () => { createAppWindow('settings') } })
  }
  dock.setMenu(Menu.buildFromTemplate(items))
}

function createTray(): void {
  const icon = nativeImage.createFromPath(path.join(process.env.VITE_PUBLIC, 'tray-logo-64x64.png'))
  tray = new Tray(icon)
  tray.setToolTip('OpenPA')
  rebuildTrayMenu()
  tray.on('click', () => focusMostRecentAppWindow())
}

function createAppWindow(target: WindowKind): BrowserWindow {
  if (target === 'vnc') {
    const vncUrl = vncUrlFromAgentUrl(runtimeConfig.agentUrl)
    if (!vncUrl) {
      // Shouldn't reach here — the tray/jumplist gating hides the entry
      // when there's no agentUrl. Belt-and-suspenders: fall back to main.
      return createAppWindow('main')
    }
    const w = new BrowserWindow({
      width: 1280,
      height: 800,
      resizable: true,
      autoHideMenuBar: true,
      icon: path.join(process.env.VITE_PUBLIC, 'logo.png'),
      title: 'OpenPA VNC Desktop',
      webPreferences: {
        // No preload — this window loads third-party content (noVNC)
        // and must not have access to the openpa IPC bridge.
        contextIsolation: true,
        sandbox: true,
        devTools: !app.isPackaged,
      },
    })
    windows.add(w)
    windowKinds.set(w, 'vnc')
    w.on('closed', () => {
      windows.delete(w)
      if (mainWin === w) mainWin = null
    })
    void w.loadURL(vncUrl)
    return w
  }

  const w = new BrowserWindow({
    width: 1100,
    height: 750,
    resizable: true,
    autoHideMenuBar: true,
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#242424',
      symbolColor: '#ffffff',
      height: 32,
    },
    icon: path.join(process.env.VITE_PUBLIC, 'logo.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
      devTools: !app.isPackaged,
    },
  })
  windows.add(w)
  windowKinds.set(w, target)
  mainWin = w

  w.on('focus', () => { mainWin = w })
  w.on('closed', () => {
    windows.delete(w)
    if (mainWin === w) mainWin = null
  })

  w.webContents.on('did-finish-load', () => {
    if (!w.isDestroyed()) {
      w.webContents.send('main-process-message', (new Date).toLocaleString())
    }
  })

  const hash = target === 'settings' ? '#/?openpa_window=settings' : '#/?openpa_window=main'
  if (VITE_DEV_SERVER_URL) {
    void w.loadURL(VITE_DEV_SERVER_URL + hash)
  } else {
    void w.loadFile(path.join(RENDERER_DIST, 'index.html'), { hash: hash.slice(1) })
  }
  return w
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

  // 4. An in-flight ``openpa upgrade`` child, if the user quit mid-
  //    upgrade. Killing it leaves ``~/.openpa/.upgrade.lock`` behind;
  //    the runner's ``acquire_lock_or_recover`` rolls back from the
  //    captured backup on the next backend boot.
  if (upgradeProcess && upgradeProcess.pid) {
    killProcessTreeSync(upgradeProcess.pid)
    upgradeProcess = null
  }
}

// Closing the last window does NOT quit. The tray icon stays alive so
// the user can reopen Main / Settings / VNC from it. Exit is reached
// only via Tray > Exit, which calls app.quit() and triggers the
// before-quit cleanup below. Default Electron behavior on non-macOS
// would quit here; this handler suppresses that.
app.on('window-all-closed', () => { /* keep app alive in tray */ })

app.on('before-quit', () => {
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
    // No windows yet? Drop — renderers re-query via
    // ``openpa:updater:check`` on mount to fetch the latest state.
    broadcastToAppWindows('openpa:updater:status', { status, ...payload })
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

// Identify OpenPA distinctly to Windows shell as early as possible
// (before app.whenReady, before any window is created). Must match the
// appId in electron-builder.json5 so the installer's shortcut and the
// running process group under the same taskbar entry.
//
// Dev runs use a ".dev" suffix so they don't share a taskbar identity
// with the installed packaged build — otherwise Windows surfaces the
// installed "OpenPA App" Start Menu shortcut as an extra jumplist
// entry that launches a parallel packaged instance, bypassing our
// single-instance lock.
//
// Note: in dev mode the jumplist will still show an extra "Electron"
// entry because Windows derives a fallback launch label from the
// running .exe's VersionInfo (node_modules/electron/dist/electron.exe
// declares FileDescription="Electron") when the AppUserModelID has no
// registered Start Menu shortcut. The packaged build doesn't hit this
// — its .exe has the right metadata and the installer registers a
// proper shortcut.
if (process.platform === 'win32') {
  app.setAppUserModelId(app.isPackaged ? 'openpa-ui.client' : 'openpa-ui.client.dev')
}

// Single-instance lock — clicking the taskbar jumplist task re-runs the
// .exe, which would otherwise spawn a duplicate process. Acquiring the
// lock makes the second invocation exit immediately while the original
// process opens the requested window via the ``second-instance`` event.
// This is also what guarantees a single tray icon: only the first
// process ever runs createTray().
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', (_event, argv) => {
    const openArg = argv.find((a) => a.startsWith('--open='))
    if (openArg) {
      const target = openArg.slice('--open='.length)
      if (target === 'main' || target === 'settings' || target === 'vnc') {
        createAppWindow(target)
        return
      }
    }
    focusMostRecentAppWindow()
  })
}

app.whenReady().then(() => {
  // Load the persisted config before the window opens so the preload's
  // sync IPC returns a populated value on the very first request.
  runtimeConfig = loadConfig();
  // Reconcile with ~/.openpa/.env so a deleted install dir re-triggers
  // the first-run installer instead of falling through to a broken UI.
  reconcileInstallStateWithDisk();
  createTray();
  rebuildJumpList();
  rebuildDockMenu();
  createAppWindow('main');
  setupAutoUpdater();
  // Subsequent launches: if the install has already completed, fire the
  // backend up so the chat / profile-selector views have a server to
  // talk to. First-run launches skip this — the backend (and the
  // SQLite DB) only spawn after the user clicks Continue in the
  // installer flow.
  if (installMarkerExists()) {
    void (async () => {
      const r = await startBackend()
      if (r.ok) await fetchCapabilities()
    })()
  }
})
