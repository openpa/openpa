<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElInput, ElDivider } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { checkSetupStatus, resetOrphanedSetup, listProfiles } from '../services/configApi';
import { fetchMe } from '../services/agentApi';

const router = useRouter();
const settingsStore = useSettingsStore();

const loggedInProfiles = ref<string[]>([]);
const checking = ref(true);

// Login form state
const loginProfileName = ref('');
const loginToken = ref('');
const loginLoading = ref(false);
const loginError = ref('');

onMounted(async () => {
  try {
    const status = await checkSetupStatus(settingsStore.agentUrl);

    if (!status.setup_complete) {
      router.replace('/setup');
      return;
    }

    // Detect orphaned setup: setup_complete=true but no profiles in DB
    if (status.has_profiles === false) {
      try {
        await resetOrphanedSetup(settingsStore.agentUrl);
      } catch {
        // If reset fails, still redirect — SetupWizard will re-check
      }
      // Clear all stale localStorage profile data
      for (const p of settingsStore.getLoggedInProfiles()) {
        settingsStore.removeTokenForProfile(p);
      }
      router.replace('/setup');
      return;
    }

    // Validate locally-stored profiles against server and prune stale entries
    const localProfiles = settingsStore.getLoggedInProfiles();
    if (localProfiles.length > 0) {
      try {
        const { profiles: serverProfiles } = await listProfiles(settingsStore.agentUrl, '');
        for (const lp of localProfiles) {
          if (!serverProfiles.includes(lp)) {
            settingsStore.removeTokenForProfile(lp);
          }
        }
      } catch {
        // Server unreachable for profile list — show what we have
      }
    }
  } catch {
    // Server unreachable — show whatever we have locally
  }

  loggedInProfiles.value = settingsStore.getLoggedInProfiles();
  checking.value = false;
});

function selectProfile(profile: string) {
  router.push(`/${profile}`);
}

async function handleLogin() {
  const profile = loginProfileName.value.trim();
  const token = loginToken.value.trim();
  if (!profile || !token) return;

  loginLoading.value = true;
  loginError.value = '';
  try {
    // Verify profile exists
    const status = await checkSetupStatus(settingsStore.agentUrl, profile);
    if (status.profile_exists === false) {
      loginError.value = `Profile '${profile}' does not exist`;
      return;
    }

    // Verify token
    const me = await fetchMe(settingsStore.agentUrl, token);
    if (me.profile !== profile) {
      loginError.value = `This token belongs to profile '${me.profile}', not '${profile}'`;
      return;
    }

    // Save and navigate
    settingsStore.setTokenForProfile(profile, token);
    settingsStore.activateProfile(profile);
    router.push(`/${profile}`);
  } catch {
    loginError.value = 'Invalid or expired token';
  } finally {
    loginLoading.value = false;
  }
}

</script>

<template>
  <div class="profile-selector" v-if="!checking">
    <div class="selector-container">
      <div class="selector-header">
        <h1 class="selector-title">OpenPA</h1>
        <p class="selector-subtitle">Select a profile or log in</p>
      </div>

      <!-- Saved profiles -->
      <div v-if="loggedInProfiles.length > 0" class="profiles-section">
        <div class="profiles-grid">
          <div
            v-for="profile in loggedInProfiles"
            :key="profile"
            class="profile-card"
            @click="selectProfile(profile)"
          >
            <div class="profile-avatar">
              <Icon icon="mdi:account-circle" />
            </div>
            <span class="profile-name">{{ profile }}</span>
            <Icon icon="mdi:chevron-right" class="profile-chevron" />
          </div>
        </div>

        <ElDivider>or log in to another profile</ElDivider>
      </div>

      <!-- Login form -->
      <div class="login-section">
        <div class="login-field">
          <label class="field-label">Profile name</label>
          <ElInput
            v-model="loginProfileName"
            placeholder="e.g. admin, lee"
            size="default"
            @keyup.enter="handleLogin"
          />
        </div>

        <div class="login-field">
          <label class="field-label">Token</label>
          <ElInput
            v-model="loginToken"
            type="textarea"
            :rows="2"
            placeholder="Paste your JWT token"
            class="token-input"
          />
        </div>

        <p v-if="loginError" class="login-error">{{ loginError }}</p>

        <div class="login-actions">
          <ElButton
            type="primary"
            :loading="loginLoading"
            @click="handleLogin"
            :disabled="!loginProfileName.trim() || !loginToken.trim()"
          >
            <Icon icon="mdi:login" /> Login
          </ElButton>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.profile-selector {
  width: 100%;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}

.selector-container {
  width: 100%;
  max-width: 420px;
  background: var(--surface-color);
  border-radius: 12px;
  border: 1px solid var(--border-color);
  padding: 32px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}

.selector-header {
  text-align: center;
  margin-bottom: 24px;
}

.selector-title {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 4px 0;
}

.selector-subtitle {
  font-size: 0.9rem;
  color: var(--text-secondary);
  margin: 0;
}

.profiles-section {
  margin-bottom: 0;
}

.profiles-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.profile-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  background: var(--bg-color);
}

.profile-card:hover {
  border-color: var(--primary-color);
  background: var(--hover-bg);
}

.profile-avatar {
  font-size: 28px;
  color: var(--primary-color);
  display: flex;
  align-items: center;
}

.profile-name {
  flex: 1;
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
}

.profile-chevron {
  font-size: 18px;
  color: var(--text-tertiary);
}

.login-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.login-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field-label {
  font-size: 0.8rem;
  font-weight: 500;
  color: var(--text-secondary);
}

.token-input :deep(textarea) {
  font-family: monospace;
  font-size: 0.8rem;
}

.login-error {
  color: var(--el-color-danger);
  font-size: 0.8rem;
  margin: 0;
}

.login-actions {
  display: flex;
  gap: 8px;
}
</style>
