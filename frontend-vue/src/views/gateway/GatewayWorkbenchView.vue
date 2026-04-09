<script setup>
import { onMounted, reactive, ref } from "vue";

import { apiFetch } from "../../services/api";

const loading = ref(false);
const saving = ref(false);
const error = ref("");
const connections = ref([]);
const rules = ref([]);
const events = ref([]);
const options = ref({
  platform_types: [],
  conversations: []
});

const connectionForm = reactive({
  platform_type: "dingtalk",
  display_name: "",
  app_key: "",
  app_secret_ref: "",
  bot_id: "",
  callback_url: "",
  config_json: "{}"
});

const ruleForm = reactive({
  connection_id: "",
  rule_name: "",
  source_scope: "private",
  source_id: "",
  keyword: "",
  conversation_id: "",
  default_model: "",
  execution_backend: "deepagents"
});

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
    if (!ruleForm.connection_id && connections.value.length) {
      ruleForm.connection_id = connections.value[0].id;
    }
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function createConnection() {
  saving.value = true;
  error.value = "";
  try {
    await apiFetch("/gateway/connections", {
      method: "POST",
      body: JSON.stringify({
        ...connectionForm,
        config_json: JSON.parse(connectionForm.config_json || "{}")
      })
    });
    connectionForm.display_name = "";
    connectionForm.app_key = "";
    connectionForm.app_secret_ref = "";
    connectionForm.bot_id = "";
    connectionForm.callback_url = "";
    connectionForm.config_json = "{}";
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  } finally {
    saving.value = false;
  }
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

async function deleteConnection(item) {
  error.value = "";
  try {
    await apiFetch(`/gateway/connections/${item.id}`, { method: "DELETE" });
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  }
}

async function createRule() {
  if (!ruleForm.connection_id) return;
  saving.value = true;
  error.value = "";
  try {
    await apiFetch("/gateway/rules", {
      method: "POST",
      body: JSON.stringify({
        ...ruleForm,
        source_id: ruleForm.source_id || null,
        keyword: ruleForm.keyword || null,
        conversation_id: ruleForm.conversation_id || null,
        default_model: ruleForm.default_model || null
      })
    });
    ruleForm.rule_name = "";
    ruleForm.source_id = "";
    ruleForm.keyword = "";
    ruleForm.conversation_id = "";
    ruleForm.default_model = "";
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  } finally {
    saving.value = false;
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

async function deleteRule(item) {
  error.value = "";
  try {
    await apiFetch(`/gateway/rules/${item.id}`, { method: "DELETE" });
    await loadGateway();
  } catch (err) {
    error.value = err.message;
  }
}
</script>

<template>
  <div class="page-grid">
    <section class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h2>Gateway</h2>
          <p>Manage platform connections, route rules, and recent gateway events.</p>
        </div>
        <button class="secondary-button" type="button" @click="loadGateway">Refresh</button>
      </div>
      <p v-if="loading">Loading...</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card">
      <h3>Platform Connection</h3>
      <div class="form-stack">
        <label class="field-label">
          Platform
          <select v-model="connectionForm.platform_type" class="text-input">
            <option v-for="item in options.platform_types" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="field-label">
          Display Name
          <input v-model="connectionForm.display_name" class="text-input" />
        </label>
        <label class="field-label">
          App Key
          <input v-model="connectionForm.app_key" class="text-input" />
        </label>
        <label class="field-label">
          Secret Ref
          <input v-model="connectionForm.app_secret_ref" class="text-input" />
        </label>
        <label class="field-label">
          Bot ID
          <input v-model="connectionForm.bot_id" class="text-input" />
        </label>
        <label class="field-label">
          Callback URL
          <input v-model="connectionForm.callback_url" class="text-input" />
        </label>
        <label class="field-label">
          Config JSON
          <textarea v-model="connectionForm.config_json" class="text-area" rows="4" />
        </label>
        <button class="primary-button" type="button" :disabled="saving" @click="createConnection">
          {{ saving ? "Saving..." : "Create Connection" }}
        </button>
      </div>
    </section>

    <section class="panel-card">
      <h3>Route Rule</h3>
      <div class="form-stack">
        <label class="field-label">
          Connection
          <select v-model="ruleForm.connection_id" class="text-input">
            <option value="" disabled>Select a connection</option>
            <option v-for="item in connections" :key="item.id" :value="item.id">{{ item.display_name }}</option>
          </select>
        </label>
        <label class="field-label">
          Rule Name
          <input v-model="ruleForm.rule_name" class="text-input" />
        </label>
        <label class="field-label">
          Source Scope
          <select v-model="ruleForm.source_scope" class="text-input">
            <option value="private">private</option>
            <option value="group">group</option>
            <option value="all">all</option>
          </select>
        </label>
        <label class="field-label">
          Source ID
          <input v-model="ruleForm.source_id" class="text-input" />
        </label>
        <label class="field-label">
          Keyword
          <input v-model="ruleForm.keyword" class="text-input" />
        </label>
        <label class="field-label">
          Conversation
          <select v-model="ruleForm.conversation_id" class="text-input">
            <option value="">No binding</option>
            <option v-for="item in options.conversations" :key="item.id" :value="item.id">{{ item.title }}</option>
          </select>
        </label>
        <label class="field-label">
          Default Model
          <input v-model="ruleForm.default_model" class="text-input" />
        </label>
        <button class="primary-button" type="button" :disabled="saving || !ruleForm.connection_id" @click="createRule">
          {{ saving ? "Saving..." : "Create Rule" }}
        </button>
      </div>
    </section>

    <section class="panel-card wide-card">
      <h3>Connections</h3>
      <ul class="data-list">
        <li v-for="item in connections" :key="item.id" class="data-row">
          <div>
            <strong>{{ item.display_name }}</strong>
            <div class="muted-text">{{ item.platform_type }} / enabled={{ item.enabled }}</div>
          </div>
          <div class="button-row">
            <button class="secondary-button slim-button" type="button" @click="toggleConnection(item)">
              {{ item.enabled ? "Disable" : "Enable" }}
            </button>
            <button class="secondary-button slim-button" type="button" @click="deleteConnection(item)">Delete</button>
          </div>
        </li>
      </ul>
    </section>

    <section class="panel-card wide-card">
      <h3>Rules</h3>
      <ul class="data-list">
        <li v-for="item in rules" :key="item.id" class="data-row">
          <div>
            <strong>{{ item.rule_name }}</strong>
            <div class="muted-text">
              {{ item.source_scope }} / keyword={{ item.keyword || "-" }} / enabled={{ item.enabled }}
            </div>
          </div>
          <div class="button-row">
            <button class="secondary-button slim-button" type="button" @click="toggleRule(item)">
              {{ item.enabled ? "Disable" : "Enable" }}
            </button>
            <button class="secondary-button slim-button" type="button" @click="deleteRule(item)">Delete</button>
          </div>
        </li>
      </ul>
    </section>

    <section class="panel-card wide-card">
      <h3>Event Logs</h3>
      <ul class="simple-list">
        <li v-for="item in events" :key="item.id">
          <strong>{{ item.event_type }}</strong>
          <span class="muted-text"> / {{ item.platform_type }} / {{ item.status }} / {{ item.created_at }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>
