/**
 * Unified update composable.
 *
 * One source of truth for both the global UpdateBanner and the
 * Settings → Updates page. Consumers get a single ``state`` ref plus
 * an ``applyUpdate()`` action; they don't have to know whether the
 * pending change is a backend wheel, an Electron shell, or both.
 *
 * Two inputs feed the state:
 *
 *   1. Backend ``GET /api/upgrade/check``  — polled at mount + every 6h,
 *      and re-fetched on demand via ``checkNow()``.
 *   2. Electron ``window.openpa.updater``  — subscribed via the existing
 *      onStatus bridge. Undefined under the web build; the rest still
 *      works.
 *
 * Two upgrade tracks are coordinated:
 *
 *   - Backend upgrade: Electron uses the existing ``backendUpgrade``
 *     IPC (in-process under main, streams events). Web UI uses the new
 *     ``POST /api/upgrade/apply`` + SSE/poll on ``/api/upgrade/status``.
 *   - Shell upgrade: ``window.openpa.updater.download()`` runs in
 *     parallel when a shell update is pending. The user confirms with
 *     "Restart now" once both tracks have settled.
 *
 * The unified phase intentionally hides "which component changed" —
 * the user sees one "OpenPA vX → vY available" card. Internal `[shell]`
 * / `[backend]` tags on log lines preserve traceability for the curious
 * but don't surface in summary copy.
 */

import { computed, onUnmounted, ref, watch } from 'vue'

import { useSettingsStore } from '../stores/settings'
import type {
  BackendStatus,
  BackendUpgradeDone,
  BackendUpgradeLog,
  BackendUpgradeStatus,
  UnifiedUpdatePhase,
  UnifiedUpdateState,
  UpdaterStatus,
  UpgradeApplyResponse,
  UpgradeStatusFile,
} from '../types/updates'

const LOG_TAIL_MAX = 500
const BACKEND_POLL_MS = 6 * 60 * 60 * 1000  // 6h — same cadence as the old banner
const STATUS_POLL_MS = 1500                  // web-UI poll cadence during applying
const VERSION_POLL_MS = 30000                // /version poll cadence for auto-reload

// ── module-level singletons ──────────────────────────────────────────────
//
// The composable returns refs that survive component remounts because
// both UpdateBanner (mounted at App.vue) and UpdatesSettings (mounted on
// route change) need to observe the same upgrade. Without sharing
// state, opening Settings during an upgrade would reset the modal log
// to empty.

const backend = ref<BackendStatus>({ status: 'unknown' })
const updater = ref<UpdaterStatus>({ status: 'unavailable' })
const log = ref<string[]>([])
const phase = ref<UnifiedUpdatePhase>('idle')
const error = ref<string | null>(null)
const explicitRestartRequired = ref(false)

let initialised = false
let backendTimer: ReturnType<typeof setInterval> | null = null
let statusTimer: ReturnType<typeof setInterval> | null = null
let statusController: AbortController | null = null
let versionTimer: ReturnType<typeof setInterval> | null = null
let bootBackendVersion: string | null = null

function isElectron(): boolean {
  return typeof window !== 'undefined' && !!window.openpa
}

function pushLog(line: string): void {
  log.value.push(line)
  if (log.value.length > LOG_TAIL_MAX) {
    log.value.splice(0, log.value.length - LOG_TAIL_MAX)
  }
}

function setPhase(next: UnifiedUpdatePhase, errMsg: string | null = null): void {
  phase.value = next
  error.value = errMsg
}

// ── Backend /api/upgrade/check polling ───────────────────────────────────

async function checkBackend(): Promise<void> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) return
  try {
    const r = await fetch(`${settings.agentUrl}/api/upgrade/check`)
    if (!r.ok) {
      backend.value = { status: 'error', reason: `HTTP ${r.status}` }
      return
    }
    backend.value = await r.json()
  } catch {
    // Transient network failure — leave the previous state in place
    // so the banner doesn't flicker.
  }
}

// ── Electron updater bridge ──────────────────────────────────────────────

function onUpdaterStatus(s: UpdaterStatus): void {
  updater.value = s
  // Forward to the unified log when a shell upgrade is in flight, so
  // the user sees the download progress alongside the backend output.
  if (phase.value === 'applying') {
    if (s.status === 'downloading') {
      const pct = s.progress?.percent
      if (pct != null) pushLog(`[shell] Downloading… ${Math.round(pct)}%`)
    } else if (s.status === 'ready') {
      pushLog('[shell] Update downloaded — ready to install.')
      explicitRestartRequired.value = true
    } else if (s.status === 'error') {
      pushLog(`[shell] Update error: ${s.error ?? 'unknown'}`)
    }
  }
}

// ── Electron backend-upgrade IPC ─────────────────────────────────────────

