<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useI18n } from "../../i18n";
import { API_BASE, apiFetch, apiUpload, handleUnauthorizedResponse, useBlobDownload } from "../../services/api";
import { useSessionStore } from "../../stores/session";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const session = useSessionStore();
const conversationId = computed(() => route.params.id);

const loading = ref(false);
const sending = ref(false);
const uploading = ref(false);
const creatingConversation = ref(false);
const historyLoading = ref(false);
const error = ref("");
const transientMessage = ref("");
const messages = ref([]);
const attachments = ref([]);
const conversations = ref([]);
const workspace = ref({
  conversation: null,
  stats: {},
  skills: [],
  my_skills: [],
  mcp: { bindings: [], capabilities: [] },
  tasks: { runtime: {}, runs: [], scheduled: [] }
});
const pendingInterruptId = ref("");
const input = ref("");
const files = ref([]);
const previewOpen = ref(false);
const previewTitle = ref("");
const previewContent = ref("");
const previewTruncated = ref(false);
const previewLoading = ref(false);
const messageListRef = ref(null);
const filePickerRef = ref(null);
const titleDraft = ref("");
const savingTitle = ref(false);
const titleEditorOpen = ref(false);
const deleteDialogOpen = ref(false);
const deletingConversation = ref(false);
const pendingDeleteConversation = ref(null);
const newConversationBackend = ref("deepagents");
const activeConversation = computed(() => workspace.value.conversation || {});
const conversationStats = computed(() => workspace.value.stats || {});
const assistantPlaceholderId = ref("");
const assistantPhase = ref("queued");
let assistantPhaseTimer = null;
function dedupeSkills(items) {
  const seen = new Set();
  const result = [];
  for (const item of items || []) {
    const key = String(item?.id || item?.name || "");
    if (!key || seen.has(key)) continue;
    seen.add(key);
    result.push(item);
  }
  return result;
}

const visibleSkills = computed(() => dedupeSkills(workspace.value.skills || []));
const mySkills = computed(() => dedupeSkills(workspace.value.my_skills || []));
const mcpBindings = computed(() => workspace.value.mcp?.bindings || []);
const mcpConnections = computed(() => workspace.value.mcp?.all_connections || []);
const attachmentCount = computed(() => Number(conversationStats.value.attachments_count || 0));
const currentUserLabel = computed(
  () => session.user?.display_name || session.user?.username || t("conversation_role_user")
);
const currentAgentLabel = computed(() => {
  const backend = String(activeConversation.value.execution_backend || "").trim();
  const model = String(activeConversation.value.model || "").trim();
  if (backend === "claude") {
    return model ? `Claude Agent · ${model}` : "Claude Agent";
  }
  if (backend === "deepagents") {
    return model ? `DeepAgent · ${model}` : "DeepAgent";
  }
  if (model) return model;
  return t("conversation_role_assistant");
});

let eventSource = null;
let transientTimer = null;

function normalizeMessages(items) {
  return [...(items || [])].sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
}

