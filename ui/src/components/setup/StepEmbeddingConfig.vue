<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue';
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption, ElSwitch } from 'element-plus';

export interface EmbeddingConfigPayload {
  enabled: boolean;
  provider: 'me5' | 'gemma';
  hf_token: string;
  vectorstore: {
    provider: 'qdrant' | 'chroma';
    qdrant: { host: string; port: number; api_key: string; https: boolean };
    chroma: {
      mode: 'http' | 'persistent';
      host: string;
      port: number;
      ssl: boolean;
      api_key: string;
      persist_path: string;
    };
  };
}

const props = defineProps<{
  config: Partial<EmbeddingConfigPayload>;
}>();

const emit = defineEmits<{
  update: [config: EmbeddingConfigPayload];
}>();

const form = ref<EmbeddingConfigPayload>({
  enabled: props.config.enabled ?? false,
  provider: (props.config.provider as EmbeddingConfigPayload['provider']) ?? 'me5',
  hf_token: props.config.hf_token ?? '',
  vectorstore: {
    // Default to ChromaDB in persistent mode so the user can complete
    // setup with no extra configuration — Chroma persistent stores its
    // database under <working_dir>/storage/chroma without needing an
    // external service running.
    provider: (props.config.vectorstore?.provider as 'qdrant' | 'chroma') ?? 'chroma',
    qdrant: {
      host: props.config.vectorstore?.qdrant?.host ?? 'localhost',
      port: props.config.vectorstore?.qdrant?.port ?? 6333,
      api_key: props.config.vectorstore?.qdrant?.api_key ?? '',
      https: props.config.vectorstore?.qdrant?.https ?? false,
    },
    chroma: {
      mode: (props.config.vectorstore?.chroma?.mode as 'http' | 'persistent') ?? 'persistent',
      host: props.config.vectorstore?.chroma?.host ?? 'localhost',
      port: props.config.vectorstore?.chroma?.port ?? 8000,
      ssl: props.config.vectorstore?.chroma?.ssl ?? false,
      api_key: props.config.vectorstore?.chroma?.api_key ?? '',
      persist_path: props.config.vectorstore?.chroma?.persist_path ?? '',
    },
  },
});

const showGemmaToken = computed(() => form.value.enabled && form.value.provider === 'gemma');
const showQdrant = computed(() => form.value.enabled && form.value.vectorstore.provider === 'qdrant');
const showChroma = computed(() => form.value.enabled && form.value.vectorstore.provider === 'chroma');
const showChromaHttp = computed(() => showChroma.value && form.value.vectorstore.chroma.mode === 'http');
const showChromaPersistent = computed(() => showChroma.value && form.value.vectorstore.chroma.mode === 'persistent');

watch(form, (val) => {
  emit('update', JSON.parse(JSON.stringify(val)));
}, { deep: true });

onMounted(() => {
  emit('update', JSON.parse(JSON.stringify(form.value)));
});
</script>

<template>
  <div class="step-embedding-config">
    <h3 class="step-title">Vector Embedding (Optional)</h3>
    <p class="step-description">
      Vector embedding lets OpenPA understand your queries semantically. You can enable it now or skip and turn it on later.
    </p>

    <div class="benefits-box">
      <strong>What you get when enabled:</strong>
      <ul>
        <li><strong>Automatic Skill Mode</strong> — OpenPA picks the most relevant skills for each request instead of showing the LLM all of them.</li>
        <li><strong>Google Places</strong> filters 336 place types down to the most relevant for your query, reducing tokens.</li>
        <li><strong>Document &amp; tool search</strong> uses semantic similarity for more accurate results.</li>
      </ul>
      <div class="benefits-note">
        First start downloads a model (~500&nbsp;MB for ME5, ~1.2&nbsp;GB for Gemma). Requires a running Qdrant or ChromaDB instance.
      </div>
    </div>

    <ElForm label-position="top" class="config-form">
      <ElFormItem label="Enable Vector Embedding">
        <ElSwitch v-model="form.enabled" />
        <div class="field-hint">
          {{ form.enabled
            ? 'Configure the embedding model and vector store below.'
            : 'Skip this step. Automatic Skill Mode will be unavailable; Google Places will use a small static type list.' }}
        </div>
      </ElFormItem>

      <template v-if="form.enabled">
        <div class="section-divider"></div>
        <h4 class="section-title">Embedding Model</h4>

        <ElFormItem label="Model">
          <ElSelect v-model="form.provider" placeholder="Select an embedding model" style="width: 100%">
            <ElOption value="me5" label="ME5 — Multilingual E5 Base (no auth required, 768 dims)" />
            <ElOption value="gemma" label="Gemma 300M — Google (requires HuggingFace token, 768 dims)" />
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
            Required to download the gated <code>google/embeddinggemma-300m</code> model. Generate one at huggingface.co/settings/tokens.
          </div>
        </ElFormItem>

        <div class="section-divider"></div>
        <h4 class="section-title">Vector Store</h4>

        <ElFormItem label="Provider">
          <ElSelect v-model="form.vectorstore.provider" style="width: 100%">
            <ElOption value="qdrant" label="Qdrant" />
            <ElOption value="chroma" label="ChromaDB" />
          </ElSelect>
        </ElFormItem>

        <template v-if="showQdrant">
          <ElFormItem label="Qdrant Host">
            <ElInput v-model="form.vectorstore.qdrant.host" placeholder="localhost" />
          </ElFormItem>
          <ElFormItem label="Qdrant Port">
            <ElInputNumber v-model="form.vectorstore.qdrant.port" :min="1" :max="65535" />
          </ElFormItem>
          <ElFormItem label="API Key (optional)">
            <ElInput
              v-model="form.vectorstore.qdrant.api_key"
              type="password"
              show-password
              placeholder="Leave blank if not using auth"
            />
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
              <ElInput
                v-model="form.vectorstore.chroma.api_key"
                type="password"
                show-password
                placeholder="Leave blank if not using auth"
              />
            </ElFormItem>
          </template>

          <template v-if="showChromaPersistent">
            <ElFormItem label="Persist Path">
              <ElInput
                v-model="form.vectorstore.chroma.persist_path"
                placeholder="Leave blank for <working_dir>/storage/chroma"
              />
              <div class="field-hint">
                Local directory where Chroma will store its database files.
              </div>
            </ElFormItem>
          </template>
        </template>
      </template>
    </ElForm>
  </div>
</template>

<style scoped>
.step-embedding-config {
  padding: 8px 0;
}

.step-title {
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 8px 0;
}

.step-description {
  color: var(--text-secondary);
  font-size: 0.875rem;
  margin: 0 0 16px 0;
  line-height: 1.5;
}

.benefits-box {
  margin: 0 0 24px 0;
  padding: 14px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.55;
}

.benefits-box ul {
  margin: 8px 0 8px 18px;
  padding: 0;
}

.benefits-box li {
  margin-bottom: 4px;
}

.benefits-box code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.8rem;
}

.benefits-note {
  margin-top: 8px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  opacity: 0.85;
}

.config-form {
  max-width: 540px;
}

.section-divider {
  border-top: 1px solid var(--border-color);
  margin: 18px 0 14px;
}

.section-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 10px 0;
}

.field-hint {
  margin-top: 4px;
  font-size: 0.775rem;
  color: var(--text-secondary);
  line-height: 1.4;
}

.field-hint code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.78rem;
}
</style>
