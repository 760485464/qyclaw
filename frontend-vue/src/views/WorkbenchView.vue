<script setup>
import { onMounted, ref } from "vue";

import { apiFetch } from "../services/api";

const loading = ref(false);
const error = ref("");
const summary = ref({
  recent_conversations: [],
  recent_skills: [],
  recent_mcp_connections: [],
  recent_scheduled_tasks: [],
  recent_attachments: [],
  stats: {
    conversation_count: 0,
    skill_count: 0,
    mcp_connection_count: 0,
    scheduled_task_count: 0,
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
          <h2>工作台</h2>
          <p>当前阶段先基于现有接口提供总览，后续再切到专门的聚合接口。</p>
        </div>
        <button class="secondary-button" type="button" @click="loadSummary">
          刷新
        </button>
      </div>

      <p v-if="loading">加载中...</p>
      <p v-else-if="error" class="error-text">{{ error }}</p>

      <div v-else class="stats-grid">
        <article class="stat-card">
          <strong>{{ summary.conversations.length }}</strong>
          <span>会话数</span>
        </article>
        <article class="stat-card">
          <strong>{{ summary.mySkills.length }}</strong>
          <span>我的技能</span>
        </article>
        <article class="stat-card">
          <strong>{{ summary.pendingSkills.length }}</strong>
          <span>待审核技能</span>
        </article>
        <article class="stat-card">
          <strong>{{ summary.mcpConnections.length }}</strong>
          <span>MCP 连接</span>
        </article>
      </div>
    </section>

    <section class="panel-card">
      <h3>最近会话</h3>
      <ul class="simple-list">
        <li v-for="item in summary.conversations.slice(0, 6)" :key="item.id">
          <RouterLink :to="`/conversations/${item.id}`">{{ item.title }}</RouterLink>
        </li>
      </ul>
    </section>

    <section class="panel-card">
      <h3>最近技能</h3>
      <ul class="simple-list">
        <li v-for="item in summary.mySkills.slice(0, 6)" :key="item.id">
          {{ item.display_name || item.name }} / {{ item.status }}
        </li>
      </ul>
    </section>
  </div>
</template>
