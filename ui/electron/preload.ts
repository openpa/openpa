import { ipcRenderer, contextBridge } from 'electron'

const listeners = new WeakMap<Function, (event: any, ...args: any[]) => void>()

// Snapshot the runtime config synchronously so the renderer can read the
// agent URL during module init (before any async IPC could resolve).
// `sendSync` is normally avoided, but here it's a one-shot call at preload
// time and it's the cleanest way to keep `window.openpa.config` populated
// from the very first line of renderer code.
let initialConfig: any = {}
try {
  initialConfig = ipcRenderer.sendSync('openpa:get-config-sync') ?? {}
} catch {
  initialConfig = {}
}

// Per-IPC log/done listener bookkeeping. We keep a parallel WeakMap so
// callers can pass plain functions to ``onLog`` / ``onDone`` and we wrap
// them with the (event, payload) → payload signature electron expects.
const installerListeners = new WeakMap<Function, (event: any, payload: any) => void>()

// Expose the OpenPA-specific bridge: a synchronous snapshot for module
// init, plus async getters/setters for live updates from the wizard.
contextBridge.exposeInMainWorld('openpa', {
  config: initialConfig,
  getConfig: () => ipcRenderer.invoke('openpa:get-config'),
  setConfig: (patch: Record<string, unknown>) =>
    ipcRenderer.invoke('openpa:set-config', patch),

  // First-run installer bridge — fronts the IPC handlers in
  // electron/main.ts. ``run`` returns a promise that resolves with the
  // installer's exit code; while it's pending, ``onLog`` callbacks
  // receive every line the script wrote to stdout/stderr.
  installer: {
    detect: () => ipcRenderer.invoke('openpa:installer:detect'),
    run: (payload: Record<string, unknown>) =>
      ipcRenderer.invoke('openpa:installer:run', payload),
    cancel: () => ipcRenderer.invoke('openpa:installer:cancel'),
    onLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('openpa:installer:log', wrapper)
    },
    offLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('openpa:installer:log', wrapper)
        installerListeners.delete(callback)
      }
    },
    onDone(callback: (result: { exitCode: number; error?: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('openpa:installer:done', wrapper)
    },
    offDone(callback: (result: { exitCode: number; error?: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('openpa:installer:done', wrapper)
        installerListeners.delete(callback)
      }
    },
  },

  // Backend lifecycle bridge. The renderer calls ``server.start()`` on
  // the Continue-to-Setup-Wizard click; the main process spawns
  // ``openpa serve`` and resolves once the health endpoint is reachable
  // (or rejects with an error string).
  server: {
    start: (): Promise<{ ok: boolean; error?: string }> =>
      ipcRenderer.invoke('openpa:server:start'),
  },

  // Backend upgrade bridge — runs ``openpa upgrade --yes`` as a child
  // process under main, streaming progress back so the renderer can
  // show a live log modal without sending the user to a terminal.
  // ``apply`` resolves when the child has exited AND the backend has
  // been restarted (on success). ``onStatus`` / ``onLog`` / ``onDone``
  // mirror the installer bridge's shape so consumers can reuse the
  // same subscribe/unsubscribe pattern.
  backendUpgrade: {
    apply: () => ipcRenderer.invoke('openpa:backend-upgrade:apply'),
    onStatus(callback: (payload: { phase: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('openpa:backend-upgrade:status', wrapper)
    },
    offStatus(callback: (payload: { phase: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('openpa:backend-upgrade:status', wrapper)
        installerListeners.delete(callback)
      }
    },
    onLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('openpa:backend-upgrade:log', wrapper)
    },
    offLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('openpa:backend-upgrade:log', wrapper)
        installerListeners.delete(callback)
      }
    },
    onDone(callback: (result: { exitCode: number; ok: boolean; error?: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('openpa:backend-upgrade:done', wrapper)
    },
    offDone(callback: (result: { exitCode: number; ok: boolean; error?: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('openpa:backend-upgrade:done', wrapper)
        installerListeners.delete(callback)
      }
    },
  },

  // Auto-updater bridge — fronts ``electron-updater`` running in the
  // main process. The renderer calls ``check``/``download``/``install``
  // explicitly; the user is always in control of when the new build
  // gets staged. ``onStatus`` receives the lifecycle events so the
  // banner can show "downloading 47%" without polling.
  updater: {
    check: () => ipcRenderer.invoke('openpa:updater:check'),
    download: () => ipcRenderer.invoke('openpa:updater:download'),
    install: () => ipcRenderer.invoke('openpa:updater:install'),
    onStatus(callback: (payload: any) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('openpa:updater:status', wrapper)
    },
    offStatus(callback: (payload: any) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('openpa:updater:status', wrapper)
        installerListeners.delete(callback)
      }
    },
  },
})

// --------- Expose some API to the Renderer process ---------
contextBridge.exposeInMainWorld('ipcRenderer', {
  on(...args: Parameters<typeof ipcRenderer.on>) {
    const [channel, listener] = args
    const wrapper = (event: any, ...args: any[]) => listener(event, ...args)
    listeners.set(listener, wrapper)
    return ipcRenderer.on(channel, wrapper)
  },
  off(...args: Parameters<typeof ipcRenderer.off>) {
    const [channel, listener] = args
    const wrapper = listeners.get(listener)
    if (wrapper) {
      listeners.delete(listener)
      return ipcRenderer.off(channel, wrapper)
    }
    return ipcRenderer
  },
  send(...args: Parameters<typeof ipcRenderer.send>) {
    const [channel, ...omit] = args
    return ipcRenderer.send(channel, ...omit)
  },
  invoke(...args: Parameters<typeof ipcRenderer.invoke>) {
    const [channel, ...omit] = args
    return ipcRenderer.invoke(channel, ...omit)
  },

  // You can expose other APTs you need here.
  // ...
})
