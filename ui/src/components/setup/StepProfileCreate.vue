<script setup lang="ts">
import { ElButton, ElInput, ElAlert } from 'element-plus';
import { Icon } from '@iconify/vue';

defineProps<{
  token: string;
  submitting: boolean;
  profileName: string;
}>();

const emit = defineEmits<{
  generate: [];
  copy: [];
}>();
</script>

<template>
  <div class="step-profile-create">
    <h3 class="step-title">Profile & Token</h3>
    <p class="step-description">
      Your profile will be created and a JWT token generated.
      The token is also saved on the server for recovery.
    </p>

    <div class="profile-info">
      <div class="info-row">
        <span class="info-label">Profile Name:</span>
        <span class="info-value">{{ profileName }}</span>
      </div>
    </div>

    <div v-if="!token" class="generate-section">
      <p class="step-description">
        Click the button below to save configuration and generate your authentication token.
      </p>
    </div>

    <div v-else class="token-section">
      <ElAlert type="success" :closable="false" show-icon class="token-alert">
        <template #title>Setup Complete!</template>
        Copy this token. It's also saved at <code>~/.openpa/tokens/{{ profileName }}.token</code> on the server.
      </ElAlert>

      <div class="token-display">
        <ElInput :model-value="token" type="textarea" :rows="3" readonly class="token-input" />
        <ElButton type="primary" @click="emit('copy')" class="copy-btn">
          <Icon icon="mdi:content-copy" /> Copy Token
        </ElButton>
      </div>

      <div class="info-box">
        If you lose this token, you can recover it from the server by reading <code>~/.openpa/tokens/{{ profileName }}.token</code>.
      </div>
    </div>
  </div>
</template>

<style scoped>
.step-profile-create { padding: 8px 0; }
.step-title { font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.step-description { color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 20px 0; line-height: 1.5; }
.profile-info { padding: 16px; background: var(--hover-bg); border-radius: 8px; margin-bottom: 24px; }
.info-row { display: flex; align-items: center; gap: 8px; }
.info-label { font-weight: 600; font-size: 0.875rem; color: var(--text-primary); }
.info-value { font-family: monospace; font-size: 0.95rem; color: var(--primary-color); font-weight: 600; }
.token-alert { margin-bottom: 16px; }
.token-alert code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.75rem; }
.token-display { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.token-input :deep(textarea) { font-family: monospace; font-size: 0.8rem; }
.copy-btn { align-self: flex-start; }
.info-box {
  padding: 12px 16px; background: var(--hover-bg); border-radius: 8px;
  font-size: 0.825rem; color: var(--text-secondary); line-height: 1.5;
}
.info-box code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.75rem; }
</style>
