<script setup lang="ts">
// First-run installer view. Walks the same decision tree as the curl /
// iwr install scripts, then shells out to those scripts via the IPC
// bridge in electron/main.ts so we don't duplicate install logic.
//
// Lifecycle:
//   1. detect() runs once on mount to populate `env` (Docker availability,
//      Python version, OS). Disables the docker mode card if Docker is
//      missing; otherwise pre-selects it.
//   2. The user clicks through deployment → mode → confirm.
//   3. ``installer.run({...})`` starts the script. ``onLog`` appends to
//      the log buffer; ``onDone`` flips into success or failure state.
//   4. On success, the main process has already persisted the agent URL
//      into openpa-config.json — we just route to /setup.
//
// Cancellation isn't wired to a button yet because the install scripts
// are not safe to interrupt mid-run (they'd leave a partially-built
// venv or a half-rendered compose file behind). The cancel IPC exists
// for the future "abort before install starts" flow.

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ElButton, ElCard, ElForm, ElFormItem, ElInput, ElRadio, ElRadioGroup,
  ElSteps, ElStep, ElAlert, ElTag,
} from 'element-plus'

const router = useRouter()

type Step = 0 | 1 | 2 | 3 | 4
const step = ref<Step>(0)

type Deployment = 'local' | 'server'
type Mode = 'docker' | 'native'

const deployment = ref<Deployment>('local')
const appHost = ref('')
const mode = ref<Mode>('native')

type DetectedEnv = {
  os: 'linux' | 'macos' | 'windows' | 'unknown'
  arch: string
  hasDocker: boolean
  hasPython: boolean
  pythonVersion: string
  recommendedMode: Mode
}
const env = ref<DetectedEnv | null>(null)
const detecting = ref(false)

const installing = ref(false)
const installDone = ref(false)
const installFailed = ref(false)
const installError = ref('')
const log = ref<Array<{ stream: string; line: string }>>([])

function pushLog(entry: { stream: string; line: string }) {
  // Split on newlines so each log line stands alone in the rendered list.
  // Some shells emit chunks of multiple lines per write; collapsing them
  // into one row would make scanning the install output painful.
  for (const piece of entry.line.split(/\r?\n/)) {
    if (piece) log.value.push({ stream: entry.stream, line: piece })
  }
  // Naive auto-scroll: queueMicrotask gives the DOM a frame to update
  // its height before we scroll past the new bottom.
  queueMicrotask(() => {
    const el = document.querySelector<HTMLElement>('.log-pane')
    if (el) el.scrollTop = el.scrollHeight
  })
}

function onDoneHandler(result: { exitCode: number; error?: string }) {
  installing.value = false
  if (result.exitCode === 0) {
    installDone.value = true
  } else {
    installFailed.value = true
    installError.value = result.error || `Installer exited with code ${result.exitCode}.`
  }
}

const installer = computed(() => window.openpa?.installer)

onMounted(async () => {
  if (!installer.value) {
    installFailed.value = true
    installError.value = 'The installer is only available in the Electron app.'
    return
  }
  detecting.value = true
  try {
    env.value = await installer.value.detect()
    mode.value = env.value.recommendedMode
  } catch (err) {
    installError.value = `Detection failed: ${err}`
  } finally {
    detecting.value = false
  }

  installer.value.onLog(pushLog)
  installer.value.onDone(onDoneHandler)
})

onUnmounted(() => {
  installer.value?.offLog(pushLog)
  installer.value?.offDone(onDoneHandler)
})

// Validation gates per step. Disabling the Next button is friendlier
// than letting the user advance and seeing an error.
const canAdvanceFromDeployment = computed(() => {
  if (deployment.value === 'local') return true
  // Same character class the install scripts validate against.
  return /^[A-Za-z0-9.:-]+$/.test(appHost.value.trim())
})
const canAdvanceFromMode = computed(() => {
  if (mode.value === 'docker' && env.value && !env.value.hasDocker) return false
  if (mode.value === 'native' && env.value && !env.value.hasPython) return false
  return true
})

