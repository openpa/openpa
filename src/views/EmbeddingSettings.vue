<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useRouter } from 'vue-router';
import {
  ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption,
  ElSwitch, ElButton, ElMessage, ElMessageBox,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useEmbeddingStatusStore } from '../stores/embeddingStatus';
import {
  getEmbeddingConfig,
  applyEmbeddingConfig,
  type EmbeddingConfig,
} from '../services/configApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();
const embeddingStatusStore = useEmbeddingStatusStore();

// Subscribe reactively to the SSE-driven store. App.vue opens the
// stream globally; this page just reads.
const { status, phase, error: errorMsg, busy: isBusy } = storeToRefs(embeddingStatusStore);

const loading = ref(true);
const saving = ref(false);

const form = ref<EmbeddingConfig>({
  enabled: false,
  provider: 'me5',
  hf_token: '',
  vectorstore: {
    provider: 'chroma',
    qdrant: { host: 'localhost', port: 6333, api_key: '', https: false },
    chroma: { mode: 'persistent', host: 'localhost', port: 8000, ssl: false, api_key: '', persist_path: '' },
  },
});

const showGemmaToken = computed(() => form.value.enabled && form.value.provider === 'gemma');
const showQdrant = computed(() => form.value.enabled && form.value.vectorstore.provider === 'qdrant');
const showChroma = computed(() => form.value.enabled && form.value.vectorstore.provider === 'chroma');
const showChromaHttp = computed(() => showChroma.value && form.value.vectorstore.chroma.mode === 'http');
const showChromaPersistent = computed(() => showChroma.value && form.value.vectorstore.chroma.mode === 'persistent');

const phaseLabel = computed(() => {
  if (!phase.value) return '';
  return ({
    loading_model: 'Loading embedding model…',
    connecting_store: 'Connecting to vector store…',
    preparing_rebuild: 'Preparing rebuild…',
    rebuilding_places: 'Rebuilding Google Places type embeddings…',
    rebuilding_tools: 'Rebuilding tool & skill embeddings…',
    rebuilding_docs: 'Rebuilding documentation embeddings…',
  } as Record<string, string>)[phase.value] ?? phase.value;
});

const statusBadgeText = computed(() => {
  switch (status.value) {
    case 'ready': return 'Success';
    case 'initializing':
    case 'rebuilding':
      return 'Waiting';
    case 'failed': return 'Failed';
    default: return 'Disabled';
  }
});

const statusBadgeClass = computed(() => `status-badge status-${status.value}`);

async function loadConfig() {
  loading.value = true;
  try {
    // Form values are not part of the SSE state, so we still fetch
    // them once via REST. The runtime status (status / phase / error)
    // comes from the SSE store.
    const res = await getEmbeddingConfig(settingsStore.agentUrl, settingsStore.authToken);
    form.value = res.config;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load embedding config');
  } finally {
    loading.value = false;
  }
}

// Detect a busy→ready transition so we can confirm to the user that
// the rebuild they triggered actually finished. We only flash the
// success toast when the watcher saw a busy state immediately before;
// otherwise opening the page on an already-ready system would
// spuriously fire a "completed" notification.
const wasBusy = ref(false);
watch(status, (curr) => {
  if (isBusy.value) {
    wasBusy.value = true;
    return;
  }
  if (wasBusy.value && curr === 'ready') {
    ElMessage.success('Vector embedding update completed successfully.');
  } else if (wasBusy.value && curr === 'failed') {
    ElMessage.error(`Vector embedding update failed: ${errorMsg.value ?? 'unknown error'}`);
  }
  wasBusy.value = false;
});

