<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { API_BASE, apiFetch, apiUpload, handleUnauthorizedResponse, useBlobDownload } from "../../services/api";
import { useSessionStore } from "../../stores/session";

const route = useRoute();
const session = useSessionStore();
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
const previewOpen = ref(false);
const previewTitle = ref("");
const previewContent = ref("");
const previewTruncated = ref(false);
const previewLoading = ref(false);
const assistantPlaceholderId = ref("");
const assistantPhase = ref("queued");

let eventSource = null;
let assistantPhaseTimer = null;

function normalizeMessages(items) {
  return [...(items || [])].sort((a, b) => {
    const left = new Date(a.created_at || 0).getTime();
    const right = new Date(b.created_at || 0).getTime();
    return left - right;
  });
}

function upsertMessage(message) {
  const index = messages.value.findIndex((item) => item.id === message.id);
  if (index >= 0) {
    messages.value[index] = { ...messages.value[index], ...message };
  } else {
    messages.value.push(message);
  }
  messages.value = normalizeMessages(messages.value);
}

function clearAssistantPlaceholder() {
  if (assistantPhaseTimer) {
    clearTimeout(assistantPhaseTimer);
    assistantPhaseTimer = null;
  }
  if (assistantPlaceholderId.value) {
    messages.value = messages.value.filter((item) => item.id !== assistantPlaceholderId.value);
  }
  assistantPlaceholderId.value = "";
  assistantPhase.value = "queued";
}

function buildAssistantPlaceholderMessage(phase) {
  const statusMap = {
    queued: "Queued and waiting to start",
    preparing_context: "Preparing context and runtime",
    recalling_memory: "Recalling long-term user memory",
    generating_reply: "Thinking and drafting a reply",
    thinking: "Thinking and drafting a reply"
  };
  return {
    id: assistantPlaceholderId.value,
    sender_role: "assistant",
    message_type: "AIMessage",
    message_status: phase,
    content_md: statusMap[phase] || "Thinking and drafting a reply",
    created_at: new Date().toISOString(),
    is_placeholder: true,
  };
}

function setAssistantPlaceholderPhase(phase) {
  assistantPhase.value = phase;
  if (!assistantPlaceholderId.value) return;
  upsertMessage(buildAssistantPlaceholderMessage(phase));
}

function showAssistantPlaceholder() {
  clearAssistantPlaceholder();
  assistantPlaceholderId.value = `placeholder-${Date.now()}`;
  setAssistantPlaceholderPhase("queued");
  assistantPhaseTimer = setTimeout(() => {
    setAssistantPlaceholderPhase("thinking");
  }, 1200);
}

