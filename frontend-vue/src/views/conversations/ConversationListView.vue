<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { apiFetch } from "../../services/api";

const router = useRouter();
const loading = ref(false);
const error = ref("");
const conversations = ref([]);

const createForm = reactive({
  title: "",
  model: "default",
  execution_backend: "deepagents"
});

async function loadConversations() {
  loading.value = true;
  error.value = "";
  try {
    const data = await apiFetch("/conversations");
    conversations.value = data.items || [];
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function createConversation() {
  error.value = "";
  try {
    const data = await apiFetch("/conversations", {
      method: "POST",
      body: JSON.stringify({
        title: createForm.title || "新会话",
        model: createForm.model,
        execution_backend: createForm.execution_backend,
        skills: [],
        tools: []
      })
    });
    router.push(`/conversations/${data.id}`);
  } catch (err) {
    error.value = err.message;
  }
}

onMounted(loadConversations);
</script>

<template>
  <div class="page-grid conversations-page">
    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h2>会话中心</h2>
          <p>先打通列表、新建和进入详情页。</p>
        </div>
        <button class="secondary-button" type="button" @click="loadConversations">
          刷新
        </button>
      </div>

      <div class="form-stack compact-form">
        <label class="field-label">
          标题
          <input v-model="createForm.title" class="text-input" />
        </label>
        <label class="field-label">
          模型
          <input v-model="createForm.model" class="text-input" />
        </label>
        <label class="field-label">
          后端
          <select v-model="createForm.execution_backend" class="text-input">
            <option value="deepagents">deepagents</option>
            <option value="claude">claude</option>
          </select>
        </label>
        <button class="primary-button" type="button" @click="createConversation">
          创建并进入
        </button>
      </div>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card wide-card">
      <h3>会话列表</h3>
      <p v-if="loading">加载中...</p>
      <ul v-else class="data-list">
        <li v-for="item in conversations" :key="item.id" class="data-row">
          <div>
            <strong>{{ item.title }}</strong>
            <div class="muted-text">
              model={{ item.model }} / backend={{ item.execution_backend }}
            </div>
          </div>
          <RouterLink class="text-link" :to="`/conversations/${item.id}`">
            进入
          </RouterLink>
        </li>
      </ul>
    </section>
  </div>
</template>
