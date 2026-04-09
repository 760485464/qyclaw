<script setup>
import { computed, onMounted, reactive, ref } from "vue";

import { useI18n } from "../../i18n";
import { apiFetch } from "../../services/api";

const { t } = useI18n();
const loading = ref(false);
const savingConnection = ref(false);
const savingRule = ref(false);
const error = ref("");
const connections = ref([]);
const rules = ref([]);
const events = ref([]);
const options = ref({ platform_types: [], conversations: [] });
const editingConnectionId = ref("");
const editingRuleId = ref("");

const connectionForm = reactive({
  platform_type: "",
  display_name: "",
  app_key: "",
  app_secret_ref: "",
  bot_id: "",
  callback_url: "",
  enabled: true,
  config_json: "{}"
});

const ruleForm = reactive({
  connection_id: "",
  rule_name: "",
  source_scope: "all",
  source_id: "",
  keyword: "",
  conversation_id: "",
  default_model: "",
  execution_backend: "",
  enabled: true
});

const gatewayStatus = computed(() => {
  if (!connections.value.length) return t("gateway_status_pending");
  const enabledCount = connections.value.filter((item) => item.enabled).length;
  return `${enabledCount}/${connections.value.length}`;
});

function resetConnectionForm() {
  editingConnectionId.value = "";
  connectionForm.platform_type = "";
  connectionForm.display_name = "";
  connectionForm.app_key = "";
  connectionForm.app_secret_ref = "";
  connectionForm.bot_id = "";
  connectionForm.callback_url = "";
  connectionForm.enabled = true;
  connectionForm.config_json = "{}";
}

function resetRuleForm() {
  editingRuleId.value = "";
  ruleForm.connection_id = "";
  ruleForm.rule_name = "";
  ruleForm.source_scope = "all";
  ruleForm.source_id = "";
  ruleForm.keyword = "";
  ruleForm.conversation_id = "";
  ruleForm.default_model = "";
  ruleForm.execution_backend = "";
  ruleForm.enabled = true;
}

function safeConfigJson(value) {
  if (!value || typeof value !== "object") return "{}";
  return JSON.stringify(value, null, 2);
}

async function loadGateway() {
  loading.value = true;
  error.value = "";
  try {
    const [connectionData, ruleData, eventData, optionData] = await Promise.all([
      apiFetch("/gateway/connections"),
      apiFetch("/gateway/rules"),
      apiFetch("/gateway/events"),
      apiFetch("/gateway/options")
    ]);
    connections.value = connectionData.items || [];
    rules.value = ruleData.items || [];
    events.value = eventData.items || [];
    options.value = optionData || { platform_types: [], conversations: [] };
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function submitConnection() {
  savingConnection.value = true;
  error.value = "";
  try {
    const payload = {
      platform_type: connectionForm.platform_type,
      display_name: connectionForm.display_name,
      app_key: connectionForm.app_key || null,
      app_secret_ref: connectionForm.app_secret_ref || null,
      bot_id: connectionForm.bot_id || null,
      callback_url: connectionForm.callback_url || null,
      enabled: connectionForm.enabled,
      config_json: JSON.parse(connectionForm.config_json || "{}")
    };
    if (editingConnectionId.value) {
      await apiFetch(`/gateway/connections/${editingConnectionId.value}`, {
        method: "PATCH",
        body: JSON.stringify(payload)
      });
    } else {
      await apiFetch("/gateway/connections", {
        method: "POST",
        body: JSON.stringify(payload)
      });
    }
    resetConnectionForm();
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  } finally {
    savingConnection.value = false;
  }
}

async function submitRule() {
  savingRule.value = true;
  error.value = "";
  try {
    const payload = {
      connection_id: ruleForm.connection_id,
      rule_name: ruleForm.rule_name,
      source_scope: ruleForm.source_scope,
      source_id: ruleForm.source_id || null,
      keyword: ruleForm.keyword || null,
      conversation_id: ruleForm.conversation_id || null,
      default_model: ruleForm.default_model || null,
      execution_backend: ruleForm.execution_backend || null,
      enabled: ruleForm.enabled
    };
    if (editingRuleId.value) {
      await apiFetch(`/gateway/rules/${editingRuleId.value}`, {
        method: "PATCH",
        body: JSON.stringify(payload)
      });
    } else {
      await apiFetch("/gateway/rules", {
        method: "POST",
        body: JSON.stringify(payload)
      });
    }
    resetRuleForm();
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  } finally {
    savingRule.value = false;
  }
}

function editConnection(item) {
  editingConnectionId.value = item.id;
  connectionForm.platform_type = item.platform_type || "";
  connectionForm.display_name = item.display_name || "";
  connectionForm.app_key = item.app_key || "";
  connectionForm.app_secret_ref = item.app_secret_ref || "";
  connectionForm.bot_id = item.bot_id || "";
  connectionForm.callback_url = item.callback_url || "";
  connectionForm.enabled = item.enabled !== false;
  connectionForm.config_json = safeConfigJson(item.config_json);
}

function editRule(item) {
  editingRuleId.value = item.id;
  ruleForm.connection_id = item.connection_id || "";
  ruleForm.rule_name = item.rule_name || "";
  ruleForm.source_scope = item.source_scope || "all";
  ruleForm.source_id = item.source_id || "";
  ruleForm.keyword = item.keyword || "";
  ruleForm.conversation_id = item.conversation_id || "";
  ruleForm.default_model = item.default_model || "";
  ruleForm.execution_backend = item.execution_backend || "";
  ruleForm.enabled = item.enabled !== false;
}

async function toggleConnection(item) {
  error.value = "";
  try {
    await apiFetch(`/gateway/connections/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !item.enabled })
    });
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  }
}

async function toggleRule(item) {
  error.value = "";
  try {
    await apiFetch(`/gateway/rules/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !item.enabled })
    });
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  }
}

