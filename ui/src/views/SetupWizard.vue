<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElSteps, ElStep, ElButton, ElMessage } from 'element-plus';
import { useSettingsStore } from '../stores/settings';
import { useConfigStore } from '../stores/config';
import { useChatStore } from '../stores/chat';
import StepServerConfig from '../components/setup/StepServerConfig.vue';
import StepEmbeddingConfig, { type EmbeddingConfigPayload } from '../components/setup/StepEmbeddingConfig.vue';
import StepLLMConfig from '../components/setup/StepLLMConfig.vue';
import StepToolConfig from '../components/setup/StepToolConfig.vue';
import StepChannelsConfig from '../components/setup/StepChannelsConfig.vue';
import StepProfileCreate from '../components/setup/StepProfileCreate.vue';
import ChannelPairingDialog from '../components/channels/ChannelPairingDialog.vue';
import { storeToRefs } from 'pinia';
import {
  completeSetup,
  checkSetupStatus,
} from '../services/configApi';
import {
  fetchSetupProfiles,
  type SetupProfile,
} from '../services/setupProfilesApi';
import {
  fetchChannelCatalogPublic,
  type ChannelCatalogEntry,
  type ChannelRow,
  type CreateChannelPayload,
} from '../services/channelApi';
import { useEmbeddingStatusStore } from '../stores/embeddingStatus';

const props = defineProps<{
  profile?: string;
}>();

const router = useRouter();
const settingsStore = useSettingsStore();
const configStore = useConfigStore();
const chatStore = useChatStore();

const currentStep = ref(0);
const submitting = ref(false);

// Read embedding lifecycle state from the SSE-driven Pinia store. The
// wizard runs pre-token, but the embedding stream is unauthenticated
// for exactly this case. App.vue's onMounted has already opened the
// shared connection by the time this view is rendered (the wizard is
// rendered as a child route).
const embeddingStatusStore = useEmbeddingStatusStore();
const { status: embeddingStatus, error: embeddingError } = storeToRefs(embeddingStatusStore);

// Belt-and-suspenders: in case the user navigates straight to /setup
// before App.vue's onMounted fires (rare but possible in dev), make
// sure the stream is connected.
embeddingStatusStore.connect(settingsStore.agentUrl);

// Determine if this is the first setup or a profile-specific setup
const profileName = computed(() => props.profile || 'admin');
const isFirstSetup = ref(true);
const generatedToken = ref('');
const checkingStatus = ref(true);

// Collected config from each step.
// ``serverConfig`` may carry a nested ``postgres`` object when the admin
// picks PostgreSQL during first setup; everything else is a flat string map.
const serverConfig = ref<Record<string, any>>({});
const embeddingConfig = ref<EmbeddingConfigPayload | null>(null);
const llmConfig = ref<Record<string, string>>({});
const toolConfigs = ref<Record<string, Record<string, string>>>({});
const agentConfigs = ref<Record<string, Record<string, string>>>({});
const channelConfigs = ref<CreateChannelPayload[]>([]);

// Catalog of supported channel types — fetched pre-auth so the post-token
// pairing rows can look up ``setup_kind`` on each created channel.
const channelsCatalog = ref<Record<string, ChannelCatalogEntry>>({});

// Server-side outcome of channel creation, populated after ``handleCompleteSetup``
// succeeds. Used both for surfacing per-channel errors and for driving
// the in-wizard pairing prompts.
const createdChannels = ref<ChannelRow[]>([]);
const channelErrors = ref<Array<{ channel_type: string | null; error: string }>>([]);

// Pairing-dialog state for the post-token pass.
const pairingChannelId = ref<string>('');
const pairingChannelLabel = ref<string>('');
const pairingOpen = ref(false);

// Active environment preset. Determined by ``SETUP_WIZARD_ENV`` in the
// project ``.env`` and reported back by /api/config/setup-profiles. We
// only use it to pre-fill the wizard forms — every step still mounts its
// usual component, every field stays editable. ``null`` means the env
// var is unset and the wizard runs from its built-in component-level
// fallbacks (today's behaviour).
const activeProfile = ref<SetupProfile | null>(null);