function onElectronUpgradeStatus(p: BackendUpgradeStatus): void {
  pushLog(`[backend] ${labelForBackendPhase(p.phase)}`)
}

function onElectronUpgradeLog(entry: BackendUpgradeLog): void {
  pushLog(`[backend] ${entry.line}`)
}

function onElectronUpgradeDone(result: BackendUpgradeDone): void {
  if (result.ok) {
    // Don't flip to 'done' yet if a shell download is mid-flight —
    // reconcileTerminalPhase handles that.
    reconcileTerminalPhase({ backendOk: true })
  } else {
    setPhase(
      'failed',
      result.error ?? `Upgrade exited with code ${result.exitCode}`,
    )
  }
}

function labelForBackendPhase(p: string): string {
  switch (p) {
    case 'starting': return 'Starting upgrade…'
    case 'upgrading': return 'Installing new version and migrating database…'
    case 'restarting': return 'Restarting backend…'
    default: return p
  }
}

// ── Web-UI POST /apply + status poll ─────────────────────────────────────

async function applyWebUpgrade(): Promise<void> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) {
    setPhase('failed', 'No backend URL configured.')
    return
  }
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (settings.authToken) {
    headers['Authorization'] = `Bearer ${settings.authToken}`
  }
  let kicked: UpgradeApplyResponse
  try {
    const r = await fetch(`${settings.agentUrl}/api/upgrade/apply`, {
      method: 'POST', headers,
    })
    if (r.status === 409) {
      // Another upgrade is already running — quietly attach to it.
      pushLog('[backend] An upgrade is already running; attaching to it.')
    } else if (!r.ok) {
      const body = await r.json().catch(() => ({}))
      setPhase('failed', body.error ?? `HTTP ${r.status}`)
      return
    } else {
      kicked = (await r.json()) as UpgradeApplyResponse
      pushLog(`[backend] Spawned upgrade runner (pid ${kicked.pid}).`)
    }
  } catch (e) {
    setPhase('failed', e instanceof Error ? e.message : String(e))
    return
  }
  // Start polling the status file. We use polling rather than SSE
  // because the SSE stream dies when the backend restarts itself
  // mid-upgrade; the poll just retries until the new backend is up.
  startStatusPolling()
}

function startStatusPolling(): void {
  if (statusTimer) return
  let lastLogLen = log.value.length
  let backoff = STATUS_POLL_MS
  const tick = async () => {
    const settings = useSettingsStore()
    if (!settings.agentUrl) return
    const headers: Record<string, string> = {}
    if (settings.authToken) {
      headers['Authorization'] = `Bearer ${settings.authToken}`
    }
    try {
      statusController?.abort()
      statusController = new AbortController()
      const r = await fetch(`${settings.agentUrl}/api/upgrade/status`, {
        headers, signal: statusController.signal,
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      backoff = STATUS_POLL_MS  // success — reset
      const body = (await r.json()) as UpgradeStatusFile
      mergeStatusFile(body, lastLogLen)
      lastLogLen = log.value.length
      if (body.phase === 'done' || body.phase === 'failed') {
        stopStatusPolling()
        reconcileTerminalPhase({
          backendOk: body.phase === 'done',
          backendError: body.error,
        })
      }
    } catch {
      // Backend likely restarting — the runner kills the parent at the
      // end of the upgrade and the supervisor (Docker / Electron) takes
      // a few seconds to bring it back. Keep polling with backoff so we
      // re-attach when it comes back.
      backoff = Math.min(backoff * 1.5, 10000)
    }
  }
  statusTimer = setInterval(() => { void tick() }, backoff)
  void tick()
}

function stopStatusPolling(): void {
  if (statusTimer) clearInterval(statusTimer)
  statusTimer = null
  statusController?.abort()
  statusController = null
}

function mergeStatusFile(s: UpgradeStatusFile, knownLines: number): void {
  // The server already trims to LOG_TAIL_MAX; we want only the lines
  // we haven't shown yet. Comparing by index is fine because the tail
  // is append-only until trimmed, and the trim drops the *oldest*
  // entries — by the time that matters the renderer has them too.
  const incoming = s.log_tail ?? []
  // Tag every web-UI status line as [backend] so the combined log
  // visually distinguishes from [shell] download progress.
  for (let i = knownLines; i < incoming.length; i++) {
    pushLog(`[backend] ${incoming[i]}`)
  }
  // Refresh current/target version if the server learned them.
  if (s.current_version && (backend.value as any).current !== s.current_version) {
    backend.value = {
      ...(backend.value as any),
      current: s.current_version,
    }
  }
}

// ── Terminal-phase reconciliation ────────────────────────────────────────

type ReconcileArgs = { backendOk: boolean; backendError?: string | null }

/** Decide the final unified phase once a track finishes. */
function reconcileTerminalPhase(args: ReconcileArgs): void {
  if (!args.backendOk) {
    setPhase('failed', args.backendError ?? 'Upgrade failed.')
    return
  }
  // Backend succeeded. If a shell update was downloaded, ask the user
  // to restart for it (which will also pick up the new backend on boot).
  if (explicitRestartRequired.value || updater.value.status === 'ready') {
    setPhase('restart_required')
    return
  }
  setPhase('done')
  // After 'done' we re-check the backend so the banner clears the
  // moment the new version reports itself. ``done`` itself is a
  // transient state the renderer can use to show a success toast for
  // a couple of seconds before settling back to ``idle``.
  void checkBackend()
}

// ── Version polling for auto-reload ──────────────────────────────────────
//
// Every tab — Web UI or Electron renderer — polls ``/version`` and
// reloads itself if the ``backend`` field changes vs. the value seen
// at boot. This is what gives a browser tab the "auto-pick-up the new
// UI after an upgrade" behaviour the Electron main process already
// provides via ``win.reload()``. In Electron the two paths race
// harmlessly: whichever fires first reloads the window, the other
// becomes a no-op once the version matches again.

async function fetchBackendVersion(): Promise<string | null> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) return null
  try {
    const r = await fetch(`${settings.agentUrl}/version`)
    if (!r.ok) return null
    const body = (await r.json()) as { backend?: string }
    return typeof body.backend === 'string' ? body.backend : null
  } catch {
    return null
  }
}

