<script setup>
import { computed, onMounted, reactive, ref } from "vue";

import { apiFetch } from "../../services/api";

const loading = ref(false);
const saving = ref(false);
const error = ref("");
const servers = ref([]);
const connections = ref([]);
const audits = ref([]);
const conversations = ref([]);
const bindings = ref([]);
const selectedConversationId = ref("");

const createForm = reactive({
  server_key: "",
  display_name: "",
  credential_ref: "",
  config_json: "{}",
  enabled: true
});

const bindForm = reactive({
  connection_id: ""
});

const selectedConversationBindings = computed(() =>
  bindings.value.filter((item) => item.conversation_id === selectedConversationId.value)
);

async function loadMcp() {
  loading.value = true;
  error.value = "";
  try {
    const [serverData, connectionData, auditData, conversationData] = await Promise.all([
      apiFetch("/mcp/servers"),
      apiFetch("/mcp/connections"),
      apiFetch("/mcp/audit"),
      apiFetch("/conversations")
    ]);
    servers.value = serverData.items || [];
    connections.value = connectionData.items || [];
    audits.value = auditData.items || [];
    conversations.value = conversationData.items || [];
    if (!selectedConversationId.value && conversations.value.length) {
      selectedConversationId.value = conversations.value[0].id;
    }
    await loadBindings();
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function loadBindings() {
  if (!selectedConversationId.value) {
    bindings.value = [];
    return;
  }
  try {
    const data = await apiFetch(`/mcp/conversations/${selectedConversationId.value}/bindings`);
    bindings.value = (data.items || []).map((item) => {
      const connection = connections.value.find((entry) => entry.id === item.connection_id);
      return {
        ...item,
        display_name: connection?.display_name || item.connection_id,
        server_key: connection?.server_key || ""
      };
    });
  } catch (err) {
    error.value = err.message;
  }
}

async function createConnection() {
  saving.value = true;
  error.value = "";
  try {
    await apiFetch("/mcp/connections", {
      method: "POST",
      body: JSON.stringify({
        server_key: createForm.server_key,
        display_name: createForm.display_name,
        scope: "user",
        credential_ref: createForm.credential_ref || null,
        config_json: JSON.parse(createForm.config_json || "{}"),
        enabled: createForm.enabled
      })
    });
    createForm.display_name = "";
    createForm.credential_ref = "";
    createForm.config_json = "{}";
    await loadMcp();
  } catch (err) {
    error.value = err.message;
  } finally {
    saving.value = false;
  }
}

async function toggleConnection(item) {
  error.value = "";
  try {
    await apiFetch(`/mcp/connections/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !item.enabled })
    });
    await loadMcp();
  } catch (err) {
    error.value = err.message;
  }
}

async function deleteConnection(item) {
  error.value = "";
  try {
    await apiFetch(`/mcp/connections/${item.id}`, {
      method: "DELETE"
    });
    await loadMcp();
  } catch (err) {
    error.value = err.message;
  }
}

async function bindConnection() {
  if (!selectedConversationId.value || !bindForm.connection_id) return;
  error.value = "";
  try {
    await apiFetch(`/mcp/conversations/${selectedConversationId.value}/bindings`, {
      method: "POST",
      body: JSON.stringify({
        connection_id: bindForm.connection_id,
        enabled: true
      })
    });
    bindForm.connection_id = "";
    await loadBindings();
    await loadMcp();
  } catch (err) {
    error.value = err.message;
  }
}

async function unbindConnection(bindingId) {
  error.value = "";
  try {
    await apiFetch(`/mcp/bindings/${bindingId}`, {
      method: "DELETE"
    });
    await loadBindings();
    await loadMcp();
  } catch (err) {
    error.value = err.message;
  }
}

onMounted(loadMcp);
</script>

<template>
  <div class="page-grid">
    <section class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h2>MCP Center</h2>
          <p>Create personal MCP connections, bind them to conversations, and review audit events.</p>
        </div>
        <button class="secondary-button" type="button" @click="loadMcp">
          Refresh
        </button>
      </div>
      <p v-if="loading">Loading...</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card">
      <h3>Create Connection</h3>
      <div class="form-stack">
        <label class="field-label">
          Server
          <select v-model="createForm.server_key" class="text-input">
            <option value="" disabled>Select a server</option>
            <option v-for="item in servers" :key="item.id" :value="item.key">
              {{ item.display_name }} / {{ item.server_type }}
            </option>
          </select>
        </label>
        <label class="field-label">
          Display Name
          <input v-model="createForm.display_name" class="text-input" />
        </label>
        <label class="field-label">
          Credential Ref
          <input v-model="createForm.credential_ref" class="text-input" />
        </label>
        <label class="field-label">
          Config JSON
          <textarea v-model="createForm.config_json" class="text-area" rows="4" />
        </label>
        <button class="primary-button" type="button" :disabled="saving || !createForm.server_key" @click="createConnection">
          {{ saving ? "Saving..." : "Create Connection" }}
        </button>
      </div>
    </section>

    <section class="panel-card">
      <h3>Bind to Conversation</h3>
      <div class="form-stack">
        <label class="field-label">
          Conversation
          <select v-model="selectedConversationId" class="text-input" @change="loadBindings">
            <option value="" disabled>Select a conversation</option>
            <option v-for="item in conversations" :key="item.id" :value="item.id">
              {{ item.title }}
            </option>
          </select>
        </label>
        <label class="field-label">
          Connection
          <select v-model="bindForm.connection_id" class="text-input">
            <option value="" disabled>Select a connection</option>
            <option v-for="item in connections" :key="item.id" :value="item.id">
              {{ item.display_name }} / {{ item.server_key }}
            </option>
          </select>
        </label>
        <button class="primary-button" type="button" :disabled="!selectedConversationId || !bindForm.connection_id" @click="bindConnection">
          Bind Connection
        </button>
      </div>

      <h4>Current Bindings</h4>
      <ul class="simple-list">
        <li v-for="item in selectedConversationBindings" :key="item.id" class="data-row">
          <div>
            <strong>{{ item.display_name }}</strong>
            <div class="muted-text">{{ item.server_key }}</div>
          </div>
          <button class="secondary-button slim-button" type="button" @click="unbindConnection(item.id)">
            Unbind
          </button>
        </li>
      </ul>
    </section>

    <section class="panel-card wide-card">
      <h3>My Connections</h3>
      <ul class="data-list">
        <li v-for="item in connections" :key="item.id" class="data-row">
          <div>
            <strong>{{ item.display_name }}</strong>
            <div class="muted-text">
              {{ item.server_key }} / enabled={{ item.enabled }}
            </div>
          </div>
          <div class="button-row">
            <button class="secondary-button slim-button" type="button" @click="toggleConnection(item)">
              {{ item.enabled ? "Disable" : "Enable" }}
            </button>
            <button class="secondary-button slim-button" type="button" @click="deleteConnection(item)">
              Delete
            </button>
          </div>
        </li>
      </ul>
    </section>

    <section class="panel-card wide-card">
      <h3>Audit Logs</h3>
      <ul class="simple-list">
        <li v-for="item in audits" :key="item.id">
          <strong>{{ item.action }}</strong>
          <span class="muted-text"> / {{ item.connection_id }} / {{ item.created_at }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>