// Step definitions depend on whether this is the first setup.
// First setup (admin): Server → Embedding → LLM → Tools → Channels → Complete
// Profile setup:       LLM → Tools → Channels → Complete  (no server config)
const steps = computed(() => {
  if (isFirstSetup.value) {
    return [
      { key: 'server', title: 'Server', description: 'General settings' },
      { key: 'embedding', title: 'Vector Embedding', description: 'Optional — semantic search' },
      { key: 'llm', title: 'LLM Providers', description: 'Configure AI models' },
      { key: 'tools', title: 'Tools', description: 'Configure built-in tools' },
      { key: 'channels', title: 'Channels', description: 'Connect messaging apps (optional)' },
      { key: 'complete', title: 'Complete', description: 'Generate token' },
    ];
  }
  return [
    { key: 'llm', title: 'LLM Providers', description: 'Configure AI models' },
    { key: 'tools', title: 'Tools', description: 'Configure built-in tools' },
    { key: 'channels', title: 'Channels', description: 'Connect messaging apps (optional)' },
    { key: 'complete', title: 'Complete', description: 'Create profile' },
  ];
});

function applyProfileDefaults(profile: SetupProfile) {
  activeProfile.value = profile;
  // Seed the step refs so each form mounts with the preset already loaded.
  // Deep-clone so the user editing fields doesn't mutate the cached preset.
  serverConfig.value = JSON.parse(JSON.stringify(profile.server_config ?? {}));
  embeddingConfig.value = profile.embedding_config
    ? (JSON.parse(JSON.stringify(profile.embedding_config)) as EmbeddingConfigPayload)
    : null;
}

// The key of the currently active step
const currentStepKey = computed(() => {
  return steps.value[currentStep.value]?.key || '';
});

// Whether we're on the final step
const isLastStep = computed(() => currentStep.value === steps.value.length - 1);

onMounted(async () => {
  checkingStatus.value = true;
  try {
    const status = await checkSetupStatus(settingsStore.agentUrl, profileName.value);
    isFirstSetup.value = !status.setup_complete;

    // Clear stale localStorage from any previous installation when re-running first setup
    if (isFirstSetup.value) {
      for (const p of settingsStore.getLoggedInProfiles()) {
        settingsStore.removeTokenForProfile(p);
      }
      // Drop notifications carried over from the previous DB — every entry
      // points at a conversation that no longer exists.
      localStorage.removeItem('openpa.notifications.v1');
    }

    // If setup is already complete and this is /setup (no profile), redirect to admin login
    if (status.setup_complete && !props.profile) {
      router.replace('/login/admin');
      return;
    }

    // If profile already exists, redirect to login
    if (status.profile_exists && props.profile) {
      router.replace(`/login/${props.profile}`);
      return;
    }

    // Resolve the active environment preset (driven by ``SETUP_WIZARD_ENV``
    // on the server) and pre-fill the step forms from it. Only relevant on
    // first setup — profile-only setup runs at a later stage where the env
    // shape is already locked into bootstrap.toml. If the endpoint fails or
    // the variable is unset we fall through to the components' built-in
    // fallback defaults, preserving today's behaviour.
    if (isFirstSetup.value) {
      try {
        const resp = await fetchSetupProfiles(settingsStore.agentUrl);
        if (resp.selected) {
          const preset = (resp.profiles ?? []).find((p) => p.id === resp.selected);
          if (preset) applyProfileDefaults(preset);
        }
      } catch {
        // Server unreachable or endpoint missing — leave the wizard
        // unconfigured; component-level fallbacks take over.
      }
    }

    // Pre-load the channel catalog so the post-token pairing pass can look
    // up ``setup_kind`` per channel. StepChannelsConfig has its own copy
    // for rendering the picker, but the wizard parent needs one too for
    // the pairing rows after Complete Setup. Best-effort: a missing
    // catalog only suppresses the auto-pairing UI; everything else still
    // works.
    try {
      channelsCatalog.value = await fetchChannelCatalogPublic(settingsStore.agentUrl);
    } catch {
      // Silent fallback — pairing rows simply won't render.
    }
  } catch {
    // Server unreachable - assume first setup
    isFirstSetup.value = true;
  } finally {
    checkingStatus.value = false;
  }
});

function handleServerConfigUpdate(config: Record<string, any>) {
  serverConfig.value = config;
}

function handleEmbeddingConfigUpdate(config: EmbeddingConfigPayload) {
  embeddingConfig.value = config;
}

function handleLLMConfigUpdate(config: Record<string, string>) {
  llmConfig.value = config;
}

function handleToolConfigsUpdate(configs: Record<string, Record<string, string>>) {
  toolConfigs.value = configs;
}

function handleAgentConfigsUpdate(configs: Record<string, Record<string, string>>) {
  agentConfigs.value = configs;
}