function triggerPostUpdateReload(): void {
  try {
    sessionStorage.setItem('openpa:just_updated', String(Date.now()))
  } catch {
    // sessionStorage unavailable (private mode etc.) — reload anyway.
  }
  window.location.reload()
}

async function pollVersion(): Promise<void> {
  const current = await fetchBackendVersion()
  if (!current) return
  if (bootBackendVersion === null) {
    bootBackendVersion = current
    return
  }
  if (current !== bootBackendVersion) {
    triggerPostUpdateReload()
  }
}

// ── Initialisation (idempotent) ──────────────────────────────────────────

function init(): void {
  if (initialised) return
  initialised = true

  void checkBackend()
  backendTimer = setInterval(() => { void checkBackend() }, BACKEND_POLL_MS)

  // Seed the boot version, then poll on a fixed cadence.
  void pollVersion()
  versionTimer = setInterval(() => { void pollVersion() }, VERSION_POLL_MS)

  if (window.openpa?.updater) {
    window.openpa.updater.onStatus(onUpdaterStatus)
  }
  if (window.openpa?.backendUpgrade) {
    window.openpa.backendUpgrade.onStatus(onElectronUpgradeStatus)
    window.openpa.backendUpgrade.onLog(onElectronUpgradeLog)
    window.openpa.backendUpgrade.onDone(onElectronUpgradeDone)
  }
}

function teardown(): void {
  // Only tear down if no one is left; in practice we keep the
  // listeners alive for the lifetime of the SPA so the in-app state
  // doesn't reset when the user navigates between routes.
  if (backendTimer) {
    clearInterval(backendTimer)
    backendTimer = null
  }
  if (versionTimer) {
    clearInterval(versionTimer)
    versionTimer = null
  }
  bootBackendVersion = null
  stopStatusPolling()
  if (window.openpa?.updater) {
    window.openpa.updater.offStatus(onUpdaterStatus)
  }
  if (window.openpa?.backendUpgrade) {
    window.openpa.backendUpgrade.offStatus(onElectronUpgradeStatus)
    window.openpa.backendUpgrade.offLog(onElectronUpgradeLog)
    window.openpa.backendUpgrade.offDone(onElectronUpgradeDone)
  }
  initialised = false
}

// ── Public API ───────────────────────────────────────────────────────────

