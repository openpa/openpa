<script setup lang="ts">
/**
 * Update banner — shows the user when either side of the system has a
 * pending upgrade. Two independent surfaces feed it:
 *
 *   1. Backend     — polls ``GET /api/upgrade/check`` once on mount and
 *                    every six hours. Surfaces "Update available" with
 *                    the ``opa upgrade -y`` command, or "incompatible
 *                    UI" when the backend's ``min_compatible_ui`` is
 *                    higher than the running build.
 *   2. Electron app — listens to the ``window.openpa.updater`` bridge
 *                    for download progress and "ready to install"
 *                    events. Triggers ``install`` on the user's click.
 *
 * Mounted globally from App.vue so it's visible on every route except
 * the installer (which has its own progress UI).
 *
 * Web build: ``window.openpa`` is undefined, so the Electron-app branch
 * is silently skipped. The backend banner still works via fetch.
 */

import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSettingsStore } from '../stores/settings'
import type {
  BackendStatus,
  BackendUpgradeDone,
  BackendUpgradeLog,
  BackendUpgradePhase,
  BackendUpgradeStatus,
  UpdaterStatus,
} from '../types/updates'

const settingsStore = useSettingsStore()
const route = useRoute()
const router = useRouter()

// ── Backend update state ─────────────────────────────────────────────────

const backend = ref<BackendStatus>({ status: 'unknown' })
const dismissedBackend = ref(false)

const APP_VERSION = (import.meta as any).env?.PACKAGE_VERSION as string | undefined
// We compare ``min_compatible_ui`` against this. ``PACKAGE_VERSION`` is
// emitted into the build at compile time; if it's missing (dev / web),
// we just skip the compatibility check rather than guessing.

async function checkBackend() {
  if (!settingsStore.agentUrl) return
  try {
    const r = await fetch(`${settingsStore.agentUrl}/api/upgrade/check`)
    if (!r.ok) {
      backend.value = { status: 'error', reason: `HTTP ${r.status}` }
      return
    }
    const data = await r.json()
    backend.value = data
  } catch (err) {
    // Don't surface transient network failures; the next poll will
    // reset state. The banner stays in whatever it was last showing.
  }
}

let backendTimer: ReturnType<typeof setInterval> | null = null

// ── Electron updater state ───────────────────────────────────────────────

const updater = ref<UpdaterStatus>({ status: 'unavailable' })
const installing = ref(false)

function onUpdaterStatus(p: UpdaterStatus) {
  updater.value = p
}

async function downloadUiUpdate() {
  if (!window.openpa) return
  await window.openpa.updater.download()
}

async function installUiUpdate() {
  if (!window.openpa) return
  installing.value = true
  await window.openpa.updater.install()
  // The app will quit immediately after; nothing else to do here.
}

// ── In-app backend upgrade (Electron only) ───────────────────────────────
//
// Available iff ``window.openpa.backendUpgrade`` exists — i.e. running
// under an Electron shell new enough to carry the IPC handler. Web /
// browser users see the "copy this command" banner unchanged.

const upgradeAvailable = computed(() => !!window.openpa?.backendUpgrade)

const upgradeOpen = ref(false)
const upgradePhase = ref<BackendUpgradePhase | 'idle' | 'done' | 'failed'>('idle')
const upgradeLog = ref<string[]>([])
const upgradeError = ref<string | null>(null)
const logTailEl = ref<HTMLElement | null>(null)

const MAX_LOG_LINES = 500  // cap the rendered tail; the runner also writes ~/.openpa/upgrade.log

function onUpgradeStatus(p: BackendUpgradeStatus) {
  upgradePhase.value = p.phase
}

