<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElSteps, ElStep, ElButton, ElMessage,
  ElForm, ElFormItem, ElInput, ElRadio, ElRadioGroup, ElAlert, ElTag,
} from 'element-plus';
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

// ── First-run installer phase (Electron only) ────────────────────────────
//
// When the renderer is hosted inside the Electron app and the persisted
// runtime config has no ``agentUrl``, this view *also* hosts the four
// installer steps (Welcome / Deployment / Mode / Install) at the start
// of the wizard, so the user moves through a single continuous flow:
//
//   Welcome → Deployment → Mode → Install → Server → … → Complete
//
// The web build (``__IS_ELECTRON__ === false``) and Electron launches
// where the install has already happened skip the prefix entirely. In
// both of those cases ``includeInstallerSteps`` is ``false`` and this
// file behaves identically to the curl-installed Setup Wizard.

const isElectron = computed(
  () => typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__,
);
const installerBridge = computed(() => window.openpa?.installer);

type InstallEnv = {
  os: 'linux' | 'macos' | 'windows' | 'unknown';
  arch: string;
  hasDocker: boolean;
  hasPython: boolean;
  pythonVersion: string;
  recommendedMode: 'docker' | 'native';
  channel: 'production' | 'test' | 'dev';
};

const installEnv = ref<InstallEnv | null>(null);
const detectingInstallEnv = ref(false);
const installDeployment = ref<'local' | 'server'>('local');
const installAppHost = ref('');
const installMode = ref<'docker' | 'native'>('native');
const installLog = ref<Array<{ stream: string; line: string }>>([]);
const installing = ref(false);
const installDone = ref(false);
const installFailed = ref(false);
const installError = ref('');

// Two visible stages plus the interstitial:
//   'installer'   — Electron-only: Welcome / Deployment / Mode / Install
//   'transition' — install finished; the user clicks Continue to start
//                   the Setup Wizard. Always rendered between the two
//                   stages so the boundary is unambiguous.
//   'setup'       — the existing Setup Wizard (Server / Embedding / …)
//
// Web users (``__IS_ELECTRON__ === false``) and Electron users with a
// prior install start at ``'setup'`` and behave exactly like the
// pre-merge curl-installed flow.
const currentStage = ref<'installer' | 'transition' | 'setup'>(
  isElectron.value && !settingsStore.agentUrl ? 'installer' : 'setup',
);

const installDockerDisabled = computed(() => {
  if (!installEnv.value) return false;
  return !installEnv.value.hasDocker;
});

function installerPushLog(entry: { stream: string; line: string }) {
  for (const piece of entry.line.split(/\r?\n/)) {
    if (piece) installLog.value.push({ stream: entry.stream, line: piece });
  }
  queueMicrotask(() => {
    const el = document.querySelector<HTMLElement>('.installer-log-pane');
    if (el) el.scrollTop = el.scrollHeight;
  });
}

function onInstallerDone(result: { exitCode: number; error?: string }) {
  installing.value = false;
  if (result.exitCode === 0) {
    installDone.value = true;
    // Slide into the transition stage. ``continueToSetupWizard`` is
    // what eventually spawns the backend (creating the SQLite DB at
    // that point), sets agentUrl, and opens the embedding SSE stream
    // — we deliberately do none of that here so the install step
    // leaves no server / no DB behind until the user explicitly
    // continues.
    setTimeout(() => {
      currentStage.value = 'transition';
    }, 600);
  } else {
    installFailed.value = true;
    installError.value =
      result.error || `Installer exited with code ${result.exitCode}.`;
  }
}

async function continueToSetupWizard() {
  // Now that the user has acknowledged the install, spin up the
  // backend (the install script no longer does this in Electron mode
  // — that's how we keep the SQLite DB from being created at install
  // time) and then run the setup-status / preset / catalog fetches
  // before mounting the Setup Wizard. ``checkingStatus`` doubles as
  // the button's loading state.
  checkingStatus.value = true;
  try {
    const bridge = window.openpa?.server;
    if (bridge) {
      const result = await bridge.start();
      if (!result.ok) {
        installFailed.value = true;
        installError.value =
          result.error || 'Failed to start the OpenPA backend.';
        // Drop back to the install-run step so the user sees the error
        // alongside the install log.
        currentStage.value = 'installer';
        currentStep.value = installerSteps.length - 1;
        return;
      }
    }
    // Sync the agentUrl now that the backend is up. ``setAgentUrl``
    // persists through to runtimeConfig so future launches skip the
    // installer stage.
    if (!settingsStore.agentUrl) {
      await settingsStore.setAgentUrl('http://localhost:1112');
    }
    embeddingStatusStore.connect(settingsStore.agentUrl);
    await runPostInstallSetupChecks();
  } finally {
    checkingStatus.value = false;
  }
  currentStage.value = 'setup';
  currentStep.value = 0;
}

