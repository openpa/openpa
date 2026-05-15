/// <reference types="vite/client" />

// Global flag for Electron environment detection (build-time define).
declare const __IS_ELECTRON__: boolean;

// Build-time app version (synced from app/__version__.py by
// scripts/sync_ui_version.py and exposed via Vite ``define``).
declare const __APP_VERSION__: string;

// Build-time install channel — which OpenPA release stream the bundled
// installer points at. ``npm run dev`` → ``"dev"``; ``npm run build``
// → ``"production"``; ``npm run build:test`` → ``"test"``. Computed in
// vite.config.ts from ``mode`` + ``command``.
declare const __OPENPA_INSTALL_CHANNEL__: 'production' | 'test' | 'dev';

// Runtime config bridge — populated by electron/preload.ts. ``config`` is
// a synchronous snapshot loaded before the renderer initializes; the
// async getters/setters round-trip to the main process so the wizard can
// update the persisted JSON.
//
// Always defined under Electron. Undefined under the web build, so callers
// must guard with optional chaining and fall back to ``import.meta.env``.
type OpenPAInstallerEnvironment = {
  os: 'linux' | 'macos' | 'windows' | 'unknown'
  arch: string
  hasDocker: boolean
  hasPython: boolean
  pythonVersion: string
  channel: 'production' | 'test' | 'dev'
}

type OpenPAInstallerRunPayload = {
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

type OpenPAInstallerLog = { stream: 'stdout' | 'stderr' | 'info'; line: string }

type OpenPAInstallerDone = { exitCode: number; error?: string }

type OpenPAInstallerBridge = {
  detect: () => Promise<OpenPAInstallerEnvironment>
  run: (payload: OpenPAInstallerRunPayload) => Promise<{ exitCode: number }>
  cancel: () => Promise<boolean>
  onLog: (cb: (entry: OpenPAInstallerLog) => void) => void
  offLog: (cb: (entry: OpenPAInstallerLog) => void) => void
  onDone: (cb: (result: OpenPAInstallerDone) => void) => void
  offDone: (cb: (result: OpenPAInstallerDone) => void) => void
}

// Status events emitted by electron-updater. We mirror the underlying
// event names verbatim so the renderer doesn't have to reinterpret
// what each phase means; ``status`` is what to render.
type OpenPAUpdaterStatus = {
  status: 'unavailable' | 'checking' | 'available' | 'up_to_date' |
          'downloading' | 'ready' | 'error'
  info?: { version?: string; releaseName?: string; releaseNotes?: string }
  progress?: { percent?: number; transferred?: number; total?: number; bytesPerSecond?: number }
  error?: string
}

type OpenPAUpdaterBridge = {
  check: () => Promise<OpenPAUpdaterStatus>
  download: () => Promise<{ ok: boolean; error?: string }>
  install: () => Promise<{ ok: boolean; error?: string }>
  onStatus: (cb: (payload: OpenPAUpdaterStatus) => void) => void
  offStatus: (cb: (payload: OpenPAUpdaterStatus) => void) => void
}

type OpenPAServerBridge = {
  // Spawn ``openpa serve`` (idempotent — no-op when something already
  // answers /health). Returns ``{ ok: true }`` once the backend's
  // health endpoint is reachable, or ``{ ok: false, error }`` if it
  // failed to start.
  start: () => Promise<{ ok: boolean; error?: string }>
}

// In-app ``openpa upgrade`` flow. ``apply`` spawns the CLI under the
// main process; ``onLog`` streams every line it prints; ``onStatus``
// emits coarse phase transitions; ``onDone`` carries the terminal
// result. ``apply`` itself resolves with the same final shape, so
// callers can await it as a one-shot without subscribing to events.
type OpenPABackendUpgradePhase = 'starting' | 'upgrading' | 'restarting'
type OpenPABackendUpgradeStatus = { phase: OpenPABackendUpgradePhase }
type OpenPABackendUpgradeLog = { stream: 'stdout' | 'stderr' | 'info'; line: string }
type OpenPABackendUpgradeDone = { exitCode: number; ok: boolean; error?: string }

type OpenPABackendUpgradeBridge = {
  apply: () => Promise<OpenPABackendUpgradeDone>
  onStatus: (cb: (payload: OpenPABackendUpgradeStatus) => void) => void
  offStatus: (cb: (payload: OpenPABackendUpgradeStatus) => void) => void
  onLog: (cb: (entry: OpenPABackendUpgradeLog) => void) => void
  offLog: (cb: (entry: OpenPABackendUpgradeLog) => void) => void
  onDone: (cb: (result: OpenPABackendUpgradeDone) => void) => void
  offDone: (cb: (result: OpenPABackendUpgradeDone) => void) => void
}

type OpenPABridge = {
  config: {
    agentUrl: string
    deploymentType: 'local' | 'server' | 'custom' | ''
    autoUpdate: boolean
    channel: 'stable' | 'beta' | 'dev'
  }
  getConfig: () => Promise<OpenPABridge['config']>
  setConfig: (
    patch: Partial<OpenPABridge['config']>,
  ) => Promise<OpenPABridge['config']>
  installer: OpenPAInstallerBridge
  server: OpenPAServerBridge
  updater: OpenPAUpdaterBridge
  backendUpgrade: OpenPABackendUpgradeBridge
}

interface Window {
  openpa?: OpenPABridge
}
