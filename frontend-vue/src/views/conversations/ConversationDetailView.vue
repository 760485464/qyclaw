<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { apiFetch, apiUpload } from "../../services/api";

const route = useRoute();
const conversationId = computed(() => route.params.id);

const loading = ref(false);
const sending = ref(false);
const uploading = ref(false);
const error = ref("");
const messages = ref([]);
const attachments = ref([]);
const pendingInterruptId = ref("");
const input = ref("");
const files = ref([]);

async function loadMessages() {
  loading.value = true;
  error.value = "";
  try {
    const [messageData, attachmentData, interruptData] = await Promise.all([
      apiFetch(`/conversations/${conversationId.value}/messages`),
      apiFetch(`/conversations/${conversationId.value}/attachments`),
      apiFetch(`/conversations/${conversationId.value}/interrupts/pending`)
    ]);
    messages.value = messageData.items || [];
    attachments.value = attachmentData.items || [];
    pendingInterruptId.value = interruptData.interrupt_id || "";
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function sendMessage() {
  if (!input.value.trim()) return;
  sending.value = true;
  error.value = "";
  try {
    await apiFetch(`/conversations/${conversationId.value}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: input.value.trim() })
    });
    input.value = "";
    await loadMessages();
  } catch (err) {
    error.value = err.message;
  } finally {
    sending.value = false;
  }
}

async function uploadFiles() {
  const selectedFiles = Array.from(files.value || []);
  if (!selectedFiles.length) return;
  uploading.value = true;
  error.value = "";
  try {
    for (const file of selectedFiles) {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("convert_to_markdown", "true");
      await apiUpload(`/conversations/${conversationId.value}/attachments`, formData);
    }
    files.value = [];
    await loadMessages();
  } catch (err) {
    error.value = err.message;
  } finally {
    uploading.value = false;
  }
}

async function decideInterrupt(decision) {
  if (!pendingInterruptId.value) return;
  error.value = "";
  try {
    await apiFetch(
      `/conversations/${conversationId.value}/interrupts/${pendingInterruptId.value}/decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision })
      }
    );
    await loadMessages();
  } catch (err) {
    error.value = err.message;
  }
}

watch(conversationId, loadMessages);
onMounted(loadMessages);
</script>

<template>
  <div class="conversation-layout">
    <section class="panel-card chat-panel">
      <div class="panel-head">
        <div>
          <h2>会话详情</h2>
          <p>第一版已接入消息列表、发送消息、审批与文件上传。</p>
        </div>
        <button class="secondary-button" type="button" @click="loadMessages">
          刷新
        </button>
      </div>

      <div v-if="pendingInterruptId" class="warning-box">
        <strong>存在待审批操作：</strong>{{ pendingInterruptId }}
        <div class="button-row">
          <button class="primary-button" type="button" @click="decideInterrupt('allow')">
            allow
          </button>
          <button class="secondary-button" type="button" @click="decideInterrupt('reject')">
            reject
          </button>
          <button class="secondary-button" type="button" @click="decideInterrupt('allow_all')">
            allow_all
          </button>
        </div>
      </div>

      <div class="message-list">
        <article v-for="item in messages" :key="item.id" class="message-card">
          <div class="message-meta">
            <span>{{ item.sender_role }}</span>
            <span>{{ item.message_type }}</span>
            <span>{{ item.message_status }}</span>
          </div>
          <pre class="message-body">{{ item.content_md }}</pre>
        </article>
      </div>

      <div class="composer-card">
        <textarea
          v-model="input"
          class="text-area"
          rows="4"
          placeholder="输入你的任务或问题"
        />
        <div class="button-row">
          <button class="primary-button" type="button" :disabled="sending" @click="sendMessage">
            {{ sending ? "发送中..." : "发送消息" }}
          </button>
        </div>
      </div>

      <p v-if="loading">加载中...</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card sidebar-panel">
      <h3>文件资料区</h3>
      <p class="muted-text">会话资料上传已接入现有后端接口。</p>

      <div class="form-stack">
        <label class="field-label">
          上传资料
          <input
            class="text-input"
            type="file"
            multiple
            @change="files = $event.target.files"
          />
        </label>
        <button class="primary-button" type="button" :disabled="uploading" @click="uploadFiles">
          {{ uploading ? "上传中..." : "上传并解析" }}
        </button>
      </div>

      <h4>附件列表</h4>
      <ul class="simple-list">
        <li v-for="item in attachments" :key="item.workspace_path">
          <strong>{{ item.original_name || item.saved_name }}</strong>
          <div class="muted-text">{{ item.workspace_path }}</div>
        </li>
      </ul>
    </section>
  </div>
</template>