function onUpgradeLog(entry: BackendUpgradeLog) {
  upgradeLog.value.push(entry.line)
  if (upgradeLog.value.length > MAX_LOG_LINES) {
    upgradeLog.value.splice(0, upgradeLog.value.length - MAX_LOG_LINES)
  }
  // Auto-scroll the log view to the bottom on each new line so the
  // user sees live progress without having to manually scroll.
  nextTick(() => {
    const el = logTailEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function onUpgradeDone(result: BackendUpgradeDone) {
  if (result.ok) {
    upgradePhase.value = 'done'
    upgradeError.value = null
    // Refresh the backend status so the banner clears once the new
    // version reports itself.
    checkBackend()
  } else {
    upgradePhase.value = 'failed'
    upgradeError.value = result.error ?? `upgrade exited with code ${result.exitCode}`
  }
}

async function applyBackendUpgrade() {
  if (!window.openpa?.backendUpgrade) return
  upgradeOpen.value = true
  upgradePhase.value = 'starting'
  upgradeLog.value = []
  upgradeError.value = null
  // The promise resolves with the same payload onDone receives; we
  // subscribe to the live stream for the in-flight UI and rely on the
  // promise only as a backstop (e.g. if the IPC handler throws before
  // emitting any event).
  try {
    await window.openpa.backendUpgrade.apply()
  } catch (err) {
    // Main throws synchronously (e.g. "an upgrade is already running")
    // before any event is emitted, so the done handler hasn't run yet
    // — set the terminal state directly here.
    upgradePhase.value = 'failed'
    upgradeError.value = String(err)
  }
}

function closeUpgradeModal() {
  // Only allow closing in a terminal state; during the upgrade the
  // backend may be mid-restart and quitting the modal mid-flight would
  // strand the user with no banner and no way back into the log view.
  if (upgradePhase.value === 'done' || upgradePhase.value === 'failed') {
    upgradeOpen.value = false
  }
}

const upgradeInFlight = computed(() => {
  return upgradePhase.value === 'starting'
      || upgradePhase.value === 'upgrading'
      || upgradePhase.value === 'restarting'
})

const upgradePhaseLabel = computed(() => {
  switch (upgradePhase.value) {
    case 'starting':   return 'Starting upgrade…'
    case 'upgrading':  return 'Installing new version and migrating database…'
    case 'restarting': return 'Restarting backend…'
    case 'done':       return 'Upgrade complete.'
    case 'failed':     return 'Upgrade failed.'
    default:           return ''
  }
})

// ── Lifecycle ────────────────────────────────────────────────────────────

onMounted(() => {
  checkBackend()
  // Six-hour cadence is enough — releases don't ship that often, and
  // the user can always click "Check for updates" in Settings → Updates
  // to force a recheck. The poll is cheap (one HTTP GET) so we don't
  // gate on visibility.
  backendTimer = setInterval(checkBackend, 6 * 60 * 60 * 1000)

  if (window.openpa?.updater) {
    window.openpa.updater.onStatus(onUpdaterStatus)
  }
  if (window.openpa?.backendUpgrade) {
    window.openpa.backendUpgrade.onStatus(onUpgradeStatus)
    window.openpa.backendUpgrade.onLog(onUpgradeLog)
    window.openpa.backendUpgrade.onDone(onUpgradeDone)
  }
})

onUnmounted(() => {
  if (backendTimer) clearInterval(backendTimer)
  window.openpa?.updater.offStatus(onUpdaterStatus)
  if (window.openpa?.backendUpgrade) {
    window.openpa.backendUpgrade.offStatus(onUpgradeStatus)
    window.openpa.backendUpgrade.offLog(onUpgradeLog)
    window.openpa.backendUpgrade.offDone(onUpgradeDone)
  }
})

// ── Visibility computed ──────────────────────────────────────────────────

const showBackend = computed(() => {
  if (dismissedBackend.value) return false
  return backend.value.status === 'available' || backend.value.status === 'too_old'
})

const showUpdater = computed(() => {
  return updater.value.status === 'downloading'
      || updater.value.status === 'ready'
      || updater.value.status === 'error'
})

// Helper to print the apply command in the banner without TypeScript
// narrowing pain; the template guards on status === 'available' first.
function applyCommand(): string {
  return backend.value.status === 'available'
    ? backend.value.apply_command
    : 'opa upgrade -y'
}

function copyCommand() {
  navigator.clipboard?.writeText(applyCommand()).catch(() => {})
}

// "Manage in Settings → Updates" link. Routes are profile-scoped, so we
// can only offer it when the current route has a ``:profile`` param — on
// /setup, /login, and the profile selector there's nothing to link to.
const settingsHref = computed(() => {
  const profile = route.params.profile
  if (typeof profile !== 'string' || !profile) return ''
  return `/${profile}/settings/updates`
})

function openSettings() {
  if (settingsHref.value) router.push(settingsHref.value)
}

// Reference APP_VERSION so the linter keeps the import; future work
// will use it for the min_compatible_ui banner.
void APP_VERSION
</script>

<template>
  <!-- In-app upgrade modal. Lives at the root so it stays visible even
       after the backend banner clears (e.g. when the upgrade reaches
       the 'done' phase and the new version is reported). -->
  <Teleport to="body">
    <div
      v-if="upgradeOpen"
      class="upgrade-modal-overlay"
      @click.self="closeUpgradeModal"
    >
      <div class="upgrade-modal" role="dialog" aria-modal="true">
        <header class="upgrade-modal-header">
          <strong>Upgrading OpenPA backend</strong>
          <button
            class="dismiss"
            :disabled="upgradeInFlight"
            :title="upgradeInFlight ? 'Upgrade in progress — please wait' : 'Close'"
            @click="closeUpgradeModal"
          >×</button>
        </header>

        <div class="upgrade-modal-phase" :class="{ failed: upgradePhase === 'failed', done: upgradePhase === 'done' }">
          <span v-if="upgradeInFlight" class="spinner" aria-hidden="true" />
          {{ upgradePhaseLabel }}
        </div>

        <div v-if="upgradeInFlight" class="upgrade-modal-warning">
          Do not close OpenPA while the upgrade is in progress.
        </div>

        <pre ref="logTailEl" class="upgrade-modal-log">{{ upgradeLog.join('\n') }}</pre>

        <div v-if="upgradeError" class="upgrade-modal-error">
          {{ upgradeError }}
        </div>

        <footer class="upgrade-modal-footer">
          <button
            class="link"
            :disabled="upgradeInFlight"
            @click="closeUpgradeModal"
          >
            Close
          </button>
        </footer>
      </div>
    </div>
  </Teleport>

  <div v-if="showBackend || showUpdater" class="update-banner-stack">
    <!-- Backend update available -->
    <div
      v-if="backend.status === 'available' && !dismissedBackend"
      class="update-banner backend"
    >
      <span class="dot" />
      <div class="text">
        <strong>OpenPA backend update available</strong>
        — {{ backend.current }} → {{ backend.latest }}.
        <template v-if="upgradeAvailable">
          Apply it from inside the app, or run
        </template>
        <template v-else>
          Run
        </template>
        <code>{{ applyCommand() }}</code>
        from the machine that's running OpenPA.
      </div>
      <button
        v-if="upgradeAvailable"
        class="link primary"
        :disabled="upgradeInFlight"
        @click="applyBackendUpgrade"
      >
        Apply now
      </button>
      <button class="link" @click="copyCommand">Copy</button>
      <a class="link" :href="backend.release_url" target="_blank" rel="noopener">Notes</a>
      <button v-if="settingsHref" class="link" @click="openSettings">Manage</button>
      <button class="dismiss" @click="dismissedBackend = true" title="Dismiss">×</button>
    </div>

    <!-- Backend too old to upgrade in place -->
    <div
      v-if="backend.status === 'too_old' && !dismissedBackend"
      class="update-banner blocked"
    >
      <span class="dot" />
      <div class="text">
        <strong>Backend is too old to upgrade in place.</strong>
        The latest release ({{ backend.latest }}) requires at least
        v{{ backend.min_supported_upgrade_from }}; this install is
        v{{ backend.current }}. See release notes for the legacy export
        path.
      </div>
      <a class="link" :href="backend.release_url" target="_blank" rel="noopener">Notes</a>
      <button v-if="settingsHref" class="link" @click="openSettings">Manage</button>
      <button class="dismiss" @click="dismissedBackend = true" title="Dismiss">×</button>
    </div>

    <!-- Electron app downloading -->
    <div v-if="updater.status === 'downloading'" class="update-banner ui">
      <span class="dot" />
      <div class="text">
        Downloading OpenPA UI update…
        <span v-if="updater.progress?.percent != null">
          {{ Math.round(updater.progress.percent) }}%
        </span>
      </div>
    </div>

    <!-- Electron app ready to install -->
    <div v-if="updater.status === 'ready'" class="update-banner ui">
      <span class="dot" />
      <div class="text">
        <strong>OpenPA UI update ready</strong>
        <span v-if="updater.info?.version"> ({{ updater.info.version }})</span>.
        Restart now to install.
      </div>
      <button class="link" :disabled="installing" @click="installUiUpdate">Restart now</button>
    </div>

    <!-- Electron app updater error -->
    <div v-if="updater.status === 'error'" class="update-banner blocked">
      <span class="dot" />
      <div class="text">
        UI update failed: {{ updater.error }}.
      </div>
    </div>

    <!-- Helper: the user dismissed the backend banner but a download
         hasn't started; offer the explicit "download" button. -->
    <div
      v-if="updater.status === 'available'"
      class="update-banner ui"
    >
      <span class="dot" />
      <div class="text">
        OpenPA UI update available.
      </div>
      <button class="link" @click="downloadUiUpdate">Download</button>
    </div>
  </div>

  <!-- In-app backend upgrade modal — Electron only. Mounted to <body>
       so the overlay covers the whole viewport (including any modal
       opened by another component). Stays blocking while the runner is
       in flight so the user can't dismiss it mid-restart. -->
  <Teleport to="body">
    <div v-if="upgradeOpen" class="upgrade-modal-overlay">
      <div class="upgrade-modal" role="dialog" aria-modal="true">
        <header class="upgrade-modal-header">
          <strong>Applying OpenPA backend update</strong>
          <button
            class="dismiss"
            :disabled="upgradeInFlight"
            title="Close"
            @click="closeUpgradeModal"
          >×</button>
        </header>

        <div class="upgrade-modal-phase" :class="upgradePhase">
          <span v-if="upgradeInFlight" class="spinner" />
          <span v-else-if="upgradePhase === 'done'">✓</span>
          <span v-else-if="upgradePhase === 'failed'">✕</span>
          <span>{{ upgradePhaseLabel }}</span>
        </div>

        <div v-if="upgradeInFlight" class="upgrade-modal-warning">
          Don't close OpenPA while the upgrade is running. If it fails,
          the previous version is restored automatically.
        </div>

        <div v-if="upgradeError" class="upgrade-modal-error">
          {{ upgradeError }}
        </div>

        <div ref="logTailEl" class="upgrade-modal-log">
          <div v-if="upgradeLog.length === 0">Waiting for output…</div>
          <div v-for="(line, i) in upgradeLog" :key="i">{{ line }}</div>
        </div>

        <footer class="upgrade-modal-footer">
          <button
            class="link"
            :disabled="upgradeInFlight"
            @click="closeUpgradeModal"
          >
            Close
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.update-banner-stack {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1900;  /* below the titlebar (1999) so it doesn't overlap */
  display: flex;
  flex-direction: column;
  pointer-events: none;
}
.update-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  font-size: 13px;
  pointer-events: auto;
  border-bottom: 1px solid rgba(0, 0, 0, 0.08);
}
.update-banner.backend {
  background: #1f4d8a;
  color: #fff;
}
.update-banner.ui {
  background: #285c3d;
  color: #fff;
}
.update-banner.blocked {
  background: #8b2929;
  color: #fff;
}
.update-banner .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.update-banner .text {
  flex: 1;
  line-height: 1.4;
}
.update-banner code {
  background: rgba(255, 255, 255, 0.18);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.update-banner .link {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.4);
  color: inherit;
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  text-decoration: none;
}
.update-banner .link:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
}
.update-banner .link:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.update-banner .dismiss {
  background: transparent;
  border: 0;
  color: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.update-banner .link.primary {
  background: rgba(255, 255, 255, 0.95);
  color: #1f4d8a;
  border-color: transparent;
  font-weight: 600;
}
.update-banner .link.primary:hover:not(:disabled) {
  background: #fff;
}

/* ── Upgrade modal ──────────────────────────────────────────────────── */
.upgrade-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.upgrade-modal {
  background: #1e1e1e;
  color: #eaeaea;
  border: 1px solid #333;
  border-radius: 6px;
  width: min(720px, 90vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}
.upgrade-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid #333;
}
.upgrade-modal-header .dismiss {
  background: transparent;
  border: 0;
  color: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.upgrade-modal-header .dismiss:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.upgrade-modal-phase {
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #bbb;
}
.upgrade-modal-phase.done {
  color: #6ec76e;
}
.upgrade-modal-phase.failed {
  color: #e08080;
}
.upgrade-modal-warning {
  margin: 0 16px 8px;
  padding: 6px 10px;
  font-size: 12px;
  color: #f0c46c;
  background: rgba(240, 196, 108, 0.08);
  border: 1px solid rgba(240, 196, 108, 0.3);
  border-radius: 4px;
}
.upgrade-modal-log {
  margin: 0 16px;
  padding: 10px;
  flex: 1;
  overflow-y: auto;
  background: #0e0e0e;
  border: 1px solid #2a2a2a;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
  min-height: 200px;
  max-height: 50vh;
}
.upgrade-modal-error {
  margin: 10px 16px 0;
  padding: 8px 10px;
  background: rgba(224, 128, 128, 0.1);
  border: 1px solid rgba(224, 128, 128, 0.4);
  color: #e08080;
  border-radius: 4px;
  font-size: 12px;
}
.upgrade-modal-footer {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px;
  border-top: 1px solid #333;
}
.upgrade-modal-footer .link {
  background: transparent;
  border: 1px solid #555;
  color: inherit;
  font-size: 13px;
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
}
.upgrade-modal-footer .link:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.06);
}
.upgrade-modal-footer .link:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid rgba(255, 255, 255, 0.2);
  border-top-color: #6ec76e;
  border-radius: 50%;
  animation: upgrade-spin 0.9s linear infinite;
  display: inline-block;
}
@keyframes upgrade-spin {
  to { transform: rotate(360deg); }
}
</style>
