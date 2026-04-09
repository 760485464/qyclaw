<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useI18n } from "../../i18n";
import { apiFetch } from "../../services/api";

const { t } = useI18n();
const router = useRouter();
const loading = ref(false);
const creating = ref(false);
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
  creating.value = true;
  error.value = "";
  try {
    const data = await apiFetch("/conversations", {
      method: "POST",
      body: JSON.stringify({
        title: createForm.title || t("conversation_center"),
        model: createForm.model,
        execution_backend: createForm.execution_backend,
        skills: [],
        tools: []
      })
    });
    router.push(`/conversations/${data.id}`);
  } catch (err) {
    error.value = err.message;
  } finally {
    creating.value = false;
  }
}

onMounted(loadConversations);
</script>

<template>
  <div class="page-grid conversations-page">
    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h2>{{ t("conversation_center") }}</h2>
          <p>{{ t("conversation_center_desc") }}</p>
        </div>
        <button class="secondary-button" type="button" @click="loadConversations">
          {{ t("refresh") }}
        </button>
      </div>

      <div class="form-stack compact-form">
        <label class="field-label">
          {{ t("title") }}
          <input v-model="createForm.title" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("model") }}
          <input v-model="createForm.model" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("backend") }}
          <select v-model="createForm.execution_backend" class="text-input">
            <option value="deepagents">{{ t("backend_deepagents") }}</option>
            <option value="claude">{{ t("backend_claude_agent") }}</option>
          </select>
        </label>
        <button class="primary-button" type="button" :disabled="creating" @click="createConversation">
          {{ creating ? t("creating") : t("create_and_open") }}
        </button>
      </div>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card wide-card">
      <h3>{{ t("conversations") }}</h3>
      <p v-if="loading">{{ t("loading") }}</p>
      <ul v-else class="data-list">
        <li v-for="item in conversations" :key="item.id" class="data-row">
          <div>
            <strong>{{ item.title }}</strong>
            <div class="muted-text">
              model={{ item.model }} / backend={{ item.execution_backend }}
            </div>
          </div>
          <RouterLink class="text-link" :to="`/conversations/${item.id}`">
            {{ t("open") }}
          </RouterLink>
        </li>
      </ul>
    </section>
  </div>
</template>