async function applyChanges() {
  if (form.value.enabled && form.value.provider === 'gemma' && !form.value.hf_token.trim()) {
    ElMessage.error('HF_TOKEN is required when the embedding provider is Gemma.');
    return;
  }

  try {
    await ElMessageBox.confirm(
      form.value.enabled
        ? 'Applying these changes will reload the embedding model and rebuild every embedding cache. Existing data in the new vector store will be replaced. The agent will be unavailable until the rebuild completes.'
        : 'Disabling Vector Embedding will turn off semantic search across the app. Cached vectors are kept untouched in the existing store, so re-enabling later is fast.',
      'Apply embedding changes?',
      { confirmButtonText: 'Apply', cancelButtonText: 'Cancel' },
    );
  } catch {
    return;
  }

  saving.value = true;
  try {
    const res = await applyEmbeddingConfig(settingsStore.agentUrl, settingsStore.authToken, form.value);
    // Surface the immediate POST response, but the source of truth is
    // the SSE stream — the store will reflect subsequent transitions
    // automatically.
    if (res.status === 'failed') {
      ElMessage.error(`Embedding apply failed: ${res.error ?? 'unknown error'}`);
    } else if (res.status === 'disabled') {
      ElMessage.success('Vector Embedding disabled.');
    } else {
      ElMessage.success('Embedding update started — please wait for it to finish.');
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to apply embedding config');
  } finally {
    saving.value = false;
  }
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

onMounted(() => {
  // Vector Embedding is admin-only. The router guard blocks
  // navigation in the normal case; this is a defense-in-depth fallback
  // so the page never tries to render or fetch as a non-admin profile.
  if (props.profile !== 'admin') {
    router.replace(`/${props.profile}/settings`);
    return;
  }
  loadConfig();
});
</script>

<template>
  <div class="embedding-settings">
    <div class="settings-container">
      <div class="settings-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <h1 class="settings-title">Vector Embedding</h1>
        <p class="settings-subtitle">
          Toggle semantic search and configure the embedding model + vector store.
        </p>
      </div>

      <div v-if="loading" class="loading-state">Loading…</div>

      <template v-else>
        <div class="status-row">
          <span class="status-label">Current status:</span>
          <span :class="statusBadgeClass">{{ statusBadgeText }}</span>
          <span v-if="phaseLabel" class="status-phase">— {{ phaseLabel }}</span>
        </div>
        <div v-if="errorMsg" class="error-banner">{{ errorMsg }}</div>

        <ElForm label-position="top" class="config-form" :disabled="isBusy || saving">
          <ElFormItem label="Enable Vector Embedding">
            <ElSwitch v-model="form.enabled" :disabled="isBusy || saving" />
            <div class="field-hint">
              {{ form.enabled
                  ? 'Configure the model and vector store below. Applying changes will reload + rebuild caches.'
                  : 'Embedding-dependent features (Automatic Skill Mode, semantic Google Places filtering, doc search) are disabled.' }}
            </div>
          </ElFormItem>

          <template v-if="form.enabled">
            <div class="section-divider"></div>
            <h3 class="section-title">Embedding Model</h3>

            <ElFormItem label="Model">
              <ElSelect v-model="form.provider" style="width: 100%">
                <ElOption value="me5" label="ME5 — Multilingual E5 Base (no auth, 768 dims)" />
                <ElOption value="gemma" label="Gemma 300M — Google (requires HF_TOKEN, 768 dims)" />
              </ElSelect>
            </ElFormItem>

            <ElFormItem v-if="showGemmaToken" label="HuggingFace Token (HF_TOKEN)">
              <ElInput
                v-model="form.hf_token"
                type="password"
                show-password
                placeholder="hf_..."
              />
              <div class="field-hint">
                Required to download the gated <code>google/embeddinggemma-300m</code> model.
              </div>
            </ElFormItem>

            <div class="section-divider"></div>
            <h3 class="section-title">Vector Store</h3>

            <ElFormItem label="Provider">
              <ElSelect v-model="form.vectorstore.provider" style="width: 100%">
                <ElOption value="qdrant" label="Qdrant" />
                <ElOption value="chroma" label="ChromaDB" />
              </ElSelect>
              <div class="field-hint">
                Switching stores triggers a full rebuild — the new store starts empty.
              </div>
            </ElFormItem>

            <template v-if="showQdrant">
              <ElFormItem label="Qdrant Host">
                <ElInput v-model="form.vectorstore.qdrant.host" placeholder="localhost" />
              </ElFormItem>
              <ElFormItem label="Qdrant Port">
                <ElInputNumber v-model="form.vectorstore.qdrant.port" :min="1" :max="65535" />
              </ElFormItem>
              <ElFormItem label="API Key (optional)">
                <ElInput v-model="form.vectorstore.qdrant.api_key" type="password" show-password />
              </ElFormItem>
              <ElFormItem label="Use HTTPS">
                <ElSwitch v-model="form.vectorstore.qdrant.https" />
              </ElFormItem>
            </template>

            <template v-if="showChroma">
              <ElFormItem label="Mode">
                <ElSelect v-model="form.vectorstore.chroma.mode" style="width: 100%">
                  <ElOption value="http" label="HTTP server" />
                  <ElOption value="persistent" label="Persistent (local file)" />
                </ElSelect>
              </ElFormItem>

              <template v-if="showChromaHttp">
                <ElFormItem label="Chroma Host">
                  <ElInput v-model="form.vectorstore.chroma.host" placeholder="localhost" />
                </ElFormItem>
                <ElFormItem label="Chroma Port">
                  <ElInputNumber v-model="form.vectorstore.chroma.port" :min="1" :max="65535" />
                </ElFormItem>
                <ElFormItem label="Use SSL">
                  <ElSwitch v-model="form.vectorstore.chroma.ssl" />
                </ElFormItem>
                <ElFormItem label="API Key (optional)">
                  <ElInput v-model="form.vectorstore.chroma.api_key" type="password" show-password />
                </ElFormItem>
              </template>

              <template v-if="showChromaPersistent">
                <ElFormItem label="Persist Path">
                  <ElInput
                    v-model="form.vectorstore.chroma.persist_path"
                    placeholder="Leave blank for <working_dir>/storage/chroma"
                  />
                </ElFormItem>
              </template>
            </template>
          </template>

          <div class="actions">
            <ElButton
              type="primary"
              :loading="saving || isBusy"
              :disabled="isBusy"
              @click="applyChanges"
            >
              {{ isBusy ? 'Waiting…' : 'Apply Changes' }}
            </ElButton>
          </div>
        </ElForm>
      </template>
    </div>
  </div>
</template>

<style scoped>
.embedding-settings { width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color); padding: 24px; box-sizing: border-box; }
.settings-container { max-width: 720px; margin: 0 auto; }
.settings-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px;
}
.back-btn:hover { color: var(--primary-color); }
.settings-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text-primary); }
.settings-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading-state { color: var(--text-secondary); padding: 24px 0; }

.status-row {
  display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  font-size: 0.875rem;
}
.status-label { color: var(--text-secondary); }
.status-badge {
  display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600;
}
.status-ready { background: #e8f5e9; color: #2e7d32; }
.status-initializing, .status-rebuilding { background: #fff8e1; color: #b07300; }
.status-failed { background: #ffebee; color: #b03030; }
.status-disabled { background: var(--hover-bg); color: var(--text-secondary); }
.status-phase { color: var(--text-secondary); }

.error-banner {
  margin: 8px 0 16px 0; padding: 10px 14px;
  background: #fff4f4; border: 1px solid #f5a3a3; border-radius: 6px;
  color: #b03030; font-size: 0.825rem;
}

.config-form { max-width: 540px; }
.section-divider { border-top: 1px solid var(--border-color); margin: 18px 0 14px; }
.section-title { font-size: 0.95rem; font-weight: 600; color: var(--text-primary); margin: 0 0 10px 0; }
.field-hint { margin-top: 4px; font-size: 0.775rem; color: var(--text-secondary); line-height: 1.4; }
.field-hint code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.78rem; }
.actions { margin-top: 24px; }
</style>
