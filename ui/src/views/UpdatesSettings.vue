<script setup lang="ts">
/**
 * Updates settings page — single unified status + Update button. Shell
 * and backend updates are coordinated through the ``useUpdate``
 * composable so this view shows exactly the same state as the global
 * UpdateBanner.
 *
 * Preferences (auto-update toggle, channel display) remain Electron-
 * only; under the web build they're grayed out.
 */

import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElSwitch, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import { useUpdate } from '../composables/useUpdate';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const APP_VERSION =
  typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '';

const { state, isElectron, checkNow, applyUpdate, applyShellRestart } = useUpdate();

// ── Manual "Check for updates" ───────────────────────────────────────────

const checking = ref(false);
const lastCheckedAt = ref<Date | null>(null);

async function doCheck() {
  checking.value = true;
  try {
    await checkNow();
    lastCheckedAt.value = new Date();
  } finally {
    checking.value = false;
  }
}

// "5 seconds ago" / "3 minutes ago" — re-derived from ``lastCheckedAt``
// every 10 seconds so the label stays roughly fresh without a re-render
// per second.
const now = ref(Date.now());

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

// Release channel is fixed at install time. We display it read-only.
const installChannel = computed<string>(() => {
  if (isElectron() && window.openpa?.config?.channel) {
    return window.openpa.config.channel;
  }
  return 'unknown';
});

// ── Status panel ─────────────────────────────────────────────────────────

type BadgeTone = 'success' | 'info' | 'warning' | 'danger';

const statusBadge = computed<{ label: string; tone: BadgeTone }>(() => {
  switch (state.value.phase) {
    case 'idle': return { label: 'Up to date', tone: 'success' };
    case 'available':
      return {
        label: state.value.latestVersion
          ? `Update available: v${state.value.latestVersion}`
          : 'Update available',
        tone: 'warning',
      };
    case 'applying': return { label: 'Updating…', tone: 'info' };
    case 'restart_required': return { label: 'Restart to finish', tone: 'warning' };
    case 'done': return { label: 'Updated', tone: 'success' };
    case 'failed': return { label: 'Update failed', tone: 'danger' };
    case 'blocked': return { label: 'Too old to upgrade', tone: 'danger' };
  }
});

const installedVersion = computed(() =>
  state.value.currentVersion ?? APP_VERSION ?? 'unknown',
);

function onPrimaryButton() {
  if (state.value.phase === 'restart_required') {
    void applyShellRestart();
  } else {
    void applyUpdate();
  }
}

const primaryButtonLabel = computed(() => {
  switch (state.value.phase) {
    case 'restart_required': return 'Restart now';
    case 'failed': return 'Retry';
    default: return 'Update now';
  }
});

// ── Lifecycle ────────────────────────────────────────────────────────────

onMounted(() => {
  void doCheck();
  setInterval(() => { now.value = Date.now(); }, 10_000);
});

function goBack() {
  router.push(`/${props.profile}`);
}
</script>

<template>
  <div class="updates-page">
    <div class="updates-container">
      <div class="updates-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Chat
        </button>
        <h1 class="updates-title">Updates</h1>
        <p class="updates-subtitle">
          Check for new versions of OpenPA and install them with one click.
        </p>
      </div>

      <!-- ── Status panel ───────────────────────────────────────────── -->
      <ElCard class="section-card" shadow="never">
        <div class="status-line">
          <Icon icon="mdi:package-variant-closed" class="status-icon" />
          <div class="status-text">
            <div class="status-title">OpenPA</div>
            <div class="status-meta">
              v{{ installedVersion }}
            </div>
          </div>
          <ElTag :type="statusBadge.tone" effect="light" round>
            {{ statusBadge.label }}
          </ElTag>
        </div>

        <div class="check-row">
          <span class="last-checked">Last checked: {{ lastCheckedLabel }}</span>
          <ElButton
            type="primary"
            :loading="checking"
            @click="doCheck"
          >
            <Icon icon="mdi:refresh" />
            <span style="margin-left: 6px">Check for updates</span>
          </ElButton>
        </div>
      </ElCard>

      <!-- ── Update available / action card ─────────────────────────── -->
      <ElCard
        v-if="state.phase === 'available' || state.phase === 'failed' || state.phase === 'restart_required'"
        class="section-card detail-card"
        shadow="never"
      >
        <h3 class="section-title">
          <template v-if="state.phase === 'available'">Update to v{{ state.latestVersion }}</template>
          <template v-else-if="state.phase === 'restart_required'">Restart to finish update</template>
          <template v-else>Update failed</template>
        </h3>
        <p class="section-body">
          <template v-if="state.phase === 'available' && state.currentVersion && state.latestVersion">
            Move OpenPA from v{{ state.currentVersion }} to v{{ state.latestVersion }}.
            Click <strong>Update now</strong> and OpenPA will install the new version,
            run migrations, and restart automatically. No commands needed.
          </template>
          <template v-else-if="state.phase === 'restart_required'">
            The downloaded update is staged. Click <strong>Restart now</strong>
            to apply it.
          </template>
          <template v-else>
            {{ state.error }}
          </template>
        </p>
        <div class="action-row">
          <ElButton type="primary" @click="onPrimaryButton">
            <Icon
              :icon="state.phase === 'restart_required' ? 'mdi:restart' : 'mdi:download'"
            />
            <span style="margin-left: 6px">{{ primaryButtonLabel }}</span>
          </ElButton>
          <a
            v-if="state.releaseUrl"
            :href="state.releaseUrl"
            target="_blank"
            rel="noopener"
            class="notes-link"
          >
            View release notes
          </a>
        </div>
      </ElCard>

      <!-- ── Blocked card (backend too old) ──────────────────────────── -->
      <ElCard
        v-else-if="state.phase === 'blocked'"
        class="section-card detail-card blocked"
        shadow="never"
      >
        <h3 class="section-title">Too old to upgrade in place</h3>
        <p class="section-body">
          {{ state.error }} See the release notes for the legacy export path.
        </p>
        <p class="section-hint" v-if="state.releaseUrl">
          <a :href="state.releaseUrl" target="_blank" rel="noopener">View release notes</a>
        </p>
      </ElCard>

      <!-- ── Preferences ───────────────────────────────────────────── -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Preferences</h3>

        <div class="pref-row" :class="{ disabled: !isElectron() }">
          <div class="pref-info">
            <div class="pref-label">Automatically check for updates</div>
            <div class="pref-hint">
              When on, the desktop app checks for updates each time it starts.
              When off, use "Check for updates" above to check manually.
            </div>
          </div>
          <ElSwitch
            v-model="autoUpdate"
            :disabled="!isElectron()"
          />
        </div>

        <div class="pref-row info-only">
          <div class="pref-info">
            <div class="pref-label">Release channel</div>
            <div class="pref-hint">
              Set at install time. Re-run the installer to switch channels.
              Each channel only sees its own releases.
            </div>
          </div>
          <ElTag effect="plain">{{ installChannel }}</ElTag>
        </div>

        <p v-if="!isElectron()" class="pref-disabled-note">
          The auto-update toggle only applies to the desktop app. Web UI
          users still get the one-click Update button above.
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

.action-row {
  display: flex; align-items: center; gap: 12px;
}
.notes-link {
  font-size: 0.8rem; color: var(--primary-color);
}

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
