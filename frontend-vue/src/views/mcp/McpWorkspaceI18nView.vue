<script setup>
import { onMounted, reactive, ref } from "vue";

import { useI18n } from "../../i18n";
import { apiFetch } from "../../services/api";

const { t } = useI18n();
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const connections = ref([]);

const createForm = reactive({
  display_name: "",
  base_url: "",
  bearer_token: "",
  headers_json: "{}",
  query_params_json: "{}",
  timeout_seconds: 30,
  enabled: true
});

function parseJsonInput(raw, fieldLabel) {
  const value = String(raw || "").trim();
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error(`${fieldLabel} must be a JSON object`);
    }
    return parsed;
  } catch (err) {
    throw new Error(`${fieldLabel}: ${err.message}`);
  }
}

async function loadMcp() {
  loading.value = true;
  error.value = "";
  try {
    const connectionData = await apiFetch("/mcp/connections");
    connections.value = connectionData.items || [];
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
    const headers = parseJsonInput(createForm.headers_json, t("mcp_http_headers"));
    const queryParams = parseJsonInput(createForm.query_params_json, t("mcp_http_query_params"));
    await apiFetch("/mcp/connections", {
      method: "POST",
      body: JSON.stringify({
        server_key: "custom_http",
        display_name: createForm.display_name,
        scope: "user",
        base_url: createForm.base_url,
        bearer_token: createForm.bearer_token || null,
        headers,
        query_params: queryParams,
        timeout_seconds: Number(createForm.timeout_seconds || 30),
        enabled: createForm.enabled
      })
    });
    createForm.display_name = "";
    createForm.base_url = "";
    createForm.bearer_token = "";
    createForm.headers_json = "{}";
    createForm.query_params_json = "{}";
    createForm.timeout_seconds = 30;
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
    await apiFetch(`/mcp/connections/${item.id}`, { method: "DELETE" });
    await loadMcp();
  } catch (err) {
    error.value = err.message;
  }
}

onMounted(loadMcp);
</script>

<template>
  <div class="page-grid mcp-page-grid">
    <section class="panel-card wide-card mcp-page-head">
      <div class="panel-head">
        <div>
          <h2>{{ t("mcp_center_title") }}</h2>
          <p>{{ t("mcp_center_desc") }}</p>
        </div>
        <button class="secondary-button" type="button" @click="loadMcp">{{ t("refresh") }}</button>
      </div>
      <p v-if="loading">{{ t("loading") }}</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card mcp-list-card">
      <div class="panel-head compact-head">
        <div>
          <h3>{{ t("my_connections") }}</h3>
          <p class="muted-text">{{ t("mcp_user_list_desc") }}</p>
        </div>
      </div>
      <ul class="data-list">
        <li v-if="!connections.length" class="muted-text">{{ t("mcp_no_connections") }}</li>
        <li v-for="item in connections" :key="item.id" class="data-row mcp-connection-row">
          <div>
            <strong>{{ item.display_name }}</strong>
            <div class="muted-text">{{ item.base_url || "-" }}</div>
            <div class="muted-text">
              {{ t("mcp_http_headers") }} {{ Object.keys(item.headers || {}).length }} ·
              {{ t("mcp_http_query_params") }} {{ Object.keys(item.query_params || {}).length }}
            </div>
          </div>
          <div class="button-row">
            <button class="secondary-button slim-button" type="button" @click="toggleConnection(item)">
              {{ item.enabled ? t("disable") : t("enable") }}
            </button>
            <button class="secondary-button slim-button" type="button" @click="deleteConnection(item)">
              {{ t("delete") }}
            </button>
          </div>
        </li>
      </ul>
    </section>

    <section class="panel-card mcp-form-card">
      <h3>{{ t("create_connection") }}</h3>
      <div class="form-stack">
        <p class="muted-text">{{ t("mcp_http_desc") }}</p>
        <label class="field-label">
          {{ t("display_name") }}
          <input v-model="createForm.display_name" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("mcp_http_url") }}
          <input v-model="createForm.base_url" class="text-input" placeholder="https://example.com/mcp" />
        </label>
        <label class="field-label">
          {{ t("mcp_http_bearer_token") }}
          <textarea v-model="createForm.bearer_token" class="text-area" rows="3" />
        </label>
        <label class="field-label">
          {{ t("mcp_http_headers") }}
          <textarea v-model="createForm.headers_json" class="text-area" rows="6" placeholder='{"X-Api-Key":"value"}' />
        </label>
        <label class="field-label">
          {{ t("mcp_http_query_params") }}
          <textarea v-model="createForm.query_params_json" class="text-area" rows="6" placeholder='{"version":"2026-04"}' />
        </label>
        <label class="field-label">
          {{ t("mcp_http_timeout") }}
          <input v-model="createForm.timeout_seconds" class="text-input" type="number" min="1" max="300" />
        </label>
        <button class="primary-button" type="button" :disabled="saving || !createForm.display_name || !createForm.base_url" @click="createConnection">
          {{ saving ? t("saving") : t("save_connection") }}
        </button>
      </div>
    </section>
  </div>
</template>
