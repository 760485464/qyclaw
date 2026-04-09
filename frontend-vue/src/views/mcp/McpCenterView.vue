<script setup>
import { onMounted, ref } from "vue";

import { apiFetch } from "../../services/api";

const loading = ref(false);
const error = ref("");
const servers = ref([]);
const connections = ref([]);

async function loadMcp() {
  loading.value = true;
  error.value = "";
  try {
    const [serverData, connectionData] = await Promise.all([
      apiFetch("/mcp/servers"),
      apiFetch("/mcp/connections")
    ]);
    servers.value = serverData.items || [];
    connections.value = connectionData.items || [];
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

onMounted(loadMcp);
</script>

<template>
  <div class="page-grid">
    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h2>MCP 中心</h2>
          <p>第一版先落服务目录和我的连接总览，后续再补创建、编辑、审计。</p>
        </div>
        <button class="secondary-button" type="button" @click="loadMcp">
          刷新
        </button>
      </div>
      <p v-if="loading">加载中...</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card">
      <h3>MCP 服务目录</h3>
      <ul class="simple-list">
        <li v-for="item in servers" :key="item.id">
          {{ item.display_name }} / {{ item.server_type }}
        </li>
      </ul>
    </section>

    <section class="panel-card">
      <h3>我的 MCP 连接</h3>
      <ul class="simple-list">
        <li v-for="item in connections" :key="item.id">
          {{ item.display_name }} / {{ item.server_key }}
        </li>
      </ul>
    </section>
  </div>
</template>
