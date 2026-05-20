<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElCard, ElCheckTag, ElEmpty, ElInput, ElSwitch, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import { openServerLogsStream, type LogEntry, type ServerLogsStreamHandle } from '../services/serverLogsStream';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const;
type LogLevel = typeof LEVELS[number];

const CLIENT_BUFFER_CAP = 2000;
const AUTO_SCROLL_THRESHOLD_PX = 30;

const entries = ref<LogEntry[]>([]);
// Plain reactive record — one ref per chip. Avoids reactive-Set quirks
// where `:checked="set.has(lvl)"` could miss the re-render on replace.
const activeLevels = ref<Record<LogLevel, boolean>>({
  DEBUG: true,
  INFO: true,
  WARNING: true,
  ERROR: true,
  CRITICAL: true,
});
const searchText = ref('');
const paused = ref(false);
const autoScroll = ref(true);
const streamReady = ref(false);
const streamError = ref<string | null>(null);

const viewportRef = ref<HTMLElement | null>(null);
let streamHandle: ServerLogsStreamHandle | null = null;

function handleLevelChange(level: LogLevel, value: boolean) {
  activeLevels.value[level] = value;
}

const visibleEntries = computed(() => {
  const needle = searchText.value.trim().toLowerCase();
  return entries.value.filter((e) => {
    if (!activeLevels.value[e.level as LogLevel]) return false;
    if (needle && !e.message.toLowerCase().includes(needle) && !e.source.toLowerCase().includes(needle)) {
      return false;
    }
    return true;
  });
});

function pushEntry(entry: LogEntry) {
  if (paused.value) return;
  const wasNearBottom = isViewportNearBottom();
  entries.value.push(entry);
  if (entries.value.length > CLIENT_BUFFER_CAP) {
    entries.value.splice(0, entries.value.length - CLIENT_BUFFER_CAP);
  }
  if (autoScroll.value && wasNearBottom) {
    nextTick(scrollViewportToBottom);
  }
}

function isViewportNearBottom(): boolean {
  const el = viewportRef.value;
  if (!el) return true;
  return el.scrollTop + el.clientHeight >= el.scrollHeight - AUTO_SCROLL_THRESHOLD_PX;
}

function scrollViewportToBottom() {
  const el = viewportRef.value;
  if (!el) return;
  el.scrollTop = el.scrollHeight;
}

function handleClear() {
  entries.value = [];
}