async function startInstall() {
  if (!installer.value) return
  step.value = 4
  installing.value = true
  log.value = []
  try {
    await installer.value.run({
      deployment: deployment.value,
      appHost: deployment.value === 'server' ? appHost.value.trim() : undefined,
      mode: mode.value,
    })
  } catch (err) {
    installing.value = false
    installFailed.value = true
    installError.value = String(err)
  }
}

function goToWizard() {
  router.replace({ name: 'setup' })
}
</script>

<template>
  <div class="installer">
    <h1>OpenPA — first-run installer</h1>
    <ElSteps :active="step" finish-status="success" simple style="margin-bottom: 24px;">
      <ElStep title="Welcome" />
      <ElStep title="Deployment" />
      <ElStep title="Mode" />
      <ElStep title="Confirm" />
      <ElStep title="Install" />
    </ElSteps>

    <!-- Step 0: welcome -->
    <ElCard v-if="step === 0">
      <h2>Let's get OpenPA running.</h2>
      <p>
        This installer downloads and runs the same script used by
        <code>curl …/install.sh</code>. We'll ask three quick questions
        (deployment type, host, and run mode), then install OpenPA and
        open the setup wizard for the first profile.
      </p>
      <p v-if="env">
        <ElTag :type="env.hasDocker ? 'success' : 'info'">
          Docker {{ env.hasDocker ? 'detected' : 'not available' }}
        </ElTag>
        <ElTag :type="env.hasPython ? 'success' : 'info'" style="margin-left: 8px;">
          Python {{ env.hasPython ? env.pythonVersion : 'not on PATH' }}
        </ElTag>
        <ElTag style="margin-left: 8px;">{{ env.os }} / {{ env.arch }}</ElTag>
      </p>
      <p v-else-if="detecting">Detecting your environment…</p>
      <ElButton type="primary" :disabled="detecting || !env" @click="step = 1">Get started</ElButton>
    </ElCard>

    <!-- Step 1: deployment -->
    <ElCard v-else-if="step === 1">
      <h2>How will you reach OpenPA?</h2>
      <ElForm label-position="top">
        <ElFormItem>
          <ElRadioGroup v-model="deployment">
            <ElRadio value="local">
              <strong>Local</strong> — bind to 127.0.0.1, only this machine
            </ElRadio>
            <ElRadio value="server">
              <strong>Server</strong> — bind to all interfaces, reachable from other devices
            </ElRadio>
          </ElRadioGroup>
        </ElFormItem>
        <ElFormItem v-if="deployment === 'server'" label="Public IP or domain">
          <ElInput v-model="appHost" placeholder="e.g. 100.120.175.90 or openpa.example.com" />
          <p class="hint">
            Used in <code>APP_URL</code> and <code>CORS_ALLOWED_ORIGINS</code>.
            Letters, digits, dot, colon, and hyphen only.
          </p>
        </ElFormItem>
      </ElForm>
      <div class="actions">
        <ElButton @click="step = 0">Back</ElButton>
        <ElButton type="primary" :disabled="!canAdvanceFromDeployment" @click="step = 2">Next</ElButton>
      </div>
    </ElCard>

    <!-- Step 2: mode -->
    <ElCard v-else-if="step === 2">
      <h2>How should OpenPA run?</h2>
      <ElForm label-position="top">
        <ElFormItem>
          <ElRadioGroup v-model="mode">
            <ElRadio value="docker" :disabled="!env?.hasDocker">
              <strong>Docker</strong> — sandboxed VNC desktop with bundled Postgres + Qdrant
              <span v-if="env?.recommendedMode === 'docker'" class="badge">recommended</span>
              <p class="hint">
                The agent runs inside a container with its own GUI. Observe
                at <code>http://&lt;host&gt;:6080/vnc.html</code>.
              </p>
            </ElRadio>
            <ElRadio value="native" :disabled="!env?.hasPython">
              <strong>Native</strong> — Python venv at <code>~/.openpa/venv</code> with SQLite
              <span v-if="env?.recommendedMode === 'native'" class="badge">recommended</span>
              <p class="hint">
                Simpler, but the agent shares your desktop and home directory.
              </p>
            </ElRadio>
          </ElRadioGroup>
        </ElFormItem>
        <ElAlert
          v-if="env && !env.hasDocker && !env.hasPython"
          type="error" show-icon :closable="false"
          title="Neither Docker nor Python 3.13+ is available."
          description="Install one and reopen this app to continue."
        />
      </ElForm>
      <div class="actions">
        <ElButton @click="step = 1">Back</ElButton>
        <ElButton type="primary" :disabled="!canAdvanceFromMode" @click="step = 3">Next</ElButton>
      </div>
    </ElCard>

    <!-- Step 3: confirm -->
    <ElCard v-else-if="step === 3">
      <h2>Ready to install</h2>
      <ul class="summary">
        <li><strong>Deployment:</strong> {{ deployment }}{{ deployment === 'server' ? ` (${appHost})` : '' }}</li>
        <li><strong>Mode:</strong> {{ mode }}</li>
        <li><strong>OS:</strong> {{ env?.os }} / {{ env?.arch }}</li>
      </ul>
      <ElAlert
        type="info" show-icon :closable="false"
        title="The script will download and run from openpa.ai."
        description="It writes config to ~/.openpa, installs dependencies, and starts the backend. You can stop it anytime."
      />
      <div class="actions">
        <ElButton @click="step = 2">Back</ElButton>
        <ElButton type="primary" @click="startInstall">Install</ElButton>
      </div>
    </ElCard>

    <!-- Step 4: install / log stream -->
    <ElCard v-else-if="step === 4">
      <h2 v-if="installing">Installing…</h2>
      <h2 v-else-if="installDone">Installed.</h2>
      <h2 v-else>Install failed.</h2>

      <ElAlert
        v-if="installFailed"
        type="error" show-icon :closable="false"
        :title="installError || 'The installer reported an error.'"
        description="Scroll the log below for details. You can close this app and rerun the installer once the issue is resolved."
        style="margin-bottom: 12px;"
      />

      <pre class="log-pane">
        <span
          v-for="(entry, i) in log" :key="i"
          :class="['log-line', `log-${entry.stream}`]"
        >{{ entry.line }}</span>
      </pre>

      <div class="actions">
        <ElButton v-if="installDone" type="primary" @click="goToWizard">Open setup wizard</ElButton>
        <ElButton v-else-if="installFailed" @click="step = 3">Try again</ElButton>
      </div>
    </ElCard>
  </div>
</template>

<style scoped>
.installer {
  max-width: 760px;
  margin: 32px auto;
  padding: 0 16px;
  font-family: system-ui, -apple-system, sans-serif;
}
.installer h1 {
  font-size: 22px;
  margin-bottom: 24px;
}
.installer h2 {
  font-size: 18px;
  margin-top: 0;
}
.installer code {
  background: rgba(127, 127, 127, 0.15);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.92em;
}
.installer .hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.installer .summary {
  padding-left: 18px;
  line-height: 1.7;
}
.installer .actions {
  display: flex;
  justify-content: space-between;
  margin-top: 20px;
}
.installer .badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 9px;
  background: var(--el-color-success-light-9);
  color: var(--el-color-success);
}
.installer .log-pane {
  background: #1e1e1e;
  color: #ddd;
  padding: 12px;
  border-radius: 6px;
  height: 320px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  margin: 0;
}
.installer .log-line {
  display: block;
}
.installer .log-stderr {
  color: #ff8a8a;
}
.installer .log-info {
  color: #80b9ff;
}
</style>