function handleChannelConfigsUpdate(configs: CreateChannelPayload[]) {
  channelConfigs.value = configs;
}

function modeNeedsPairing(channelType: string, modeId: string): boolean {
  const entry = channelsCatalog.value[channelType];
  if (!entry) return false;
  const mode = entry.modes.find((m) => m.id === modeId);
  return Boolean(mode && mode.setup_kind);
}

const channelsAwaitingPairing = computed(() =>
  createdChannels.value.filter((c) => modeNeedsPairing(c.channel_type, c.mode)),
);

function openPairingFor(channel: ChannelRow) {
  pairingChannelId.value = channel.id;
  pairingChannelLabel.value =
    channelsCatalog.value[channel.channel_type]?.display_name || channel.channel_type;
  pairingOpen.value = true;
}

function handleNext() {
  if (currentStepKey.value === 'embedding' && embeddingConfig.value?.enabled) {
    if (embeddingConfig.value.provider === 'gemma' && !embeddingConfig.value.hf_token.trim()) {
      ElMessage.error('HF_TOKEN is required when the embedding provider is Gemma.');
      return;
    }
  }
  if (currentStep.value < steps.value.length - 1) {
    currentStep.value++;
  }
}

function handlePrev() {
  if (currentStep.value > 0) {
    currentStep.value--;
  }
}