export function useUpdate() {
  init()

  // Cleanup on the LAST consumer unmount is hard to track without ref
  // counting; in practice the global UpdateBanner is mounted for the
  // entire session, so this onUnmounted is a no-op in real use. Tests
  // can invoke teardown() explicitly.
  onUnmounted(() => {
    /* deliberately no-op — see comment */
  })

  const state = computed<UnifiedUpdateState>(() => {
    const b = backend.value
    const u = updater.value

    // Compute "is there an update available?" from both inputs.
    const backendAvailable = b.status === 'available'
    const shellAvailable =
      u.status === 'available' || u.status === 'downloading' || u.status === 'ready'

    const blocked = b.status === 'too_old'

    // Display version: prefer whichever is newer/known. The pure-shell
    // case carries its version in updater.info.version; the backend
    // case in backend.latest. If both are present and differ, pick the
    // string-larger one (semver lexicographic works for our 0.X.Y
    // versions; refine later if we go past 9).
    let latestVersion: string | null = null
    if (backendAvailable && 'latest' in b) latestVersion = b.latest
    const shellVersion = u.info?.version ?? null
    if (shellVersion && (!latestVersion || shellVersion > latestVersion)) {
      latestVersion = shellVersion
    }

    const currentVersion: string | null =
      'current' in b && b.current ? b.current : null

    let computedPhase: UnifiedUpdatePhase = 'idle'
    let errMsg = error.value
    let releaseUrl: string | null = null
    if ('release_url' in b && b.release_url) releaseUrl = b.release_url

    if (phase.value === 'applying') {
      computedPhase = 'applying'
    } else if (phase.value === 'failed') {
      computedPhase = 'failed'
    } else if (phase.value === 'done') {
      computedPhase = 'done'
    } else if (phase.value === 'restart_required') {
      computedPhase = 'restart_required'
    } else if (blocked) {
      computedPhase = 'blocked'
      if (b.status === 'too_old') {
        errMsg =
          `Latest is v${b.latest} but it requires at least v${b.min_supported_upgrade_from}; ` +
          `this install is v${b.current}.`
      }
    } else if (backendAvailable || shellAvailable) {
      computedPhase = 'available'
    }

    const canApply =
      computedPhase === 'available' || computedPhase === 'failed'

    return {
      phase: computedPhase,
      latestVersion,
      currentVersion,
      canApply,
      log: log.value,
      phaseLabel: phaseLabelFor(computedPhase),
      error: errMsg,
      releaseUrl,
    }
  })

  // ── Actions ────────────────────────────────────────────────────────────

  async function checkNow(): Promise<void> {
    void checkBackend()
    if (isElectron() && window.openpa?.updater) {
      try {
        const s = await window.openpa.updater.check()
        if (s) onUpdaterStatus(s)
      } catch (e) {
        onUpdaterStatus({
          status: 'error',
          error: e instanceof Error ? e.message : 'updater error',
        })
      }
    }
  }

  /** Start the unified upgrade. Backend + shell tracks run in parallel. */
  async function applyUpdate(): Promise<void> {
    if (state.value.phase === 'applying') return
    log.value = []
    explicitRestartRequired.value = false
    setPhase('applying')
    pushLog('Starting upgrade…')

    // Shell track (Electron only): kick off the download in parallel.
    const u = updater.value
    if (isElectron() && (u.status === 'available' || u.status === 'downloading')) {
      pushLog('[shell] Requesting installer download…')
      try {
        void window.openpa!.updater.download()
      } catch (e) {
        pushLog(`[shell] Download failed to start: ${String(e)}`)
      }
    }

    // Backend track: pick whichever transport is available. Electron
    // IPC is preferred when present because it carries the post-upgrade
    // backend restart itself; the web-UI POST relies on the supervisor
    // (Docker / Electron) to relaunch.
    if (isElectron() && window.openpa?.backendUpgrade) {
      try {
        await window.openpa.backendUpgrade.apply()
        // onDone handler reconciles the terminal phase
      } catch (e) {
        setPhase('failed', e instanceof Error ? e.message : String(e))
      }
    } else {
      await applyWebUpgrade()
    }
  }

  /** Trigger ``electron-updater.quitAndInstall`` from the modal's footer. */
  async function applyShellRestart(): Promise<void> {
    if (!isElectron() || !window.openpa?.updater) return
    try {
      await window.openpa.updater.install()
    } catch (e) {
      setPhase('failed', e instanceof Error ? e.message : String(e))
    }
  }

  /** Clear a 'done' / 'failed' state so the modal can close. */
  function dismiss(): void {
    if (phase.value === 'done' || phase.value === 'failed') {
      setPhase('idle')
      log.value = []
      explicitRestartRequired.value = false
    }
  }

  return {
    state,
    isElectron,
    checkNow,
    applyUpdate,
    applyShellRestart,
    dismiss,
    // Exposed for tests / debugging only:
    _backend: backend,
    _updater: updater,
    _teardown: teardown,
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────

function phaseLabelFor(p: UnifiedUpdatePhase): string {
  switch (p) {
    case 'idle': return ''
    case 'available': return 'Update available'
    case 'applying': return 'Updating OpenPA…'
    case 'restart_required': return 'Restart to finish update'
    case 'done': return 'Update complete'
    case 'failed': return 'Update failed'
    case 'blocked': return 'Cannot upgrade in place'
  }
}

// ── Re-exports for callers that don't want to dip into ../types ──────────

export type {
  UnifiedUpdatePhase,
  UnifiedUpdateState,
  UpgradeStatusFile,
  UpgradeStatusPhase,
} from '../types/updates'

// Silence linter "watch unused" if we add a watch later; keep the import.
void watch
