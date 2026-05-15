<script setup lang="ts">
/**
 * Updates settings page — manual "Check for updates", auto-update toggle,
 * release channel picker, and a status panel showing both the backend
 * (Python server) and the desktop app (Electron / electron-updater).
 *
 * Both update tracks are independent:
 *   - Backend updates come via the ``/api/upgrade/check`` endpoint which
 *     polls GitHub Releases. Applying the upgrade is intentionally still a
 *     CLI operation (``openpa upgrade -y``) — we surface the exact command
 *     and a Copy button rather than running it from a long-lived HTTP
 *     server. See ``app/api/upgrade.py``.
 *   - Desktop-app updates come via ``electron-updater`` and are driven by
 *     the ``window.openpa.updater`` IPC bridge. Auto-check on startup is
 *     gated by ``runtimeConfig.autoUpdate``; the channel (stable/beta/dev)
 *     is read once at boot, so changing it requires an app restart.
 *
 * Under the web build (``window.openpa`` is undefined) only the backend
 * panel is interactive; the desktop-app preferences are grayed out.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElMessage, ElSwitch, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import type { BackendStatus, UpdaterStatus } from '../types/updates';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const APP_VERSION =
  typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '';
const isElectron = computed(() => typeof window !== 'undefined' && !!window.openpa);

// ── Backend update state ─────────────────────────────────────────────────

const backend = ref<BackendStatus>({ status: 'unknown' });

async function checkBackend(): Promise<void> {
  if (!settingsStore.agentUrl) {
    backend.value = { status: 'error', reason: 'No agent URL configured' };
    return;
  }
  try {
    const r = await fetch(`${settingsStore.agentUrl}/api/upgrade/check`);
    if (!r.ok) {
      backend.value = { status: 'error', reason: `HTTP ${r.status}` };
      return;
    }
    backend.value = await r.json();
  } catch (err) {
    backend.value = {
      status: 'error',
      reason: err instanceof Error ? err.message : 'Network error',
    };
  }
}

// ── Desktop-app updater state ────────────────────────────────────────────

const updater = ref<UpdaterStatus>({ status: 'unavailable' });
const installing = ref(false);

function onUpdaterStatus(payload: UpdaterStatus) {
  updater.value = payload;
}

async function checkUpdater(): Promise<void> {
  if (!window.openpa) return;
  try {
    const result = await window.openpa.updater.check();
    // The main process emits status events on its own; ``check`` returns
    // the immediate result, which is mostly useful for unit tests. Trust
    // the event stream for the live status.
    if (result) onUpdaterStatus(result);
  } catch (err) {
    onUpdaterStatus({
      status: 'error',
      error: err instanceof Error ? err.message : 'Updater error',
    });
  }
}

async function downloadUpdater() {
  if (!window.openpa) return;
  try {
    await window.openpa.updater.download();
  } catch (err) {
    ElMessage.error(
      `Couldn't start download: ${err instanceof Error ? err.message : 'unknown'}`,
    );
  }
}

async function installUpdater() {
  if (!window.openpa) return;
  installing.value = true;
  try {
    await window.openpa.updater.install();
  } catch (err) {
    installing.value = false;
    ElMessage.error(
      `Install failed: ${err instanceof Error ? err.message : 'unknown'}`,
    );
  }
}

// ── Manual "Check for updates" ───────────────────────────────────────────

const checking = ref(false);
const lastCheckedAt = ref<Date | null>(null);

async function checkAll() {
  checking.value = true;
  try {
    await Promise.all([checkBackend(), isElectron.value ? checkUpdater() : Promise.resolve()]);
    lastCheckedAt.value = new Date();
  } finally {
    checking.value = false;
  }
}

// "5 seconds ago" / "3 minutes ago" — re-derived from ``lastCheckedAt``
// every 10 seconds so the label stays roughly fresh without a re-render
// per second.
const now = ref(Date.now());
let nowTimer: ReturnType<typeof setInterval> | null = null;

const lastCheckedLabel = computed(() => {
  if (!lastCheckedAt.value) return 'Not checked yet';
  const seconds = Math.max(0, Math.floor((now.value - lastCheckedAt.value.getTime()) / 1000));
  if (seconds < 5) return 'Just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return lastCheckedAt.value.toLocaleString();
});

// ── Preferences (Electron only) ──────────────────────────────────────────

const autoUpdate = computed({
  get: () => settingsStore.autoUpdate,
  set: (value: boolean) => {
    settingsStore.setAutoUpdate(value);
  },
});

// The release channel is fixed at install time and is not user-switchable.
// We display it read-only so support can identify which feed the install is
// polling, but offer no UI to change it — that requires re-running the
// installer. See ``app/upgrade/channel.py`` for the env-var contract.
const backendChannelLabel = computed(() => {
  const b = backend.value;
  if ('channel' in b && b.channel) return b.channel;
  return 'unknown';
});

// ── Status labels / colors ───────────────────────────────────────────────

type BadgeTone = 'success' | 'info' | 'warning' | 'danger';

const backendBadge = computed<{ label: string; tone: BadgeTone }>(() => {
  switch (backend.value.status) {
    case 'up_to_date': return { label: 'Up to date', tone: 'success' };
    case 'available':  return { label: `Update available: v${backend.value.latest}`, tone: 'warning' };
    case 'too_old':    return { label: 'Too old to upgrade in place', tone: 'danger' };
    case 'unreachable': return { label: "Couldn't reach release server", tone: 'info' };
    case 'unavailable': return { label: 'Upgrade module unavailable', tone: 'info' };
    case 'error':      return { label: `Error: ${backend.value.reason}`, tone: 'danger' };
    default:           return { label: 'Not checked yet', tone: 'info' };
  }
});

const updaterBadge = computed<{ label: string; tone: BadgeTone }>(() => {
  if (!isElectron.value) {
    return { label: 'Desktop app only', tone: 'info' };
  }
  switch (updater.value.status) {
    case 'up_to_date':  return { label: 'Up to date', tone: 'success' };
    case 'available':   return { label: 'Update available', tone: 'warning' };
    case 'checking':    return { label: 'Checking…', tone: 'info' };
    case 'downloading': {
      const pct = updater.value.progress?.percent;
      return { label: pct != null ? `Downloading ${Math.round(pct)}%` : 'Downloading…', tone: 'info' };
    }
    case 'ready':       return { label: 'Ready to install', tone: 'warning' };
    case 'error':       return { label: `Error: ${updater.value.error ?? 'unknown'}`, tone: 'danger' };
    case 'unavailable': return { label: 'Not checked yet', tone: 'info' };
    default:            return { label: 'Not checked yet', tone: 'info' };
  }
});

// ── Backend detail helpers ───────────────────────────────────────────────

const applyCommand = computed(() =>
  backend.value.status === 'available' ? backend.value.apply_command : 'openpa upgrade -y',
);

function copyApplyCommand() {
  navigator.clipboard
    ?.writeText(applyCommand.value)
    .then(() => ElMessage.success('Command copied'))
    .catch(() => ElMessage.warning('Copy failed — select and copy manually'));
}

// ── Lifecycle ────────────────────────────────────────────────────────────

onMounted(() => {
  void checkAll();
  if (window.openpa?.updater) {
    window.openpa.updater.onStatus(onUpdaterStatus);
  }
  nowTimer = setInterval(() => { now.value = Date.now(); }, 10_000);
});

onUnmounted(() => {
  if (window.openpa?.updater) {
    window.openpa.updater.offStatus(onUpdaterStatus);
  }
  if (nowTimer) clearInterval(nowTimer);
});

function goBack() {
  router.push(`/${props.profile}/settings`);
}
</script>

<template>
  <div class="updates-page">
    <div class="updates-container">
      <div class="updates-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <h1 class="updates-title">Updates</h1>
        <p class="updates-subtitle">
          Check for new versions of OpenPA and choose how the desktop app updates itself.
        </p>
      </div>

      <!-- ── Status panel ───────────────────────────────────────────── -->
      <ElCard class="section-card" shadow="never">
        <div class="status-row">
          <div class="status-line">
            <Icon icon="mdi:server" class="status-icon" />
            <div class="status-text">
              <div class="status-title">OpenPA backend</div>
              <div class="status-meta" v-if="'current' in backend && backend.current">
                v{{ backend.current }}
              </div>
            </div>
            <ElTag :type="backendBadge.tone" effect="light" round>
              {{ backendBadge.label }}
            </ElTag>
          </div>

          <div class="status-line">
            <Icon icon="mdi:monitor" class="status-icon" />
            <div class="status-text">
              <div class="status-title">Desktop app</div>
              <div class="status-meta">
                <span v-if="APP_VERSION">v{{ APP_VERSION }}</span>
                <span v-else>version unknown</span>
              </div>
            </div>
            <ElTag :type="updaterBadge.tone" effect="light" round>
              {{ updaterBadge.label }}
            </ElTag>
          </div>
        </div>

        <div class="check-row">
          <span class="last-checked">Last checked: {{ lastCheckedLabel }}</span>
          <ElButton
            type="primary"
            :loading="checking"
            @click="checkAll"
          >
            <Icon icon="mdi:refresh" />
            <span style="margin-left: 6px">Check for updates</span>
          </ElButton>
        </div>
      </ElCard>

      <!-- ── Backend update detail ──────────────────────────────────── -->
      <ElCard
        v-if="backend.status === 'available'"
        class="section-card detail-card"
        shadow="never"
      >
        <h3 class="section-title">Backend update available</h3>
        <p class="section-body">
          v{{ backend.current }} → <strong>v{{ backend.latest }}</strong>.
          Run the command below on the machine that's hosting OpenPA to apply it.
        </p>
        <div class="cmd-row">
          <code class="cmd">{{ applyCommand }}</code>
          <ElButton size="small" @click="copyApplyCommand">
            <Icon icon="mdi:content-copy" />
            <span style="margin-left: 4px">Copy</span>
          </ElButton>
        </div>
        <p class="section-hint">
          <a :href="backend.release_url" target="_blank" rel="noopener">View release notes</a>
        </p>
      </ElCard>

      <ElCard
        v-else-if="backend.status === 'too_old'"
        class="section-card detail-card blocked"
        shadow="never"
      >
        <h3 class="section-title">Backend is too old to upgrade in place</h3>
        <p class="section-body">
          The latest release (v{{ backend.latest }}) requires at least
          v{{ backend.min_supported_upgrade_from }}; this install is v{{ backend.current }}.
          See the release notes for the legacy export path.
        </p>
        <p class="section-hint">
          <a :href="backend.release_url" target="_blank" rel="noopener">View release notes</a>
        </p>
      </ElCard>

      <!-- ── Desktop app actions (only when there's something to do) ── -->
      <ElCard
        v-if="isElectron && (updater.status === 'available' || updater.status === 'ready')"
        class="section-card detail-card"
        shadow="never"
      >
        <h3 class="section-title">Desktop app update</h3>
        <p class="section-body" v-if="updater.status === 'available'">
          A new version of the OpenPA desktop app is available
          <span v-if="updater.info?.version">(v{{ updater.info.version }})</span>.
          Download it now to install on next restart.
        </p>
        <p class="section-body" v-if="updater.status === 'ready'">
          The OpenPA desktop app update
          <span v-if="updater.info?.version">(v{{ updater.info.version }})</span>
          is ready. Restart to install.
        </p>
        <div class="action-row">
          <ElButton
            v-if="updater.status === 'available'"
            type="primary"
            @click="downloadUpdater"
          >
            Download
          </ElButton>
          <ElButton
            v-if="updater.status === 'ready'"
            type="primary"
            :loading="installing"
            @click="installUpdater"
          >
            Restart and install
          </ElButton>
        </div>
      </ElCard>

      <!-- ── Preferences ───────────────────────────────────────────── -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Preferences</h3>

        <div class="pref-row" :class="{ disabled: !isElectron }">
          <div class="pref-info">
            <div class="pref-label">Automatically check for desktop-app updates</div>
            <div class="pref-hint">
              When on, the app checks for desktop-app updates each time it starts.
              When off, use "Check for updates" above to check manually.
            </div>
          </div>
          <ElSwitch
            v-model="autoUpdate"
            :disabled="!isElectron"
          />
        </div>

        <div class="pref-row info-only">
          <div class="pref-info">
            <div class="pref-label">Release channel</div>
            <div class="pref-hint">
              Set at install time and not user-switchable. To change channels,
              re-run the OpenPA installer on the host machine. Each channel
              only sees its own releases — for example, <strong>test</strong>
              installs never see <strong>production</strong> versions.
            </div>
          </div>
          <ElTag effect="plain">{{ backendChannelLabel }}</ElTag>
        </div>

        <p v-if="!isElectron" class="pref-disabled-note">
          Desktop-app preferences are available in the OpenPA desktop app only.
        </p>
      </ElCard>
    </div>
  </div>
</template>

<style scoped>
.updates-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.updates-container { max-width: 720px; margin: 0 auto; }
.updates-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.updates-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.updates-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }

.section-card { margin-bottom: 12px; background: var(--surface-color); }
.section-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.section-body { font-size: 0.875rem; color: var(--text-secondary); margin: 0 0 12px 0; line-height: 1.5; }
.section-hint { font-size: 0.8rem; color: var(--text-tertiary); margin: 8px 0 0 0; }
.section-hint a { color: var(--primary-color); }

.detail-card { border-left: 3px solid var(--el-color-warning); }
.detail-card.blocked { border-left-color: var(--el-color-danger); }

.status-row { display: flex; flex-direction: column; gap: 10px; }
.status-line {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 0;
}
.status-icon {
  font-size: 22px; color: var(--text-tertiary); flex-shrink: 0;
}
.status-text { flex: 1; min-width: 0; }
.status-title { font-size: 0.9rem; font-weight: 600; color: var(--text-primary); }
.status-meta { font-size: 0.8rem; color: var(--text-tertiary); margin-top: 2px; }

.check-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border-color);
  gap: 12px;
}
.last-checked { font-size: 0.8rem; color: var(--text-tertiary); }

.cmd-row {
  display: flex; align-items: center; gap: 8px;
  background: var(--hover-bg); padding: 8px 12px; border-radius: 6px;
}
.cmd {
  flex: 1; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.875rem; color: var(--text-primary);
  white-space: nowrap; overflow-x: auto;
}

.action-row { display: flex; gap: 8px; }

.pref-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border-color);
}
.pref-row:last-child { border-bottom: none; }
.pref-row.disabled .pref-label,
.pref-row.disabled .pref-hint { opacity: 0.55; }
.pref-info { flex: 1; min-width: 0; }
.pref-label {
  font-size: 0.9rem; font-weight: 500; color: var(--text-primary);
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.pref-hint { font-size: 0.8rem; color: var(--text-tertiary); margin-top: 4px; line-height: 1.4; }
.pref-disabled-note {
  font-size: 0.8rem; color: var(--text-tertiary); font-style: italic;
  margin: 12px 0 0 0;
}
.info-only { opacity: 0.9; }
</style>