function upsertMessage(message) {
    const index = messages.value.findIndex((item) => item.id === message.id);
  if (index >= 0) {
    messages.value[index] = { ...messages.value[index], ...message };
  } else {
    messages.value.push(message);
  }
  messages.value = normalizeMessages(messages.value);
  nextTick(scrollToBottom);
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
    queued: t("conversation_agent_queued"),
    preparing_context: t("conversation_agent_preparing_context"),
    recalling_memory: t("conversation_agent_recalling_memory"),
    generating_reply: t("conversation_agent_thinking"),
    thinking: t("conversation_agent_thinking")
  };
  const statusText = statusMap[phase] || t("conversation_agent_thinking");
  return {
    id: assistantPlaceholderId.value,
    sender_role: "assistant",
    message_type: "AIMessage",
    message_status: phase,
    content_md: statusText,
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

function scrollToBottom() {
  const element = messageListRef.value;
  if (!element) return;
  element.scrollTop = element.scrollHeight;
}

function messageRoleLabel(item) {
  if (item.sender_role === "user" || item.sender_role === "human") return currentUserLabel.value;
  if (item.message_type === "ToolMessage") return t("conversation_role_tool");
  if (item.message_type === "SystemMessage") return t("conversation_role_system");
  return currentAgentLabel.value;
}

function messageBubbleClass(item) {
  if (item.is_placeholder) return "message-row is-assistant is-placeholder";
  if (item.sender_role === "user" || item.sender_role === "human") return "message-row is-user";
  if (item.message_type === "ToolMessage") return "message-row is-tool";
  if (item.message_type === "SystemMessage") return "message-row is-system";
  return "message-row is-assistant";
}

function messageAvatarLabel(item) {
  const label = messageRoleLabel(item);
  return String(label || "?").slice(0, 1).toUpperCase();
}

function messageTriggerTags(item) {
  const payload = item?.attachments_json;
  const tags = Array.isArray(payload?.trigger_tags) ? payload.trigger_tags : [];
  return tags.filter((tag) => tag === "skill" || tag === "mcp");
}

function triggerTagLabel(tag) {
  if (tag === "skill") return t("conversation_trigger_skill");
  if (tag === "mcp") return t("conversation_trigger_mcp");
  return String(tag || "");
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function sanitizeHtml(value) {
  return String(value || "")
    .replace(/<\s*(script|style|iframe|object|embed)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/\son[a-z]+\s*=\s*(".*?"|'.*?'|[^\s>]+)/gi, "")
    .replace(/\s(href|src)\s*=\s*(['"])\s*javascript:[\s\S]*?\2/gi, "");
}

function renderInlineMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  html = html.replace(/_([^_]+)_/g, "<em>$1</em>");
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return html;
}

function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  const parts = [];
  let inCode = false;
  let codeLines = [];
  let listItems = [];

  function flushCode() {
    if (!codeLines.length) return;
    parts.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    codeLines = [];
  }

  function flushList() {
    if (!listItems.length) return;
    parts.push(`<ul>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  }

  for (const rawLine of lines) {
    const line = rawLine ?? "";
    if (line.trim().startsWith("```")) {
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushList();
      const level = heading[1].length;
      parts.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const list = line.match(/^\s*[-*]\s+(.*)$/);
    if (list) {
      listItems.push(list[1]);
      continue;
    }
    flushList();
    if (!line.trim()) {
      continue;
    }
    parts.push(`<p>${renderInlineMarkdown(line)}</p>`);
  }
  flushList();
  if (inCode) {
    flushCode();
  }
  return parts.filter(Boolean).join("");
}

function renderRichContent(content) {
  const value = String(content || "");
  if (!value.trim()) return "";
  if (/<[a-z][\s\S]*>/i.test(value)) {
    return sanitizeHtml(value);
  }
  return renderMarkdown(value);
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function formatRuntime(ms) {
  const value = Number(ms || 0);
  if (!value) return "0 ms";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function statusChipClass(status) {
  if (status === "active" || status === "running" || status === "success") return "status-chip is-active";
  if (status === "paused" || status === "queued") return "status-chip is-paused";
  if (status === "completed") return "status-chip is-completed";
  if (status === "failed" || status === "error") return "status-chip is-failed";
  return "status-chip";
}

function normalizeConversationTitle(title) {
  return String(title || "").trim();
}

function isTimestampLikeTitle(title) {
  const value = normalizeConversationTitle(title);
  if (!value) return false;
  return (
    /^\d{4}-\d{1,2}-\d{1,2}[ t]\d{1,2}:\d{2}(:\d{2})?/.test(value) ||
    /^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+\d{1,2}:\d{2}/.test(value)
  );
}

function canAutoRenameConversation() {
  const currentTitle = normalizeConversationTitle(activeConversation.value.title);
  return !currentTitle || currentTitle === t("conversation_center") || currentTitle === t("conversation_detail") || isTimestampLikeTitle(currentTitle);
}

function buildAutoConversationTitle(content) {
  const source = String(content || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    .replace(/[#>*_\-\n\r]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!source) return t("conversation_detail");
  return source.length > 24 ? `${source.slice(0, 24).trim()}...` : source;
}

async function updateConversationTitle(nextTitle, successMessage = "msg_conversation_title_saved") {
  const readyId = await ensureConversationReady();
  const title = normalizeConversationTitle(nextTitle);
  if (!readyId || !title) return;
  savingTitle.value = true;
  error.value = "";
  try {
    const updated = await apiFetch(`/conversations/${readyId}`, {
      method: "PATCH",
      body: JSON.stringify({ title })
    });
    workspace.value = {
      ...workspace.value,
      conversation: { ...(workspace.value.conversation || {}), ...updated }
    };
    titleDraft.value = updated.title || title;
    await loadConversations();
    flashMessage(t(successMessage));
  } catch (err) {
    error.value = err.message;
  } finally {
    savingTitle.value = false;
  }
}

async function loadConversations() {
  historyLoading.value = true;
  try {
    const data = await apiFetch("/conversations");
    conversations.value = data.items || [];
  } finally {
    historyLoading.value = false;
  }
}

async function ensureConversationReady() {
  if (conversationId.value) return conversationId.value;
  if (creatingConversation.value) return null;
  creatingConversation.value = true;
  error.value = "";
  try {
    const data = await apiFetch("/conversations");
    const items = data.items || [];
    if (items.length) {
      const targetId = items[0].id;
      await router.replace(`/conversations/${targetId}`);
      return targetId;
    }
    const created = await apiFetch("/conversations", {
      method: "POST",
      body: JSON.stringify({
        title: t("conversation_center"),
        model: "default",
        execution_backend: newConversationBackend.value,
        skills: [],
        tools: []
      })
    });
    await router.replace(`/conversations/${created.id}`);
    return created.id;
  } catch (err) {
    error.value = err.message;
    return null;
  } finally {
    creatingConversation.value = false;
  }
}

async function loadWorkspace() {
  const data = await apiFetch(`/conversations/${conversationId.value}/workspace`);
  workspace.value = {
    conversation: data.conversation || null,
    stats: data.stats || {},
    skills: data.skills || [],
    my_skills: data.my_skills || [],
    mcp: data.mcp || { bindings: [], capabilities: [] },
    tasks: data.tasks || { runtime: {}, runs: [], scheduled: [] }
  };
  titleDraft.value = data.conversation?.title || "";
}

async function loadConversationData() {
  const readyId = await ensureConversationReady();
  if (!readyId) return;
  loading.value = true;
  error.value = "";
  try {
    const [messageData, attachmentData, interruptData] = await Promise.all([
      apiFetch(`/conversations/${conversationId.value}/messages`),
      apiFetch(`/conversations/${conversationId.value}/attachments`),
      apiFetch(`/conversations/${conversationId.value}/interrupts/pending`),
      loadWorkspace(),
      loadConversations()
    ]);
    messages.value = normalizeMessages(messageData.items || []);
    attachments.value = attachmentData.items || [];
    pendingInterruptId.value = interruptData.interrupt_id || "";
    await nextTick();
    scrollToBottom();
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
    loadWorkspace().catch(() => {});
    loadConversations().catch(() => {});
  });
  eventSource.addEventListener("message.updated", (event) => {
    const data = JSON.parse(event.data);
    if (data?.payload) {
      if (data.payload.sender_role !== "user" && data.payload.sender_role !== "human") {
        clearAssistantPlaceholder();
      }
      upsertMessage(data.payload);
    }
    loadWorkspace().catch(() => {});
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

function flashMessage(message) {
  transientMessage.value = message;
  if (transientTimer) {
    clearTimeout(transientTimer);
  }
  transientTimer = setTimeout(() => {
    transientMessage.value = "";
  }, 2500);
}

async function sendMessage() {
  const readyId = await ensureConversationReady();
  const content = input.value.trim();
  if (!readyId || !content) return;
  const shouldAutoRename = canAutoRenameConversation();
  const autoTitle = shouldAutoRename ? buildAutoConversationTitle(content) : "";
  sending.value = true;
  error.value = "";
  try {
    const result = await apiFetch(`/conversations/${conversationId.value}/messages`, {
      method: "POST",
      body: JSON.stringify({ content })
    });
    if (result?.accepted && result?.queued) {
      showAssistantPlaceholder();
    }
    input.value = "";
    if (autoTitle) {
      await updateConversationTitle(autoTitle, "msg_conversation_title_auto");
    }
  } catch (err) {
    error.value = err.message;
  } finally {
    sending.value = false;
  }
}

async function uploadFiles() {
  const readyId = await ensureConversationReady();
  if (!readyId) return;
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
    if (filePickerRef.value) {
      filePickerRef.value.value = "";
    }
    await loadConversationData();
    flashMessage(t("msg_files_uploaded"));
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
    await apiFetch(`/conversations/${conversationId.value}/interrupts/${pendingInterruptId.value}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision })
    });
    await loadConversationData();
  } catch (err) {
    error.value = err.message;
  }
}

async function refreshConversationSkills() {
  const readyId = await ensureConversationReady();
  if (!readyId) return;
  error.value = "";
  try {
    await apiFetch(`/conversations/${conversationId.value}/refresh_skills`, { method: "POST" });
    await loadWorkspace();
    flashMessage(t("msg_skills_refreshed"));
  } catch (err) {
    error.value = err.message;
  }
}

async function createConversation() {
  creatingConversation.value = true;
  error.value = "";
  try {
    const created = await apiFetch("/conversations", {
      method: "POST",
      body: JSON.stringify({
        title: t("conversation_center"),
        model: activeConversation.value.model || "default",
        execution_backend: newConversationBackend.value,
        skills: [],
        tools: []
      })
    });
    await loadConversations();
    router.push(`/conversations/${created.id}`);
  } catch (err) {
    error.value = err.message;
  } finally {
    creatingConversation.value = false;
  }
}

async function saveConversationTitle() {
  await updateConversationTitle(titleDraft.value);
  if (!error.value) titleEditorOpen.value = false;
}

async function autoRenameConversation() {
  const userMessage = messages.value.find((item) => item.sender_role === "user" || item.sender_role === "human");
  const nextTitle = buildAutoConversationTitle(userMessage?.content_md || userMessage?.content || "");
  await updateConversationTitle(nextTitle, "msg_conversation_title_auto");
  if (!error.value) titleEditorOpen.value = false;
}

function openTitleEditor() {
  titleDraft.value = activeConversation.value.title || "";
  titleEditorOpen.value = true;
}

function openDeleteDialog(item) {
  if (!item?.id) return;
  pendingDeleteConversation.value = item;
  deleteDialogOpen.value = true;
}

function closeDeleteDialog() {
  if (deletingConversation.value) return;
  deleteDialogOpen.value = false;
  pendingDeleteConversation.value = null;
}

async function confirmDeleteConversation() {
  const item = pendingDeleteConversation.value;
  if (!item?.id) return;
  deletingConversation.value = true;
  error.value = "";
  try {
    await apiFetch(`/conversations/${item.id}`, { method: "DELETE" });
    const deletingCurrent = item.id === conversationId.value;
    const nextItems = conversations.value.filter((entry) => entry.id !== item.id);
    conversations.value = nextItems;
    if (deletingCurrent) {
      const nextId = nextItems[0]?.id || "";
      if (nextId) {
        router.push(`/conversations/${nextId}`);
      } else {
        router.push("/conversations");
      }
    } else {
      await loadConversations();
    }
    closeDeleteDialog();
    flashMessage(t("msg_conversation_deleted"));
  } catch (err) {
    error.value = err.message;
  } finally {
    deletingConversation.value = false;
  }
}

async function installConversationSkill(skillId) {
  const readyId = await ensureConversationReady();
  if (!readyId || !skillId) return;
  error.value = "";
  try {
    await apiFetch(`/skills/${skillId}/install`, {
      method: "POST",
      body: JSON.stringify({ conversation_id: readyId })
    });
    await loadWorkspace();
    flashMessage(t("msg_skill_installed"));
  } catch (err) {
    error.value = err.message;
  }
}

async function removeConversationSkill(skillId) {
  const readyId = await ensureConversationReady();
  if (!readyId || !skillId) return;
  error.value = "";
  try {
    await apiFetch(`/skills/${skillId}/install/${readyId}`, { method: "DELETE" });
    await loadWorkspace();
    flashMessage(t("msg_skill_uninstalled"));
  } catch (err) {
    error.value = err.message;
  }
}

async function addConversationMcp(connectionId) {
  const readyId = await ensureConversationReady();
  if (!readyId || !connectionId) return;
  error.value = "";
  try {
    await apiFetch(`/mcp/conversations/${readyId}/bindings`, {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId, enabled: true })
    });
    await loadWorkspace();
    flashMessage(t("msg_mcp_bound"));
  } catch (err) {
    error.value = err.message;
  }
}

async function removeConversationMcp(item) {
  const bindingId = item?.binding_id || item?.id;
  if (!bindingId) return;
  error.value = "";
  try {
    await apiFetch(`/mcp/bindings/${bindingId}`, { method: "DELETE" });
    await loadWorkspace();
    flashMessage(t("msg_mcp_unbound"));
  } catch (err) {
    error.value = err.message;
  }
}

async function downloadAttachment(path, filename) {
  error.value = "";
  try {
    const headers = {};
    if (session.token) headers.Authorization = `Bearer ${session.token}`;
    const response = await fetch(`${API_BASE}/conversations/${conversationId.value}/attachments/download?path=${encodeURIComponent(path)}`, { headers });
    if (!response.ok) {
      handleUnauthorizedResponse(response);
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    useBlobDownload(await response.blob(), filename || path.split("/").pop() || "download");
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
    const data = await apiFetch(`/conversations/${conversationId.value}/attachments/markdown?path=${encodeURIComponent(path)}`);
    previewContent.value = data.content || "";
    previewTruncated.value = !!data.truncated;
  } catch (err) {
    error.value = err.message;
    previewOpen.value = false;
  } finally {
    previewLoading.value = false;
  }
}

function goConversation(id) {
  if (!id || id === conversationId.value) return;
  router.push(`/conversations/${id}`);
}

function triggerFilePicker() {
  filePickerRef.value?.click();
}

function onFilesSelected(event) {
  files.value = event.target.files;
}

watch(conversationId, async () => {
  await loadConversationData();
  startEventStream();
});

onMounted(async () => {
  await loadConversationData();
  startEventStream();
});

onBeforeUnmount(() => {
  stopEventStream();
  clearAssistantPlaceholder();
  if (transientTimer) {
    clearTimeout(transientTimer);
  }
});
</script>

<template>
  <div class="conversation-shell">
    <aside class="panel-card history-sidebar">
      <div class="panel-head compact-head">
        <div>
          <h3>{{ t("conversation_history") }}</h3>
          <p class="muted-text">{{ t("conversation_history_desc") }}</p>
        </div>
      </div>
      <div v-if="historyLoading" class="muted-text">{{ t("loading") }}</div>
      <ul v-else class="simple-list history-list">
        <li
          v-for="item in conversations"
          :key="item.id"
          :class="['history-item', item.id === conversationId ? 'is-active' : '']"
          @click="goConversation(item.id)"
        >
          <div class="history-item-main">
            <strong>{{ item.title }}</strong>
            <div class="muted-text">{{ item.model }} / {{ item.execution_backend }}</div>
          </div>
          <button
            class="history-delete-button"
            type="button"
            :aria-label="t('delete')"
            @click.stop="openDeleteDialog(item)"
          >
            &times;
          </button>
        </li>
      </ul>
      <div class="history-footer">
        <select v-model="newConversationBackend" class="text-input history-backend-select">
          <option value="deepagents">{{ t("backend_deepagents") }}</option>
          <option value="claude">{{ t("backend_claude_agent") }}</option>
        </select>
        <button class="primary-button slim-button history-create-button" type="button" :disabled="creatingConversation" @click="createConversation">
          {{ creatingConversation ? t("creating") : t("new_conversation") }}
        </button>
      </div>
    </aside>

    <section class="panel-card chat-surface conversation-main">
      <div class="chat-topbar">
        <div class="chat-title-row">
          <div class="conversation-title-block">
            <h2 class="chat-title conversation-title-small">{{ activeConversation.title || t("conversation_detail") }}</h2>
            <button class="secondary-button slim-button title-edit-button" type="button" :aria-label="t('rename_conversation')" @click="openTitleEditor">
              ✎
            </button>
          </div>
          <p class="muted-text compact-meta">
            {{ activeConversation.model || "-" }} / {{ activeConversation.execution_backend || "-" }}
          </p>
        </div>
        <div class="chat-topbar-actions">
          <div class="stats-inline">
            <span>{{ t("conversation_messages_count") }} {{ conversationStats.message_count || 0 }}</span>
            <span>{{ t("conversation_tokens_count") }} {{ conversationStats.total_tokens || 0 }}</span>
            <span>{{ t("conversation_runtime_count") }} {{ formatRuntime(conversationStats.total_runtime_ms) }}</span>
          </div>
        </div>
      </div>

      <div v-if="pendingInterruptId" class="warning-box">
        <strong>{{ t("pending_interrupt") }}:</strong> {{ pendingInterruptId }}
        <div class="button-row">
          <button class="primary-button" type="button" @click="decideInterrupt('allow')">{{ t("allow") }}</button>
          <button class="secondary-button" type="button" @click="decideInterrupt('reject')">{{ t("reject") }}</button>
          <button class="secondary-button" type="button" @click="decideInterrupt('allow_all')">{{ t("allow_all") }}</button>
        </div>
      </div>

      <div ref="messageListRef" class="chat-thread">
        <div v-if="loading" class="chat-empty">{{ t("loading") }}</div>
        <div v-else-if="!messages.length" class="chat-empty">{{ t("conversation_empty") }}</div>
        <article v-for="item in messages" :key="item.id" :class="messageBubbleClass(item)">
          <div class="message-avatar">{{ messageAvatarLabel(item) }}</div>
          <div class="message-bubble">
            <div class="message-meta">
              <strong>{{ messageRoleLabel(item) }}</strong>
              <span>{{ item.message_type }}</span>
              <span>{{ item.message_status }}</span>
              <span v-for="tag in messageTriggerTags(item)" :key="`${item.id}-${tag}`" class="status-chip is-completed">
                {{ triggerTagLabel(tag) }}
              </span>
              <span>{{ formatDateTime(item.created_at) }}</span>
            </div>
            <div v-if="item.is_placeholder" class="thinking-indicator">
              <span>{{ item.content_md }}</span>
              <span class="thinking-dots" aria-hidden="true">
                <i></i><i></i><i></i>
              </span>
            </div>
            <div v-else class="message-body rich-content" v-html="renderRichContent(item.content_md)"></div>
            <div class="message-foot">
              <span v-if="item.total_tokens">{{ t("conversation_tokens") }} {{ item.total_tokens }}</span>
              <span v-if="item.run_duration_ms">{{ t("conversation_runtime") }} {{ formatRuntime(item.run_duration_ms) }}</span>
            </div>
          </div>
        </article>
      </div>

      <div class="chat-composer">
        <div class="composer-input-wrap">
          <textarea
            v-model="input"
            class="text-area composer-input has-toolbar"
            rows="3"
            :placeholder="t('enter_task')"
            @keydown.ctrl.enter.prevent="sendMessage"
          />
          <input ref="filePickerRef" class="hidden-file-input" type="file" multiple @change="onFilesSelected" />
          <button class="secondary-button slim-button composer-attach-button" type="button" @click="triggerFilePicker">
            {{ t("upload_files") }}
          </button>
          <button class="primary-button slim-button composer-send-button" type="button" :disabled="sending" @click="sendMessage">
            {{ sending ? t("sending") : t("send_message") }}
          </button>
        </div>
        <div v-if="files && files.length" class="composer-files">
          <span v-for="file in Array.from(files)" :key="`${file.name}-${file.size}`" class="capability-chip">
            {{ file.name }}
          </span>
        </div>
        <div class="composer-actions">
          <div class="muted-text">{{ t("conversation_composer_hint") }}</div>
          <div class="button-row">
            <button v-if="files && files.length" class="secondary-button" type="button" :disabled="uploading" @click="uploadFiles">
              {{ uploading ? t("uploading") : t("upload_and_convert") }}
            </button>
          </div>
        </div>
        <p v-if="error" class="error-text">{{ error }}</p>
        <p v-if="transientMessage" class="success-text">{{ transientMessage }}</p>
      </div>
    </section>

    <aside class="conversation-sidebar">
      <section class="panel-card sidebar-card">
        <div class="panel-head compact-head">
          <div>
            <h3>{{ t("skill_list") }}</h3>
            <p class="muted-text">{{ t("conversation_skills_desc") }}</p>
          </div>
          <button class="secondary-button slim-button" type="button" @click="refreshConversationSkills">{{ t("refresh") }}</button>
        </div>
        <div class="inline-skill-form">
          <div class="muted-text">{{ t("conversation_add_skill_desc") }}</div>
          <ul class="simple-list chip-list">
            <li v-if="!mySkills.length" class="muted-text">{{ t("conversation_no_my_skills") }}</li>
            <li v-for="item in mySkills" :key="`mine-${item.id}`" class="info-item">
              <div class="info-item-top">
                <strong>{{ item.display_name || item.name }}</strong>
                <span :class="['status-chip', item.conversation_enabled ? 'is-active' : 'is-failed']">
                  {{ item.conversation_enabled ? t("conversation_skill_enabled") : t("conversation_skill_disabled") }}
                </span>
              </div>
              <div class="muted-text skill-desc-clamp">{{ item.description || t("no_description") }}</div>
              <div class="button-row">
                <span class="muted-text">{{ item.conversation_enabled ? t("conversation_added_to_conversation") : t("conversation_not_added_to_conversation") }}</span>
                <button
                  v-if="!item.conversation_enabled"
                  class="primary-button slim-button"
                  type="button"
                  @click="installConversationSkill(item.id)"
                >
                  {{ t("add_to_conversation") }}
                </button>
                <button
                  v-else
                  class="secondary-button slim-button"
                  type="button"
                  @click="removeConversationSkill(item.id)"
                >
                  {{ t("remove_from_conversation") }}
                </button>
              </div>
            </li>
          </ul>
        </div>
      </section>

      <section class="panel-card sidebar-card">
        <div class="panel-head compact-head">
          <div>
            <h3>{{ t("mcp_connections") }}</h3>
            <p class="muted-text">{{ t("conversation_mcp_desc") }}</p>
          </div>
        </div>
        <ul class="simple-list chip-list">
          <li v-if="!mcpConnections.length" class="muted-text">{{ t("conversation_no_mcp") }}</li>
          <li v-for="item in mcpConnections" :key="item.id" class="info-item conversation-mcp-item">
            <div class="info-item-top">
              <strong>{{ item.display_name }}</strong>
              <span :class="['status-chip', item.conversation_enabled ? 'is-active' : 'is-failed']">
                {{ item.conversation_enabled ? t("conversation_mcp_bound") : t("conversation_mcp_unbound") }}
              </span>
            </div>
            <div class="muted-text conversation-mcp-url">{{ item.base_url || item.server_key || "-" }}</div>
            <div class="button-row conversation-mcp-actions">
              <button
                v-if="!item.conversation_enabled"
                class="primary-button slim-button"
                type="button"
                @click="addConversationMcp(item.connection_id || item.id)"
              >
                {{ t("add_to_conversation") }}
              </button>
              <button
                v-else
                class="secondary-button slim-button"
                type="button"
                @click="removeConversationMcp(item)"
              >
                {{ t("remove_from_conversation") }}
              </button>
            </div>
          </li>
        </ul>
      </section>
    </aside>

    <div v-if="titleEditorOpen" class="admin-modal-backdrop" @click.self="titleEditorOpen = false">
      <section class="panel-card admin-modal-card title-overlay-card">
        <div class="panel-head">
          <div>
            <h3>{{ t("rename_conversation") }}</h3>
            <p class="muted-text">{{ t("conversation_detail") }}</p>
          </div>
          <button class="secondary-button" type="button" @click="titleEditorOpen = false">{{ t("close") }}</button>
        </div>
        <div class="form-stack">
          <label class="field-label">
            {{ t("title") }}
            <input
              v-model="titleDraft"
              class="text-input conversation-title-input"
              :placeholder="t('conversation_detail')"
              @keydown.enter.prevent="saveConversationTitle"
            />
          </label>
          <div class="button-row">
            <button class="primary-button" type="button" :disabled="savingTitle" @click="saveConversationTitle">
              {{ savingTitle ? t("saving") : t("rename_conversation") }}
            </button>
            <button class="secondary-button" type="button" :disabled="savingTitle" @click="autoRenameConversation">
              {{ t("auto_rename_conversation") }}
            </button>
          </div>
        </div>
      </section>
    </div>

    <div v-if="deleteDialogOpen" class="admin-modal-backdrop" @click.self="closeDeleteDialog">
      <section class="panel-card admin-modal-card title-overlay-card">
        <div class="panel-head">
          <div>
            <h3>{{ t("delete") }}</h3>
            <p class="muted-text">{{ t("confirm_delete_conversation", { name: pendingDeleteConversation?.title || "-" }) }}</p>
          </div>
          <button class="secondary-button" type="button" :disabled="deletingConversation" @click="closeDeleteDialog">{{ t("close") }}</button>
        </div>
        <div class="button-row">
          <button class="danger-button" type="button" :disabled="deletingConversation" @click="confirmDeleteConversation">
            {{ deletingConversation ? t("loading") : t("delete") }}
          </button>
          <button class="secondary-button" type="button" :disabled="deletingConversation" @click="closeDeleteDialog">
            {{ t("close") }}
          </button>
        </div>
      </section>
    </div>

    <section v-if="previewOpen" class="panel-card overlay-card">
      <div class="panel-head">
        <div>
          <h3>{{ previewTitle }}</h3>
          <p class="muted-text">{{ t("markdown_preview_desc") }}</p>
        </div>
        <button class="secondary-button" type="button" @click="previewOpen = false">{{ t("close") }}</button>
      </div>
      <p v-if="previewLoading">{{ t("loading") }}</p>
      <div v-else class="preview-body">
        <p v-if="previewTruncated" class="warning-box">{{ t("preview_truncated") }}</p>
        <div class="message-body rich-content" v-html="renderRichContent(previewContent)"></div>
      </div>
    </section>

  </div>
</template>

<style scoped>
.thinking-indicator {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: rgba(255, 255, 255, 0.78);
}

.thinking-dots {
  display: inline-flex;
  align-items: center;
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