function handleSaveLog() {
  if (visibleEntries.value.length === 0) return;
  const lines = visibleEntries.value.map((e) => {
    const ts = e.ts || '';
    const level = (e.level || '').padEnd(8);
    return `[${ts}] ${level} ${e.source} - ${e.message}`;
  });
  const content = lines.join('\n') + '\n';
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const filename = `openpa-logs-${stamp}.log`;
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function handleTogglePause() {
  paused.value = !paused.value;
}

function handleScrollToBottom() {
  scrollViewportToBottom();
}

function goBack() {
  router.push(`/${props.profile}`);
}

function levelTagType(level: string): 'success' | 'info' | 'warning' | 'danger' | 'primary' {
  switch (level) {
    case 'DEBUG': return 'info';
    case 'INFO': return 'success';
    case 'WARNING': return 'warning';
    case 'ERROR':
    case 'CRITICAL': return 'danger';
    default: return 'info';
  }
}

function formatTimestamp(iso: string): string {
  if (!iso) return '';
  // Parse ISO into HH:mm:ss.SSS — Loguru emits ISO 8601 with microseconds.
  const m = iso.match(/T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/);
  if (!m) return iso;
  const millis = (m[4] ?? '').padEnd(3, '0').slice(0, 3);
  return `${m[1]}:${m[2]}:${m[3]}.${millis}`;
}

onMounted(() => {
  const agentUrl = settingsStore.agentUrl;
  const token = settingsStore.authToken;
  if (!agentUrl || !token) {
    streamError.value = 'Not authenticated';
    return;
  }
  streamHandle = openServerLogsStream(
    agentUrl,
    token,
    (entry) => pushEntry(entry),
    () => {
      streamReady.value = true;
      nextTick(scrollViewportToBottom);
    },
    (err) => {
      streamError.value = err instanceof Error ? err.message : String(err);
    },
  );
});

onBeforeUnmount(() => {
  streamHandle?.close();
  streamHandle = null;
});

// Snap to bottom whenever auto-scroll is re-enabled.
watch(autoScroll, (on) => {
  if (on) nextTick(scrollViewportToBottom);
});
</script>

<template>
  <div class="developer-page">
    <button class="back-btn" @click="goBack">
      <Icon icon="mdi:arrow-left" />
      Back to Chat
    </button>

    <div class="developer-header">
      <h1>Developer</h1>
      <p>Debugging and development tools.</p>
    </div>

    <ElCard class="logs-card" shadow="never">
      <template #header>
        <div class="logs-card-header">
          <div class="logs-card-title">
            <Icon icon="mdi:file-document-outline" class="logs-card-icon" />
            <span>Server Logs</span>
            <ElTag
              v-if="streamReady"
              type="success"
              size="small"
              effect="plain"
            >live</ElTag>
            <ElTag
              v-else-if="!streamError"
              type="info"
              size="small"
              effect="plain"
            >connecting…</ElTag>
            <ElTag
              v-else
              type="danger"
              size="small"
              effect="plain"
            >{{ streamError }}</ElTag>
          </div>
          <div class="logs-card-actions">
            <ElButton
              size="small"
              :type="paused ? 'primary' : 'default'"
              @click="handleTogglePause"
            >
              <Icon :icon="paused ? 'mdi:play' : 'mdi:pause'" />
              {{ paused ? 'Resume' : 'Pause' }}
            </ElButton>
            <ElButton size="small" @click="handleClear">
              <Icon icon="mdi:broom" />
              Clear
            </ElButton>
            <ElButton
              size="small"
              :disabled="visibleEntries.length === 0"
              @click="handleSaveLog"
            >
              <Icon icon="mdi:download" />
              Save
            </ElButton>
            <ElButton size="small" @click="handleScrollToBottom">
              <Icon icon="mdi:chevron-down" />
              Tail
            </ElButton>
            <div class="autoscroll-toggle">
              <span>Auto-scroll</span>
              <ElSwitch v-model="autoScroll" size="small" />
            </div>
          </div>
        </div>
      </template>

      <div class="logs-toolbar">
        <div class="level-chips">
          <ElCheckTag
            v-for="lvl in LEVELS"
            :key="lvl"
            :checked="activeLevels[lvl]"
            :class="['level-chip', `level-chip-${lvl.toLowerCase()}`]"
            @change="handleLevelChange(lvl, $event)"
          >{{ lvl }}</ElCheckTag>
        </div>
        <ElInput
          v-model="searchText"
          placeholder="Filter by text…"
          size="small"
          class="logs-search"
          clearable
        >
          <template #prefix><Icon icon="mdi:magnify" /></template>
        </ElInput>
      </div>

      <div
        ref="viewportRef"
        class="logs-viewport"
        :class="{ 'logs-viewport-paused': paused }"
      >
        <div
          v-for="(entry, idx) in visibleEntries"
          :key="idx"
          :class="['log-row', `log-row-${entry.level.toLowerCase()}`]"
        >
          <span class="log-time">{{ formatTimestamp(entry.ts) }}</span>
          <ElTag
            :type="levelTagType(entry.level)"
            size="small"
            effect="plain"
            class="log-level"
          >{{ entry.level }}</ElTag>
          <span class="log-source">{{ entry.source }}</span>
          <span class="log-message">{{ entry.message }}</span>
        </div>
        <ElEmpty
          v-if="visibleEntries.length === 0"
          :image-size="64"
          description="No log entries match the current filters."
        />
      </div>
      <div class="logs-footer">
        <span>{{ visibleEntries.length }} shown · {{ entries.length }} buffered (cap {{ CLIENT_BUFFER_CAP }})</span>
        <span v-if="paused" class="paused-hint">Paused — incoming entries are discarded.</span>
      </div>
    </ElCard>
  </div>
</template>

<style scoped>
.developer-page {
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.back-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.875rem;
  padding: 4px 0;
  margin-bottom: 16px;
  transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }

.developer-header { margin-bottom: 20px; }
.developer-header h1 { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text-primary); }
.developer-header p { font-size: 0.875rem; color: var(--text-secondary); margin: 0; }

.logs-card { background: var(--card-bg, var(--bg-color)); }

.logs-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.logs-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text-primary);
}
.logs-card-icon { font-size: 20px; color: var(--primary-color); }
.logs-card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.autoscroll-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  padding-left: 8px;
}

.logs-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.level-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.level-chip {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  font-size: 0.78rem;
  letter-spacing: 0.04em;
  padding: 4px 12px;
  cursor: pointer;
  user-select: none;
  border: 1px solid transparent;
  opacity: 0.45;
  transition: opacity 0.15s, border-color 0.15s, background-color 0.15s;
}
.level-chip:hover { opacity: 0.85; }
.level-chip.is-checked {
  opacity: 1;
  border-color: currentColor;
}
.logs-search { width: 260px; }

.logs-viewport {
  background: #1e1e1e;
  color: #d4d4d4;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.55;
  padding: 12px 14px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  height: 60vh;
  min-height: 320px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  transition: opacity 0.15s;
}
.logs-viewport-paused { opacity: 0.7; }

.log-row {
  display: grid;
  grid-template-columns: 92px 78px minmax(160px, 240px) 1fr;
  gap: 10px;
  padding: 2px 0;
  align-items: baseline;
}
.log-row + .log-row { border-top: 1px dotted rgba(255, 255, 255, 0.05); }
.log-time { color: #9ca3af; }
.log-level { justify-self: start; min-width: 60px; text-align: center; }
.log-source { color: #94a3b8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.log-message { color: #e2e8f0; }

.log-row-debug .log-message { color: #94a3b8; }
.log-row-info .log-message { color: #d4d4d4; }
.log-row-warning .log-message { color: #facc15; }
.log-row-error .log-message { color: #f87171; }
.log-row-critical { background: rgba(248, 113, 113, 0.08); }
.log-row-critical .log-message { color: #fca5a5; font-weight: 600; }

.logs-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  font-size: 0.8rem;
  color: var(--text-tertiary, var(--text-secondary));
}
.paused-hint { color: var(--el-color-warning); }
</style>