async function loadMessages() {
  loading.value = true;
  error.value = "";
  try {
    const [messageData, attachmentData, interruptData] = await Promise.all([
      apiFetch(`/conversations/${conversationId.value}/messages`),
      apiFetch(`/conversations/${conversationId.value}/attachments`),
      apiFetch(`/conversations/${conversationId.value}/interrupts/pending`)
    ]);
    messages.value = normalizeMessages(messageData.items || []);
    attachments.value = attachmentData.items || [];
    pendingInterruptId.value = interruptData.interrupt_id || "";
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

function stopEventStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function startEventStream() {
  stopEventStream();
  if (!session.token || !conversationId.value) return;
  const url = `${API_BASE}/conversations/${conversationId.value}/events?token=${encodeURIComponent(session.token)}`;
  eventSource = new EventSource(url);
  eventSource.addEventListener("message.created", (event) => {
    const data = JSON.parse(event.data);
    if (data?.payload) {
      if (data.payload.sender_role !== "user" && data.payload.sender_role !== "human") {
        clearAssistantPlaceholder();
      }
      upsertMessage(data.payload);
    }
  });
  eventSource.addEventListener("message.updated", (event) => {
    const data = JSON.parse(event.data);
    if (data?.payload) {
      if (data.payload.sender_role !== "user" && data.payload.sender_role !== "human") {
        clearAssistantPlaceholder();
      }
      upsertMessage(data.payload);
    }
  });
  eventSource.addEventListener("runtime.stage", (event) => {
    const data = JSON.parse(event.data);
    const stage = String(data?.payload?.stage || "").trim();
    if (!assistantPlaceholderId.value || !stage) return;
    if (["queued", "preparing_context", "recalling_memory", "generating_reply", "thinking"].includes(stage)) {
      setAssistantPlaceholderPhase(stage === "generating_reply" ? "thinking" : stage);
    }
  });
}

async function sendMessage() {
  if (!input.value.trim()) return;
  sending.value = true;
  error.value = "";
  try {
    const result = await apiFetch(`/conversations/${conversationId.value}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: input.value.trim() })
    });
    if (result?.accepted && result?.queued) {
      showAssistantPlaceholder();
    }
    input.value = "";
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

async function downloadAttachment(path, filename) {
  error.value = "";
  try {
    const headers = {};
    if (session.token) {
      headers.Authorization = `Bearer ${session.token}`;
    }
    const response = await fetch(
      `${API_BASE}/conversations/${conversationId.value}/attachments/download?path=${encodeURIComponent(path)}`,
      { headers }
    );
    if (!response.ok) {
      handleUnauthorizedResponse(response);
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    useBlobDownload(blob, filename || path.split("/").pop() || "download");
  } catch (err) {
    error.value = err.message;
  }
}

async function previewMarkdown(item) {
  const path = item?.markdown?.workspace_path;
  if (!path) return;
  previewOpen.value = true;
  previewLoading.value = true;
  previewTitle.value = `${item.original_name || item.saved_name} (.md)`;
  previewContent.value = "";
  previewTruncated.value = false;
  try {
    const data = await apiFetch(
      `/conversations/${conversationId.value}/attachments/markdown?path=${encodeURIComponent(path)}`
    );
    previewContent.value = data.content || "";
    previewTruncated.value = !!data.truncated;
  } catch (err) {
    error.value = err.message;
    previewOpen.value = false;
  } finally {
    previewLoading.value = false;
  }
}

watch(conversationId, async () => {
  await loadMessages();
  startEventStream();
});

onMounted(async () => {
  await loadMessages();
  startEventStream();
});

onBeforeUnmount(() => {
  stopEventStream();
  clearAssistantPlaceholder();
});
</script>

<template>
  <div class="conversation-layout">
    <section class="panel-card chat-panel">
      <div class="panel-head">
        <div>
          <h2>Conversation Detail</h2>
          <p>Streaming updates, approvals, uploads, downloads, and markdown preview are enabled.</p>
        </div>
        <button class="secondary-button" type="button" @click="loadMessages">
          Refresh
        </button>
      </div>

      <div v-if="pendingInterruptId" class="warning-box">
        <strong>Pending interrupt:</strong> {{ pendingInterruptId }}
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
          <div v-if="item.is_placeholder" class="thinking-indicator">
            <span>{{ item.content_md }}</span>
            <span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span>
          </div>
          <pre v-else class="message-body">{{ item.content_md }}</pre>
        </article>
      </div>

      <div class="composer-card">
        <textarea
          v-model="input"
          class="text-area"
          rows="4"
          placeholder="Enter a task, question, or instruction"
        />
        <div class="button-row">
          <button class="primary-button" type="button" :disabled="sending" @click="sendMessage">
            {{ sending ? "Sending..." : "Send Message" }}
          </button>
        </div>
      </div>

      <p v-if="loading">Loading...</p>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <section class="panel-card sidebar-panel">
      <h3>Files</h3>
      <p class="muted-text">Uploads are stored in the conversation workspace and can be converted to markdown.</p>

      <div class="form-stack">
        <label class="field-label">
          Upload files
          <input
            class="text-input"
            type="file"
            multiple
            @change="files = $event.target.files"
          />
        </label>
        <button class="primary-button" type="button" :disabled="uploading" @click="uploadFiles">
          {{ uploading ? "Uploading..." : "Upload and Convert" }}
        </button>
      </div>

      <h4>Attachments</h4>
      <ul class="simple-list attachment-list">
        <li v-for="item in attachments" :key="item.workspace_path" class="attachment-item">
          <div>
            <strong>{{ item.original_name || item.saved_name }}</strong>
            <div class="muted-text">{{ item.workspace_path }}</div>
          </div>
          <div class="button-row">
            <button
              class="secondary-button slim-button"
              type="button"
              @click="downloadAttachment(item.workspace_path, item.original_name || item.saved_name)"
            >
              Original
            </button>
            <button
              v-if="item.markdown?.workspace_path"
              class="secondary-button slim-button"
              type="button"
              @click="downloadAttachment(item.markdown.workspace_path, `${item.original_name || item.saved_name}.md`)"
            >
              Markdown
            </button>
            <button
              v-if="item.markdown?.workspace_path"
              class="primary-button slim-button"
              type="button"
              @click="previewMarkdown(item)"
            >
              Preview
            </button>
          </div>
        </li>
      </ul>
    </section>

    <section v-if="previewOpen" class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h3>{{ previewTitle }}</h3>
          <p class="muted-text">Markdown preview from the parsed workspace file.</p>
        </div>
        <button class="secondary-button" type="button" @click="previewOpen = false">
          Close
        </button>
      </div>
      <p v-if="previewLoading">Loading preview...</p>
      <pre v-else class="message-body preview-body">{{ previewContent }}</pre>
      <p v-if="previewTruncated" class="muted-text">Preview truncated because the file is too large.</p>
    </section>
  </div>
</template>

<style scoped>
.thinking-indicator {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.thinking-dots {
  display: inline-flex;
  gap: 6px;
}

.thinking-dots i {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: currentColor;
  opacity: 0.28;
  animation: thinkingPulse 1.1s infinite ease-in-out;
}

.thinking-dots i:nth-child(2) {
  animation-delay: 0.16s;
}

.thinking-dots i:nth-child(3) {
  animation-delay: 0.32s;
}

@keyframes thinkingPulse {
  0%, 80%, 100% {
    transform: scale(0.72);
    opacity: 0.22;
  }
  40% {
    transform: scale(1);
    opacity: 0.95;
  }
}
</style>