async function startInstallerRun() {
  const bridge = installerBridge.value;
  if (!bridge) {
    installFailed.value = true;
    installError.value = 'The installer is only available in the Electron app.';
    return;
  }
  installing.value = true;
  installDone.value = false;
  installFailed.value = false;
  installError.value = '';
  installLog.value = [];
  try {
    await bridge.run({
      deployment: installDeployment.value,
      appHost:
        installDeployment.value === 'server' ? installAppHost.value.trim() : undefined,
      mode: installMode.value,
    });
    // The actual success/failure transition is driven by the
    // ``openpa:installer:done`` event handler (``onInstallerDone``); the
    // ``run`` promise just resolves once the child exits.
  } catch (err) {
    installing.value = false;
    installFailed.value = true;
    installError.value = String(err);
  }
}

// Read embedding lifecycle state from the SSE-driven Pinia store. The
// wizard runs pre-token, but the embedding stream is unauthenticated
// for exactly this case. App.vue's onMounted has already opened the
// shared connection by the time this view is rendered (the wizard is
// rendered as a child route).
const embeddingStatusStore = useEmbeddingStatusStore();
const { status: embeddingStatus, error: embeddingError } = storeToRefs(embeddingStatusStore);

// Belt-and-suspenders: in case the user navigates straight to /setup
// before App.vue's onMounted fires (rare but possible in dev), make
// sure the stream is connected. Skip if agentUrl is still unset —
// that's the Electron first-run case where the backend isn't up yet;
// ``onInstallerDone`` re-runs this once the script has reported success.
if (settingsStore.agentUrl) {
  embeddingStatusStore.connect(settingsStore.agentUrl);
}

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

// First-run installer steps (Electron only, stage = 'installer').
const installerSteps = [
  { key: 'install-welcome', title: 'Welcome', description: 'Get OpenPA running' },
  { key: 'install-deployment', title: 'Deployment', description: 'How OpenPA is reached' },
  { key: 'install-mode', title: 'Mode', description: 'Docker or native' },
  { key: 'install-run', title: 'Install', description: 'Bootstrap the backend' },
] as const;