async function handleCompleteSetup() {
  submitting.value = true;
  try {
    const config: Record<string, unknown> = { profile: profileName.value };

    if (isFirstSetup.value) {
      config.server_config = serverConfig.value;
      if (embeddingConfig.value) {
        config.embedding_config = embeddingConfig.value;
      }
    }
    // Always send LLM and tool configs (both first setup and profile setup)
    config.llm_config = llmConfig.value;
    config.tool_configs = toolConfigs.value;
    if (Object.keys(agentConfigs.value).length > 0) {
      config.agent_configs = agentConfigs.value;
    }
    if (channelConfigs.value.length > 0) {
      config.channel_configs = channelConfigs.value;
    }

    const result = await completeSetup(settingsStore.agentUrl, config as any);
    generatedToken.value = result.token;
    createdChannels.value = (result as any).channels || [];
    channelErrors.value = (result as any).channel_errors || [];

    // Activate the token immediately so the in-wizard pairing dialog
    // (ChannelPairingDialog) can authenticate against the SSE stream.
    // Final navigation + chat-state reset still waits for the user to
    // click "Start Using OpenPA" in handleFinish().
    if (channelsAwaitingPairing.value.length > 0) {
      settingsStore.setTokenForProfile(profileName.value, generatedToken.value);
      settingsStore.activateProfile(profileName.value);
    }

    // The backend kicks off model load + cache rebuild in the
    // background when first-setup completes with embedding enabled.
    // We don't need to poll — the embedding-status SSE store is
    // already subscribed and will receive each phase transition
    // automatically. Status-driven computeds below gate the
    // "Start Using OpenPA" button.

    if (channelErrors.value.length > 0) {
      ElMessage.warning(
        `Setup completed, but ${channelErrors.value.length} channel(s) failed to register.`,
      );
    } else {
      ElMessage.success('Setup completed! Copy your token below.');
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Setup failed');
  } finally {
    submitting.value = false;
  }
}

async function handleFinish() {
  if (!generatedToken.value) {
    ElMessage.warning('Please complete setup first.');
    return;
  }
  if (embeddingBlocking.value) {
    ElMessage.warning('Vector embedding is still loading. Please wait a moment.');
    return;
  }
  if (embeddingFailed.value) {
    ElMessage.error(
      `Vector embedding failed to initialize: ${embeddingError.value ?? 'unknown error'}. ` +
      'You can still proceed, but Automatic Skill Mode and other embedding-dependent features will be unavailable.'
    );
    // Allow the user to proceed even on failure — they can fix the
    // backing service or disable embedding from Settings later.
  }

  // Save token per-profile and activate, then redirect to chat
  settingsStore.setTokenForProfile(profileName.value, generatedToken.value);
  settingsStore.activateProfile(profileName.value);
  configStore.setupComplete = true;
  // Drop any in-memory chat state from the pre-wipe session before entering
  // the chat view; otherwise the conversations list / messages survive and
  // their detail views 404 against the freshly empty DB.
  await chatStore.resetForProfileSwitch();
  router.push(`/${profileName.value}`);
}

const embeddingReady = computed(
  () => embeddingStatus.value === 'ready' || embeddingStatus.value === 'disabled',
);

const embeddingBlocking = computed(() => {
  if (!isFirstSetup.value) return false;
  if (!embeddingConfig.value?.enabled) return false;
  return embeddingStatus.value === 'initializing'
    || embeddingStatus.value === 'rebuilding';
});

const embeddingFailed = computed(() => embeddingStatus.value === 'failed');

async function copyToken() {
  try {
    await navigator.clipboard.writeText(generatedToken.value);
    ElMessage.success('Token copied to clipboard!');
  } catch {
    ElMessage.error('Failed to copy token');
  }
}
</script>

<template>
  <div class="setup-wizard">
    <div v-if="checkingStatus" class="loading-state">Checking setup status...</div>
    <div v-else class="setup-container">
      <div class="setup-header">
        <h1 class="setup-title">
          {{ isFirstSetup ? 'Welcome to OpenPA' : `Setup Profile: ${profileName}` }}
        </h1>
        <p class="setup-subtitle">
          {{ isFirstSetup ? "Let's set up your personal assistant" : 'Configure your new profile' }}
        </p>
        <div v-if="activeProfile" class="setup-profile-badge">
          Pre-filled from <strong>{{ activeProfile.label }}</strong> preset (SETUP_WIZARD_ENV)
        </div>
      </div>

      <ElSteps :active="currentStep" finish-status="success" align-center class="setup-steps">
        <ElStep v-for="step in steps" :key="step.key" :title="step.title" :description="step.description" />
      </ElSteps>

      <div class="step-content">
        <StepServerConfig
          v-if="currentStepKey === 'server'"
          :config="serverConfig"
          :first-setup="isFirstSetup"
          @update="handleServerConfigUpdate"
        />
        <StepEmbeddingConfig
          v-else-if="currentStepKey === 'embedding'"
          :config="embeddingConfig ?? {}"
          @update="handleEmbeddingConfigUpdate"
        />
        <StepLLMConfig
          v-else-if="currentStepKey === 'llm'"
          :agent-url="settingsStore.agentUrl"
          :config="llmConfig"
          @update="handleLLMConfigUpdate"
        />
        <StepToolConfig
          v-else-if="currentStepKey === 'tools'"
          :agent-url="settingsStore.agentUrl"
          :configs="toolConfigs"
          :profile="profileName"
          :is-first-setup="isFirstSetup"
          @update="handleToolConfigsUpdate"
          @update:agent-configs="handleAgentConfigsUpdate"
        />
        <StepChannelsConfig
          v-else-if="currentStepKey === 'channels'"
          :agent-url="settingsStore.agentUrl"
          :configs="channelConfigs"
          @update="handleChannelConfigsUpdate"
        />
        <StepProfileCreate
          v-else-if="currentStepKey === 'complete'"
          :token="generatedToken"
          :submitting="submitting"
          :profile-name="profileName"
          @generate="handleCompleteSetup"
          @copy="copyToken"
        />

        <div
          v-if="generatedToken && channelErrors.length > 0"
          class="channel-errors-box"
        >
          <strong>{{ channelErrors.length }} channel(s) failed to register</strong>
          <ul>
            <li v-for="(err, i) in channelErrors" :key="i">
              {{ err.channel_type || 'unknown' }}: {{ err.error }}
            </li>
          </ul>
          <p class="hint">
            Setup completed; you can re-add these from
            <strong>Settings → Channels</strong> after entering OpenPA.
          </p>
        </div>

        <div
          v-if="generatedToken && channelsAwaitingPairing.length > 0"
          class="pairing-box"
        >
          <strong>Pair your channels</strong>
          <p>
            The channels below need an interactive pairing step (QR scan or
            verification code) before they can receive messages. You can
            pair them now or skip and finish from
            <strong>Settings → Channels</strong> later.
          </p>
          <div
            v-for="ch in channelsAwaitingPairing"
            :key="ch.id"
            class="pairing-row"
          >
            <span class="pairing-name">
              {{ channelsCatalog[ch.channel_type]?.display_name || ch.channel_type }}
              <small>({{ ch.mode }})</small>
            </span>
            <ElButton size="small" @click="openPairingFor(ch)">Pair now</ElButton>
          </div>
        </div>

        <div
          v-if="generatedToken && embeddingConfig?.enabled && !embeddingReady"
          class="embedding-status-box"
          :class="{ 'is-failed': embeddingFailed }"
        >
          <template v-if="embeddingBlocking">
            <strong>Loading vector embedding model…</strong>
            <p>
              The embedding model is downloading and loading into memory.
              This can take a minute on first run.
              The Start button will activate when it's ready.
            </p>
          </template>
          <template v-else-if="embeddingFailed">
            <strong>Vector embedding failed to initialize</strong>
            <p>{{ embeddingError ?? 'Unknown error.' }}</p>
            <p class="hint">
              You can still proceed — Automatic Skill Mode and other
              embedding-dependent features will be unavailable until you
              fix the backing service or disable Vector Embedding from Settings.
            </p>
          </template>
        </div>
      </div>

      <div class="step-actions">
        <ElButton v-if="currentStep > 0 && !generatedToken" @click="handlePrev">Previous</ElButton>
        <div class="spacer"></div>
        <ElButton
          v-if="!isLastStep"
          type="primary"
          @click="handleNext"
        >
          Next
        </ElButton>
        <ElButton
          v-else-if="!generatedToken"
          type="primary"
          :loading="submitting"
          @click="handleCompleteSetup"
        >
          {{ isFirstSetup ? 'Complete Setup' : `Create Profile "${profileName}"` }}
        </ElButton>
        <ElButton
          v-else
          type="success"
          :loading="embeddingBlocking"
          :disabled="embeddingBlocking"
          @click="handleFinish"
        >
          {{ embeddingBlocking ? 'Loading embedding model…' : 'Start Using OpenPA' }}
        </ElButton>
      </div>
    </div>

    <ChannelPairingDialog
      v-model="pairingOpen"
      :channel-id="pairingChannelId"
      :channel-label="pairingChannelLabel"
    />
  </div>
</template>

<style scoped>
.setup-wizard {
  width: 100%;
  height: 100vh;
  display: flex;
  align-items: safe center;
  justify-content: safe center;
  background: var(--bg-color);
  overflow-y: auto;
  padding: 24px;
  box-sizing: border-box;
}

.loading-state { color: var(--text-secondary); font-size: 0.95rem; }

.setup-container {
  width: 100%;
  max-width: 720px;
  background: var(--surface-color);
  border-radius: 12px;
  border: 1px solid var(--border-color);
  padding: 32px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}

.setup-header { text-align: center; margin-bottom: 32px; }
.setup-title { font-size: 1.75rem; font-weight: 700; color: var(--text-primary); margin: 0 0 8px 0; }
.setup-subtitle { font-size: 0.95rem; color: var(--text-secondary); margin: 0; }
.setup-profile-badge {
  margin-top: 12px;
  display: inline-block;
  padding: 4px 10px;
  background: var(--hover-bg);
  border-radius: 999px;
  font-size: 0.78rem;
  color: var(--text-secondary);
}
.setup-profile-badge strong { color: var(--text-primary); }
.setup-steps { margin-bottom: 32px; }
.step-content { min-height: 300px; margin-bottom: 24px; }
.step-actions {
  display: flex; align-items: center; padding-top: 16px;
  border-top: 1px solid var(--border-color);
}
.spacer { flex: 1; }

.embedding-status-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.embedding-status-box strong {
  display: block;
  color: var(--text-primary);
  margin-bottom: 4px;
}

.embedding-status-box p {
  margin: 4px 0 0 0;
}

.embedding-status-box .hint {
  font-size: 0.78rem;
  opacity: 0.85;
  margin-top: 6px;
}

.embedding-status-box.is-failed {
  border: 1px solid #f5a3a3;
  background: #fff4f4;
  color: #b03030;
}

.embedding-status-box.is-failed strong { color: #b03030; }

.channel-errors-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: #fff4f4;
  border: 1px solid #f5a3a3;
  border-radius: 8px;
  font-size: 0.85rem;
  color: #b03030;
  line-height: 1.5;
}
.channel-errors-box strong { display: block; margin-bottom: 6px; }
.channel-errors-box ul { margin: 4px 0 6px 0; padding-left: 20px; }
.channel-errors-box .hint { margin-top: 6px; font-size: 0.78rem; color: var(--text-secondary); }

.pairing-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.5;
}
.pairing-box strong { display: block; color: var(--text-primary); margin-bottom: 4px; }
.pairing-box p { margin: 4px 0 8px 0; }
.pairing-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0;
  border-top: 1px solid var(--border-color);
}
.pairing-row:first-of-type { border-top: none; }
.pairing-name { color: var(--text-primary); font-weight: 500; }
.pairing-name small { color: var(--text-secondary); margin-left: 6px; font-weight: 400; }
</style>
