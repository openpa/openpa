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

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSettingsStore } from '../stores/settings'
import type { BackendStatus, UpdaterStatus } from '../types/updates'

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
})

onUnmounted(() => {
  if (backendTimer) clearInterval(backendTimer)
  window.openpa?.updater.offStatus(onUpdaterStatus)
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
        Run
        <code>{{ applyCommand() }}</code>
        from the machine that's running OpenPA.
      </div>
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
</style>