// Setup-wizard core steps. ``isFirstSetup=true`` adds the Server +
// Embedding steps that the admin uses to wire up storage and embeddings;
// ``isFirstSetup=false`` is the per-profile flow used for additional
// profiles after admin setup is done.
const setupSteps = computed(() => {
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

// The set of steps shown for the current stage. The transition stage
// shows no step bar at all (we render an interstitial card instead).
const steps = computed(() => {
  if (currentStage.value === 'installer') return [...installerSteps];
  if (currentStage.value === 'setup') return setupSteps.value;
  return [];
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

async function detectInstallEnvironment() {
  const bridge = installerBridge.value;
  if (!bridge) return;
  detectingInstallEnv.value = true;
  try {
    installEnv.value = await bridge.detect();
    installMode.value = installEnv.value.recommendedMode;
  } catch (err) {
    installError.value = `Detection failed: ${err}`;
  } finally {
    detectingInstallEnv.value = false;
  }
}

async function runPostInstallSetupChecks() {
  // Mirrors the script-install path: hit the running backend for setup
  // status + preset + channel catalog, populate ``isFirstSetup`` and the
  // pre-fill values, redirect if setup is already complete.
  try {
    const status = await checkSetupStatus(settingsStore.agentUrl, profileName.value);
    isFirstSetup.value = !status.setup_complete;

    if (isFirstSetup.value) {
      for (const p of settingsStore.getLoggedInProfiles()) {
        settingsStore.removeTokenForProfile(p);
      }
      localStorage.removeItem('openpa.notifications.v1');
    }

    if (status.setup_complete && !props.profile) {
      router.replace('/login/admin');
      return;
    }
    if (status.profile_exists && props.profile) {
      router.replace(`/login/${props.profile}`);
      return;
    }

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

    try {
      channelsCatalog.value = await fetchChannelCatalogPublic(settingsStore.agentUrl);
    } catch {
      // Silent fallback — pairing rows simply won't render.
    }
  } catch {
    // Server unreachable - assume first setup
    isFirstSetup.value = true;
  }
}

onMounted(async () => {
  // Path A: Electron + first run. Host the installer stage here. The
  // backend isn't running yet, so skip ``checkSetupStatus``; we run it
  // lazily when ``continueToSetupWizard`` is clicked.
  if (currentStage.value === 'installer') {
    isFirstSetup.value = true;  // genuine first-run
    checkingStatus.value = false;
    const bridge = installerBridge.value;
    if (bridge) {
      bridge.onLog(installerPushLog);
      bridge.onDone(onInstallerDone);
      void detectInstallEnvironment();
    }
    return;
  }

  // Path B: web build, or Electron with a prior install. No installer
  // stage — run the existing setup-status flow against the already-up
  // backend.
  checkingStatus.value = true;
  await runPostInstallSetupChecks();
  checkingStatus.value = false;
});

onUnmounted(() => {
  const bridge = installerBridge.value;
  if (bridge) {
    bridge.offLog(installerPushLog);
    bridge.offDone(onInstallerDone);
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

// Per-step gates. Disabling Next is friendlier than silently advancing
// into an invalid state.
const canAdvanceFromInstallerDeployment = computed(() => {
  if (installDeployment.value === 'local') return true;
  return /^[A-Za-z0-9.:-]+$/.test(installAppHost.value.trim());
});
const canAdvanceFromInstallerMode = computed(() => {
  if (installMode.value === 'docker' && installDockerDisabled.value) return false;
  return true;
});

function handleNext() {
  const key = currentStepKey.value;

  // Installer phase gates / actions.
  if (key === 'install-welcome') {
    if (detectingInstallEnv.value || !installEnv.value) return;
    currentStep.value++;
    return;
  }
  if (key === 'install-deployment') {
    if (!canAdvanceFromInstallerDeployment.value) return;
    currentStep.value++;
    return;
  }
  if (key === 'install-mode') {
    if (!canAdvanceFromInstallerMode.value) return;
    // Advance into the Install step and trigger the script there.
    currentStep.value++;
    void startInstallerRun();
    return;
  }
  if (key === 'install-run') {
    // No manual advance — onInstallerDone schedules the transition once
    // the script reports success.
    return;
  }

  // Existing setup-step gates.
  if (key === 'embedding' && embeddingConfig.value?.enabled) {
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
  // No going back from the install-run step once the script has started:
  // the venv / db / .env writes are already in flight.
  if (currentStepKey.value === 'install-run' && (installing.value || installDone.value)) {
    return;
  }
  if (currentStep.value > 0) {
    currentStep.value--;
  }
}

function retryInstallerRun() {
  installFailed.value = false;
  installError.value = '';
  installLog.value = [];
  void startInstallerRun();
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
          <template v-if="currentStage === 'installer'">
            OpenPA — first-run installer
          </template>
          <template v-else-if="currentStage === 'transition'">
            Installation complete
          </template>
          <template v-else>
            {{ isFirstSetup ? 'Welcome to OpenPA' : `Setup Profile: ${profileName}` }}
          </template>
        </h1>
        <p class="setup-subtitle">
          <template v-if="currentStage === 'installer'">
            Set up the backend, then continue to the Setup Wizard.
          </template>
          <template v-else-if="currentStage === 'transition'">
            The backend is running. Continue to configure your LLM
            providers, tools, and first profile.
          </template>
          <template v-else>
            {{ isFirstSetup ? "Let's set up your personal assistant" : 'Configure your new profile' }}
          </template>
        </p>
        <div
          v-if="activeProfile && currentStage === 'setup'"
          class="setup-profile-badge"
        >
          Pre-filled from <strong>{{ activeProfile.label }}</strong> preset (SETUP_WIZARD_ENV)
        </div>
      </div>

      <ElSteps
        v-if="currentStage !== 'transition'"
        :active="currentStep"
        finish-status="success"
        align-center
        class="setup-steps"
      >
        <ElStep v-for="step in steps" :key="step.key" :title="step.title" :description="step.description" />
      </ElSteps>

      <!-- Interstitial between the two stages. The user clicks Continue
           to enter the Setup Wizard; making this an explicit step keeps
           the boundary unambiguous and gives the user a chance to re-read
           the install log before moving on. -->
      <div v-if="currentStage === 'transition'" class="stage-transition">
        <div class="stage-transition-icon"></div>
        <h2>First-run installer finished.</h2>
        <p>
          The OpenPA backend is up and running. Next: configure your LLM
          providers, built-in tools, channels, and create the first
          profile in the Setup Wizard.
        </p>
        <details class="stage-transition-log">
          <summary>View install log</summary>
          <pre class="installer-log-pane installer-log-pane--inline">
            <span
              v-for="(entry, i) in installLog" :key="i"
              :class="['installer-log-line', `installer-log-${entry.stream}`]"
            >{{ entry.line }}</span>
          </pre>
        </details>
        <ElButton
          type="primary"
          size="large"
          :loading="checkingStatus"
          @click="continueToSetupWizard"
        >
          {{ checkingStatus ? 'Loading setup wizard…' : 'Continue to Setup Wizard →' }}
        </ElButton>
      </div>

      <div class="step-content">
        <!-- ── Installer phase (Electron + first-run only) ──────── -->
        <div v-if="currentStepKey === 'install-welcome'" class="installer-pane">
          <h2>Let's get OpenPA running.</h2>
          <p v-if="installEnv?.channel === 'dev'">
            Developer install. We'll reuse the local checkout's
            <code>.venv</code> instead of downloading from PyPI. After a
            few quick questions we'll start the backend, then continue to
            the setup wizard — all inside this window.
          </p>
          <p v-else>
            This installer runs the same script as
            <code>curl …/install.sh</code>. We'll ask three quick questions
            (deployment, host, and run mode), then install OpenPA and
            continue to the setup wizard for the first profile.
          </p>
          <p v-if="installEnv" class="installer-env-tags">
            <ElTag :type="installEnv.hasDocker ? 'success' : 'info'">
              Docker {{ installEnv.hasDocker ? 'detected' : 'not available' }}
            </ElTag>
            <ElTag :type="installEnv.hasPython ? 'success' : 'info'">
              Python {{ installEnv.hasPython ? installEnv.pythonVersion : 'not on PATH' }}
            </ElTag>
            <ElTag>{{ installEnv.os }} / {{ installEnv.arch }}</ElTag>
          </p>
          <p v-else-if="detectingInstallEnv">Detecting your environment…</p>
        </div>

        <div v-else-if="currentStepKey === 'install-deployment'" class="installer-pane">
          <h2>How will you reach OpenPA?</h2>
          <ElForm label-position="top">
            <ElFormItem>
              <ElRadioGroup v-model="installDeployment">
                <ElRadio value="local">
                  <strong>Local</strong> — bind to 127.0.0.1, only this machine
                </ElRadio>
                <ElRadio value="server">
                  <strong>Server</strong> — bind to all interfaces, reachable from other devices
                </ElRadio>
              </ElRadioGroup>
            </ElFormItem>
            <ElFormItem v-if="installDeployment === 'server'" label="Public IP or domain">
              <ElInput v-model="installAppHost" placeholder="e.g. 100.120.175.90 or openpa.example.com" />
              <p class="installer-hint">
                Used in <code>APP_URL</code> and <code>CORS_ALLOWED_ORIGINS</code>.
                Letters, digits, dot, colon, and hyphen only.
              </p>
            </ElFormItem>
          </ElForm>
        </div>

        <div v-else-if="currentStepKey === 'install-mode'" class="installer-pane">
          <h2>How should OpenPA run?</h2>
          <ElForm label-position="top">
            <ElFormItem>
              <ElRadioGroup v-model="installMode">
                <ElRadio value="docker" :disabled="installDockerDisabled">
                  <strong>Docker</strong> — sandboxed VNC desktop with bundled Postgres + Qdrant
                  <span v-if="installEnv?.recommendedMode === 'docker'" class="installer-badge">recommended</span>
                  <span class="installer-hint">
                    The agent runs inside a container with its own GUI. Observe
                    at <code>http://&lt;host&gt;:6080/vnc.html</code>.
                    <template v-if="installEnv?.channel === 'dev'">
                      <br>Not available with <code>npm run dev</code> — Docker support for the dev channel is not yet implemented.
                    </template>
                  </span>
                </ElRadio>
                <ElRadio value="native">
                  <strong>Native</strong> — Python venv at <code>~/.openpa/venv</code> with SQLite
                  <span v-if="installEnv?.recommendedMode === 'native'" class="installer-badge">recommended</span>
                  <span class="installer-hint">
                    Simpler, but the agent shares your desktop and home directory.
                    <template v-if="installEnv && !installEnv.hasPython">
                      <br>Python 3.13 isn't on PATH; the installer will fetch an isolated copy via uv (~80 MB).
                    </template>
                  </span>
                </ElRadio>
              </ElRadioGroup>
            </ElFormItem>
          </ElForm>
        </div>

        <div v-else-if="currentStepKey === 'install-run'" class="installer-pane">
          <h2 v-if="installing">Installing…</h2>
          <h2 v-else-if="installDone">Installed.</h2>
          <h2 v-else-if="installFailed">Install failed.</h2>
          <h2 v-else>Preparing installer…</h2>
          <ElAlert
            v-if="installFailed"
            type="error" show-icon :closable="false"
            :title="installError || 'The installer reported an error.'"
            description="Scroll the log below for details. You can retry once the issue is resolved."
            style="margin-bottom: 12px;"
          />
          <pre class="installer-log-pane">
            <span
              v-for="(entry, i) in installLog" :key="i"
              :class="['installer-log-line', `installer-log-${entry.stream}`]"
            >{{ entry.line }}</span>
          </pre>
        </div>

        <!-- ── Existing setup steps ───────────────────────────── -->
        <StepServerConfig
          v-else-if="currentStepKey === 'server'"
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

      <div v-if="currentStage !== 'transition'" class="step-actions">
        <!-- Previous: hidden during the install-run step and after the
             token has been generated. -->
        <ElButton
          v-if="currentStep > 0 && !generatedToken
                && !(currentStepKey === 'install-run' && (installing || installDone))"
          @click="handlePrev"
        >
          Previous
        </ElButton>
        <div class="spacer"></div>

        <!-- Installer-phase action buttons (replace the generic Next on
             the install steps) -->
        <template v-if="currentStepKey === 'install-welcome'">
          <ElButton
            type="primary"
            :disabled="detectingInstallEnv || !installEnv"
            @click="handleNext"
          >
            Get started
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-deployment'">
          <ElButton
            type="primary"
            :disabled="!canAdvanceFromInstallerDeployment"
            @click="handleNext"
          >
            Next
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-mode'">
          <ElButton
            type="primary"
            :disabled="!canAdvanceFromInstallerMode"
            @click="handleNext"
          >
            Install
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-run'">
          <ElButton
            v-if="installFailed"
            type="primary"
            @click="retryInstallerRun"
          >
            Try again
          </ElButton>
          <!-- During install and on success: no manual action; the
               install-done handler advances the step on its own. -->
        </template>

        <!-- Setup-phase action buttons (unchanged from the original
             single-flow wizard) -->
        <template v-else>
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
        </template>
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

/* ── Stage transition (between installer + setup wizard) ───────────── */
.stage-transition {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 36px 16px 8px;
  text-align: center;
}
.stage-transition h2 {
  font-size: 1.2rem;
  color: var(--text-primary);
  margin: 0;
}
.stage-transition p {
  margin: 0;
  max-width: 480px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.stage-transition-icon {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--el-color-success-light-9, rgba(103, 194, 58, 0.15));
  position: relative;
}
.stage-transition-icon::after {
  content: '';
  position: absolute;
  top: 18px;
  left: 16px;
  width: 22px;
  height: 11px;
  border-left: 3px solid var(--el-color-success, #67c23a);
  border-bottom: 3px solid var(--el-color-success, #67c23a);
  transform: rotate(-45deg);
}
.stage-transition-log {
  width: 100%;
  max-width: 600px;
  text-align: left;
  margin-top: 4px;
}
.stage-transition-log summary {
  cursor: pointer;
  font-size: 0.85rem;
  color: var(--text-secondary);
  user-select: none;
  margin-bottom: 8px;
}
.stage-transition-log .installer-log-pane--inline {
  height: 200px;
}

/* ── Installer-phase styles (Electron + first-run only) ─────────────── */
.installer-pane h2 {
  font-size: 1.05rem;
  margin: 0 0 12px 0;
  color: var(--text-primary);
}
.installer-pane code {
  background: rgba(127, 127, 127, 0.15);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.92em;
}
.installer-pane .installer-hint {
  display: block;
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.45;
  color: var(--text-secondary);
}
.installer-pane .installer-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 9px;
  background: var(--el-color-success-light-9);
  color: var(--el-color-success);
}
.installer-pane .installer-env-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.installer-pane .installer-log-pane {
  background: #1e1e1e;
  color: #ddd;
  padding: 12px;
  border-radius: 6px;
  height: 280px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  margin: 0;
}
.installer-pane .installer-log-line { display: block; }
.installer-pane .installer-log-stderr { color: #ff8a8a; }
.installer-pane .installer-log-info { color: #80b9ff; }

/* Element Plus radios default to inline-flex laid out horizontally and
   force their label to a single line — multi-line slot content (the
   <strong> + description + hint blocks on the Mode page) collides with
   that layout. Stack them vertically and let labels wrap. */
.installer-pane :deep(.el-radio-group) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  width: 100%;
  gap: 12px;
}
.installer-pane :deep(.el-radio) {
  display: flex;
  align-items: flex-start;
  width: 100%;
  height: auto;
  margin-right: 0;
  white-space: normal;
}
.installer-pane :deep(.el-radio__input) { margin-top: 3px; }
.installer-pane :deep(.el-radio__label) {
  white-space: normal;
  line-height: 1.5;
}

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
