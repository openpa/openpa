import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } from 'electron'
import { fileURLToPath } from 'node:url'
import { spawn, type ChildProcess } from 'node:child_process'
import fs from 'node:fs'
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

let installerProcess: ChildProcess | null = null

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

  detected.recommendedMode = detected.hasDocker ? 'docker' : 'native'
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
  if (payload.mode === 'native' && !env.hasPython) {
    throw new Error('Native mode selected but Python 3.13+ was not found on PATH.')
  }
  if (payload.deployment === 'server' && !payload.appHost) {
    throw new Error('Server deployment requires a public host (IP or domain).')
  }

  // Download the installer script into the user-data dir. Re-downloading
  // on every install run is intentional: it picks up upstream fixes
  // without requiring an Electron app update.
  const isWindows = env.os === 'windows'
  const scriptName = isWindows ? 'install.ps1' : 'install.sh'
  const scriptUrl = `${INSTALLER_SCRIPT_BASE}/${scriptName}`
  const scriptDir = path.join(app.getPath('userData'), 'installer')
  fs.mkdirSync(scriptDir, { recursive: true })
  const scriptPath = path.join(scriptDir, scriptName)

  send('openpa:installer:log', { stream: 'info', line: `Downloading ${scriptUrl}...` })
  await downloadFile(scriptUrl, scriptPath)
  if (!isWindows) fs.chmodSync(scriptPath, 0o755)

  // Build CLI args. We always pass --unattended + --no-launch so the
  // script doesn't prompt or open a browser — this UI handles both.
  const args: string[] = [
    '--deployment', payload.deployment,
    '--mode', payload.mode,
    '--unattended',
    '--no-launch',
  ]
  if (payload.appHost) args.push('--host', payload.appHost)

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
      return [a]
    })
    cmdArgs = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath, ...psArgs]
  } else {
    cmd = 'bash'
    cmdArgs = [scriptPath, ...args]
  }

  send('openpa:installer:log', { stream: 'info', line: `Running ${cmd} ${cmdArgs.join(' ')}` })

  return new Promise((resolve, reject) => {
    const child = spawn(cmd, cmdArgs, {
      env: {
        ...process.env,
        // Preserve any custom template/script base the user set, so
        // staging installs can be tested end-to-end from the GUI.
        OPENPA_TEMPLATE_BASE: process.env.OPENPA_TEMPLATE_BASE ?? `${INSTALLER_SCRIPT_BASE}/templates`,
      },
    })
    installerProcess = child

    child.stdout.on('data', (chunk: Buffer) => {
      send('openpa:installer:log', { stream: 'stdout', line: chunk.toString() })
    })
    child.stderr.on('data', (chunk: Buffer) => {
      send('openpa:installer:log', { stream: 'stderr', line: chunk.toString() })
    })
    child.on('error', (err) => {
      installerProcess = null
      send('openpa:installer:done', { exitCode: -1, error: String(err) })
      reject(err)
    })
    child.on('close', (code) => {
      installerProcess = null
      const exitCode = code ?? -1
      // On success, persist the agent URL the wizard should connect to.
      // The script also writes its own .env files; this just keeps the
      // Electron-side runtime config in sync so the next launch routes
      // straight to /setup.
      if (exitCode === 0) {
        const host = payload.deployment === 'local' ? 'localhost' : payload.appHost!
        updateConfig({
          agentUrl: `http://${host}:1112`,
          deploymentType: payload.deployment,
        })
      }
      send('openpa:installer:done', { exitCode })
      resolve({ exitCode })
    })
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

// Quit when all windows are closed
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
    win = null
  }
})

app.on('before-quit', () => {
  isQuitting = true;
});

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
  createTray();
  createWindow();
  setupAutoUpdater();
})
