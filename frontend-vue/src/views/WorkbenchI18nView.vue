<script setup>
import { onMounted, ref } from "vue";

import { useI18n } from "../i18n";
import { apiFetch } from "../services/api";

const { t } = useI18n();
const loading = ref(false);
const error = ref("");
const summary = ref({
  recent_conversations: [],
  recent_skills: [],
  recent_attachments: [],
  stats: {
    conversation_count: 0,
    skill_count: 0,
    mcp_connection_count: 0,
    pending_interrupt_count: 0
  },
  gateway: {
    status: "not_configured",
    summary: ""
  }
});

async function loadSummary() {
  loading.value = true;
  error.value = "";
  try {
    summary.value = await apiFetch("/dashboard/summary");
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

onMounted(loadSummary);
</script>

<template>
  <div class="page-grid">
    <section class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h2>{{ t("workbench_title") }}</h2>
          <p>{{ t("workbench_desc") }}</p>
        </div>
        <button class="secondary-button" type="button" @click="loadSummary">
          {{ t("refresh") }}
        </button>
      </div>

      <p v-if="loading">{{ t("loading") }}</p>
      <p v-else-if="error" class="error-text">{{ error }}</p>

      <div v-else class="stats-grid">
        <article class="stat-card">
          <strong>{{ summary.stats.conversation_count }}</strong>
          <span>{{ t("conversations") }}</span>
        </article>
        <article class="stat-card">
          <strong>{{ summary.stats.skill_count }}</strong>
          <span>{{ t("my_skills") }}</span>
        </article>
        <article class="stat-card">
          <strong>{{ summary.stats.pending_interrupt_count }}</strong>
          <span>{{ t("pending_approvals") }}</span>
        </article>
        <article class="stat-card">
          <strong>{{ summary.stats.mcp_connection_count }}</strong>
          <span>{{ t("mcp_connections") }}</span>
        </article>
      </div>
    </section>

    <section class="panel-card">
      <h3>{{ t("recent_conversations") }}</h3>
      <ul class="simple-list">
        <li v-for="item in summary.recent_conversations" :key="item.id">
          <RouterLink :to="`/conversations/${item.id}`">{{ item.title }}</RouterLink>
        </li>
      </ul>
    </section>

    <section class="panel-card">
      <h3>{{ t("recent_skills") }}</h3>
      <ul class="simple-list">
        <li v-for="item in summary.recent_skills" :key="item.id">
          {{ item.display_name || item.name }} / {{ item.status }}
        </li>
      </ul>
    </section>

    <section class="panel-card">
      <h3>{{ t("recent_attachments") }}</h3>
      <ul class="simple-list">
        <li v-for="item in summary.recent_attachments" :key="`${item.message_id}-${item.workspace_path}`">
          {{ item.original_name || item.saved_name }}
        </li>
      </ul>
    </section>

    <section class="panel-card">
      <h3>{{ t("gateway_status") }}</h3>
      <p class="muted-text">{{ summary.gateway.summary }}</p>
      <div class="info-box">
        <strong>{{ t("status") }}:</strong> {{ summary.gateway.status }}
      </div>
    </section>
  </div>
</template>