async function deleteConnection(item) {
  if (!window.confirm(t("gateway_confirm_delete_connection", { name: item.display_name }))) return;
  error.value = "";
  try {
    await apiFetch(`/gateway/connections/${item.id}`, { method: "DELETE" });
    if (editingConnectionId.value === item.id) resetConnectionForm();
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  }
}

async function deleteRule(item) {
  if (!window.confirm(t("gateway_confirm_delete_rule", { name: item.rule_name }))) return;
  error.value = "";
  try {
    await apiFetch(`/gateway/rules/${item.id}`, { method: "DELETE" });
    if (editingRuleId.value === item.id) resetRuleForm();
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  }
}

onMounted(loadGateway);
</script>

<template>
  <div class="page-grid gateway-grid">
    <section class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h2>{{ t("gateway_title") }}</h2>
          <p>{{ t("gateway_desc") }}</p>
        </div>
        <button class="secondary-button" type="button" @click="loadGateway">{{ t("refresh") }}</button>
      </div>
      <div class="info-box">
        <strong>{{ t("status") }}:</strong> {{ gatewayStatus }}
      </div>
      <p v-if="loading">{{ t("loading") }}</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card">
      <div class="panel-head compact-head">
        <div>
          <h3>{{ editingConnectionId ? t("gateway_update_connection") : t("gateway_create_connection") }}</h3>
          <p class="muted-text">{{ t("gateway_connections") }}</p>
        </div>
        <button v-if="editingConnectionId" class="secondary-button slim-button" type="button" @click="resetConnectionForm">
          {{ t("gateway_cancel_edit") }}
        </button>
      </div>
      <div class="form-stack">
        <label class="field-label">
          {{ t("gateway_platform_type") }}
          <select v-model="connectionForm.platform_type" class="text-input">
            <option value="" disabled>{{ t("gateway_select_platform") }}</option>
            <option v-for="item in options.platform_types" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="field-label">
          {{ t("display_name") }}
          <input v-model="connectionForm.display_name" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_app_key") }}
          <input v-model="connectionForm.app_key" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_secret_ref") }}
          <input v-model="connectionForm.app_secret_ref" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_bot_id") }}
          <input v-model="connectionForm.bot_id" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_callback_url") }}
          <input v-model="connectionForm.callback_url" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_config_json") }}
          <textarea v-model="connectionForm.config_json" class="text-area" rows="4" />
        </label>
        <label class="checkbox-row">
          <input v-model="connectionForm.enabled" type="checkbox" />
          <span>{{ t("gateway_enabled") }}</span>
        </label>
        <button
          class="primary-button"
          type="button"
          :disabled="savingConnection || !connectionForm.platform_type || !connectionForm.display_name"
          @click="submitConnection"
        >
          {{ savingConnection ? t("saving") : editingConnectionId ? t("gateway_update_connection") : t("gateway_create_connection") }}
        </button>
      </div>
    </section>

    <section class="panel-card">
      <div class="panel-head compact-head">
        <div>
          <h3>{{ editingRuleId ? t("gateway_update_rule") : t("gateway_create_rule") }}</h3>
          <p class="muted-text">{{ t("gateway_rules") }}</p>
        </div>
        <button v-if="editingRuleId" class="secondary-button slim-button" type="button" @click="resetRuleForm">
          {{ t("gateway_cancel_edit") }}
        </button>
      </div>
      <div class="form-stack">
        <label class="field-label">
          {{ t("connection") }}
          <select v-model="ruleForm.connection_id" class="text-input">
            <option value="" disabled>{{ t("gateway_connection_required") }}</option>
            <option v-for="item in connections" :key="item.id" :value="item.id">
              {{ item.display_name }} / {{ item.platform_type }}
            </option>
          </select>
        </label>
        <label class="field-label">
          {{ t("gateway_rule_name") }}
          <input v-model="ruleForm.rule_name" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_source_scope") }}
          <select v-model="ruleForm.source_scope" class="text-input">
            <option value="private">{{ t("gateway_scope_private") }}</option>
            <option value="group">{{ t("gateway_scope_group") }}</option>
            <option value="all">{{ t("gateway_scope_all") }}</option>
          </select>
        </label>
        <label class="field-label">
          {{ t("gateway_source_id") }}
          <input v-model="ruleForm.source_id" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_keyword") }}
          <input v-model="ruleForm.keyword" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_bind_conversation") }}
          <select v-model="ruleForm.conversation_id" class="text-input">
            <option value="">{{ t("gateway_select_conversation_optional") }}</option>
            <option v-for="item in options.conversations" :key="item.id" :value="item.id">
              {{ item.title }}
            </option>
          </select>
        </label>
        <label class="field-label">
          {{ t("gateway_default_model") }}
          <input v-model="ruleForm.default_model" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("gateway_execution_backend") }}
          <input v-model="ruleForm.execution_backend" class="text-input" />
        </label>
        <label class="checkbox-row">
          <input v-model="ruleForm.enabled" type="checkbox" />
          <span>{{ t("gateway_enabled") }}</span>
        </label>
        <button
          class="primary-button"
          type="button"
          :disabled="savingRule || !ruleForm.connection_id || !ruleForm.rule_name"
          @click="submitRule"
        >
          {{ savingRule ? t("saving") : editingRuleId ? t("gateway_update_rule") : t("gateway_create_rule") }}
        </button>
      </div>
    </section>

    <section class="panel-card wide-card">
      <h3>{{ t("gateway_connections") }}</h3>
      <ul class="data-list">
        <li v-if="!connections.length" class="muted-text">{{ t("gateway_no_connections") }}</li>
        <li v-for="item in connections" :key="item.id" class="data-row gateway-row">
          <div>
            <strong>{{ item.display_name }}</strong>
            <div class="muted-text">{{ item.platform_type }} / {{ item.callback_url || "-" }}</div>
            <div class="muted-text">{{ t("gateway_bot_id") }}: {{ item.bot_id || "-" }}</div>
          </div>
          <div class="button-row">
            <span :class="item.enabled ? 'status-chip is-active' : 'status-chip is-paused'">
              {{ item.enabled ? t("enable") : t("disable") }}
            </span>
            <button class="secondary-button slim-button" type="button" @click="editConnection(item)">{{ t("gateway_edit") }}</button>
            <button class="secondary-button slim-button" type="button" @click="toggleConnection(item)">
              {{ item.enabled ? t("disable") : t("enable") }}
            </button>
            <button class="secondary-button slim-button" type="button" @click="deleteConnection(item)">{{ t("delete") }}</button>
          </div>
        </li>
      </ul>
    </section>

    <section class="panel-card wide-card">
      <h3>{{ t("gateway_rules") }}</h3>
      <ul class="data-list">
        <li v-if="!rules.length" class="muted-text">{{ t("gateway_no_rules") }}</li>
        <li v-for="item in rules" :key="item.id" class="data-row gateway-row">
          <div>
            <strong>{{ item.rule_name }}</strong>
            <div class="muted-text">{{ item.source_scope }} / {{ item.keyword || "-" }}</div>
            <div class="muted-text">{{ item.default_model || "-" }} / {{ item.execution_backend || "-" }}</div>
          </div>
          <div class="button-row">
            <span :class="item.enabled ? 'status-chip is-active' : 'status-chip is-paused'">
              {{ item.enabled ? t("enable") : t("disable") }}
            </span>
            <button class="secondary-button slim-button" type="button" @click="editRule(item)">{{ t("gateway_edit") }}</button>
            <button class="secondary-button slim-button" type="button" @click="toggleRule(item)">
              {{ item.enabled ? t("disable") : t("enable") }}
            </button>
            <button class="secondary-button slim-button" type="button" @click="deleteRule(item)">{{ t("delete") }}</button>
          </div>
        </li>
      </ul>
    </section>

    <section class="panel-card wide-card">
      <h3>{{ t("gateway_events") }}</h3>
      <ul class="simple-list">
        <li v-if="!events.length" class="muted-text">{{ t("gateway_no_events") }}</li>
        <li v-for="item in events" :key="item.id" class="info-item">
          <div class="info-item-top">
            <strong>{{ item.event_type || "-" }}</strong>
            <span :class="item.status === 'success' ? 'status-chip is-active' : item.status === 'failed' ? 'status-chip is-failed' : 'status-chip'">
              {{ item.status || "-" }}
            </span>
          </div>
          <div class="muted-text">{{ t("gateway_event_source") }}: {{ item.platform_type }} / {{ item.source_id || "-" }}</div>
          <div class="muted-text">{{ item.created_at || "-" }}</div>
          <pre v-if="item.message_text" class="message-body task-log-body">{{ item.message_text }}</pre>
          <pre v-if="item.detail" class="message-body task-log-body">{{ JSON.stringify(item.detail, null, 2) }}</pre>
        </li>
      </ul>
    </section>
  </div>
</template>
