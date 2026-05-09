/// <reference types="vite/client" />

// Global flag for Electron environment detection (build-time define).
declare const __IS_ELECTRON__: boolean;

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
  recommendedMode: 'docker' | 'native'
}

type OpenPAInstallerRunPayload = {
  deployment: 'local' | 'server'
  appHost?: string
  mode: 'docker' | 'native'
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

type OpenPABridge = {
  config: {
    agentUrl: string
    deploymentType: 'local' | 'server' | ''
    autoUpdate: boolean
    channel: 'stable' | 'beta' | 'dev'
  }
  getConfig: () => Promise<OpenPABridge['config']>
  setConfig: (
    patch: Partial<OpenPABridge['config']>,
  ) => Promise<OpenPABridge['config']>
  installer: OpenPAInstallerBridge
  updater: OpenPAUpdaterBridge
}

interface Window {
  openpa?: OpenPABridge
}
