<script setup lang="ts">
import { ref, watch, computed, onMounted } from 'vue';
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioGroup, ElRadio, ElSelect, ElOption } from 'element-plus';

const props = defineProps<{
  config: Record<string, any>;
  // True only during the very first setup wizard pass for the admin. The DB
  // provider choice is exposed only when this is true; on every subsequent
  // wizard run (e.g. after reconfigure, or for any non-admin profile) the
  // backend silently rejects changes anyway, but we hide the UI as well so
  // it doesn't look configurable.
  firstSetup?: boolean;
}>();

const emit = defineEmits<{
  update: [config: Record<string, any>];
}>();

const isFirstSetup = computed(() => props.firstSetup ?? true);

const form = ref({
  service_name: props.config.service_name || 'openpa-agent',
  agent_name: props.config.agent_name || 'OPENPA Agent',
  working_dir: props.config.working_dir || '~/.openpa',
  user_working_dir: props.config.user_working_dir || '~/Documents',
  sqlite_db_path: props.config.sqlite_db_path || 'openpa.db',
  db_provider: (props.config.db_provider as 'sqlite' | 'postgres') || 'sqlite',
  postgres: {
    host: props.config.postgres?.host || 'localhost',
    port: props.config.postgres?.port ?? 5432,
    database: props.config.postgres?.database || 'openpa',
    user: props.config.postgres?.user || 'openpa',
    password: props.config.postgres?.password || '',
    sslmode: props.config.postgres?.sslmode || 'prefer',
  },
});

// Emit only the keys relevant to the chosen backend so the wizard payload
// stays clean (and the server-side filter doesn't have to second-guess us).
function buildPayload() {
  const base: Record<string, any> = {
    service_name: form.value.service_name,
    agent_name: form.value.agent_name,
    working_dir: form.value.working_dir,
    user_working_dir: form.value.user_working_dir,
  };
  if (!isFirstSetup.value || form.value.db_provider === 'sqlite') {
    base.sqlite_db_path = form.value.sqlite_db_path;
  }
  if (isFirstSetup.value) {
    base.db_provider = form.value.db_provider;
    if (form.value.db_provider === 'postgres') {
      base.postgres = { ...form.value.postgres };
    }
  }
  return base;
}

watch(form, () => {
  emit('update', buildPayload());
}, { deep: true });

onMounted(() => {
  emit('update', buildPayload());
});
</script>

<template>
  <div class="step-server-config">
    <h3 class="step-title">Server Configuration</h3>
    <p class="step-description">
      Configure your OpenPA server identity. Host and port are managed via the <code>.env</code> file on the server.
    </p>

    <ElForm label-position="top" class="config-form">
      <ElFormItem label="Service Name">
        <ElInput v-model="form.service_name" placeholder="openpa-agent" />
      </ElFormItem>
      <ElFormItem label="Agent Display Name">
        <ElInput v-model="form.agent_name" placeholder="OPENPA Agent" />
      </ElFormItem>
      <ElFormItem label="OpenPA System Directory">
        <ElInput v-model="form.working_dir" placeholder="~/.openpa" />
        <div class="field-hint">Internal storage for profiles, skills, persona, and tool state.</div>
      </ElFormItem>
      <ElFormItem label="User Working Directory">
        <ElInput v-model="form.user_working_dir" placeholder="~/Documents" />
        <div class="field-hint">Default active path for all tools. Created on first run if missing.</div>
      </ElFormItem>

      <template v-if="isFirstSetup">
        <ElFormItem label="Database Provider">
          <ElRadioGroup v-model="form.db_provider">
            <ElRadio value="sqlite">SQLite (default)</ElRadio>
            <ElRadio value="postgres">PostgreSQL</ElRadio>
          </ElRadioGroup>
          <div class="field-hint">
            Chosen once during first setup and locked thereafter — adding or migrating
            data to a different backend later requires manual export/import.
          </div>
        </ElFormItem>
      </template>

      <template v-if="form.db_provider === 'sqlite'">
        <ElFormItem label="Database Name">
          <ElInput v-model="form.sqlite_db_path" placeholder="openpa.db" />
          <div class="field-hint">SQLite database filename, stored inside the OpenPA system directory.</div>
        </ElFormItem>
      </template>

      <template v-else>
        <ElFormItem label="Postgres Host">
          <ElInput v-model="form.postgres.host" placeholder="localhost" />
        </ElFormItem>
        <ElFormItem label="Postgres Port">
          <ElInputNumber v-model="form.postgres.port" :min="1" :max="65535" />
        </ElFormItem>
        <ElFormItem label="Database Name">
          <ElInput v-model="form.postgres.database" placeholder="openpa" />
        </ElFormItem>
        <ElFormItem label="Username">
          <ElInput v-model="form.postgres.user" placeholder="openpa" />
        </ElFormItem>
        <ElFormItem label="Password">
          <ElInput v-model="form.postgres.password" type="password" show-password />
          <div class="field-hint">
            Stored in <code>~/.openpa/bootstrap.toml</code> in plaintext —
            same trust model as <code>~/.pgpass</code>.
          </div>
        </ElFormItem>
        <ElFormItem label="SSL Mode">
          <ElSelect v-model="form.postgres.sslmode">
            <ElOption label="disable" value="disable" />
            <ElOption label="allow" value="allow" />
            <ElOption label="prefer" value="prefer" />
            <ElOption label="require" value="require" />
            <ElOption label="verify-ca" value="verify-ca" />
            <ElOption label="verify-full" value="verify-full" />
          </ElSelect>
        </ElFormItem>
      </template>
    </ElForm>

    <div class="info-box">
      <strong>Note:</strong> Server host, port, and URL are configured in the <code>.env</code> file on the server machine.
      These cannot be changed from the UI as they require a server restart.
    </div>
  </div>
</template>

<style scoped>
.step-server-config {
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
  margin: 0 0 24px 0;
  line-height: 1.5;
}

.config-form {
  max-width: 480px;
}

.field-hint {
  margin-top: 4px;
  font-size: 0.775rem;
  color: var(--text-secondary);
  line-height: 1.4;
}

.info-box {
  margin-top: 24px;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.825rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.info-box code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.8rem;
}
</style>
