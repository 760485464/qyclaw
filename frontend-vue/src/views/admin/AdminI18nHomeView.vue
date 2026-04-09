<script setup>
import { computed, onMounted, reactive, ref } from "vue";

import { useI18n } from "../../i18n";
import { apiFetch } from "../../services/api";
import { useSessionStore } from "../../stores/session";

const { t } = useI18n();
const session = useSessionStore();

const loading = ref(false);
const saving = ref(false);
const error = ref("");
const success = ref("");
const users = ref([]);
const search = ref("");

const createOpen = ref(false);
const editOpen = ref(false);
const resetOpen = ref(false);

const createForm = reactive({
  username: "",
  display_name: "",
  email: "",
  password: "",
  is_admin: false
});

const editTargetId = ref("");
const editForm = reactive({
  display_name: "",
  email: "",
  is_admin: false,
  is_blocked: false
});

const resetTargetId = ref("");
const resetPassword = ref("");

const filteredUsers = computed(() => {
  const keyword = search.value.trim().toLowerCase();
  if (!keyword) return users.value;
  return users.value.filter((user) =>
    [user.username, user.display_name, user.email].some((item) =>
      String(item || "").toLowerCase().includes(keyword)
    )
  );
});

const userStats = computed(() => {
  const total = users.value.length;
  const admins = users.value.filter((item) => item.is_admin).length;
  const blocked = users.value.filter((item) => item.is_blocked).length;
  return {
    total,
    admins,
    blocked,
    active: total - blocked
  };
});

const editTarget = computed(() => users.value.find((item) => item.id === editTargetId.value) || null);
const resetTarget = computed(() => users.value.find((item) => item.id === resetTargetId.value) || null);

function clearMessages() {
  error.value = "";
  success.value = "";
}

function formatDate(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

function resetCreateForm() {
  createForm.username = "";
  createForm.display_name = "";
  createForm.email = "";
  createForm.password = "";
  createForm.is_admin = false;
}

function openCreateModal() {
  resetCreateForm();
  clearMessages();
  createOpen.value = true;
}

function closeCreateModal() {
  createOpen.value = false;
}

function openEditModal(user) {
  editTargetId.value = user.id;
  editForm.display_name = user.display_name || "";
  editForm.email = user.email || "";
  editForm.is_admin = Boolean(user.is_admin);
  editForm.is_blocked = Boolean(user.is_blocked);
  clearMessages();
  editOpen.value = true;
}

function closeEditModal() {
  editOpen.value = false;
  editTargetId.value = "";
}

function openResetModal(user) {
  resetTargetId.value = user.id;
  resetPassword.value = "";
  clearMessages();
  resetOpen.value = true;
}

function closeResetModal() {
  resetOpen.value = false;
  resetTargetId.value = "";
  resetPassword.value = "";
}

async function loadUsers() {
  loading.value = true;
  error.value = "";
  try {
    const data = await apiFetch("/users");
    users.value = data.items || [];
  } catch (err) {
    error.value = err.message || t("admin_load_failed");
  } finally {
    loading.value = false;
  }
}

async function createUser() {
  saving.value = true;
  clearMessages();
  try {
    await apiFetch("/users", {
      method: "POST",
      body: JSON.stringify(createForm)
    });
    closeCreateModal();
    success.value = t("admin_user_created");
    await loadUsers();
  } catch (err) {
    error.value = err.message || t("admin_save_failed");
  } finally {
    saving.value = false;
  }
}

async function saveUser() {
  const target = editTarget.value;
  if (!target) return;
  saving.value = true;
  clearMessages();
  try {
    await apiFetch(`/users/${target.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        display_name: editForm.display_name,
        email: editForm.email,
        is_admin: editForm.is_admin,
        is_blocked: editForm.is_blocked
      })
    });
    closeEditModal();
    success.value = t("admin_user_updated");
    await loadUsers();
    if (target.id === session.user?.id) {
      await session.syncCurrentUser();
    }
  } catch (err) {
    error.value = err.message || t("admin_save_failed");
  } finally {
    saving.value = false;
  }
}

async function submitResetPassword() {
  const target = resetTarget.value;
  if (!target || !resetPassword.value.trim()) return;
  saving.value = true;
  clearMessages();
  try {
    await apiFetch(`/users/${target.id}/password/reset`, {
      method: "POST",
      body: JSON.stringify({ new_password: resetPassword.value })
    });
    closeResetModal();
    success.value = t("admin_password_reset_done");
  } catch (err) {
    error.value = err.message || t("admin_save_failed");
  } finally {
    saving.value = false;
  }
}

async function deleteUser(user) {
  const confirmed = window.confirm(t("admin_confirm_delete_user", { name: user.display_name || user.username }));
  if (!confirmed) return;

  saving.value = true;
  clearMessages();
  try {
    await apiFetch(`/users/${user.id}`, { method: "DELETE" });
    success.value = t("admin_user_deleted");
    if (user.id === session.user?.id) {
      session.clearSession();
      window.location.href = "/login";
      return;
    }
    await loadUsers();
  } catch (err) {
    error.value = err.message || t("admin_save_failed");
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadUsers();
});
</script>

<template>
  <section class="page-grid admin-grid">
    <article class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h2>{{ t("admin_title") }}</h2>
          <p class="muted-text">{{ t("admin_desc") }}</p>
        </div>
        <div class="button-row">
          <button class="primary-button slim-button" type="button" @click="openCreateModal">
            {{ t("admin_create_user") }}
          </button>
          <button class="secondary-button slim-button" type="button" :disabled="loading" @click="loadUsers">
            {{ loading ? t("loading") : t("refresh") }}
          </button>
        </div>
      </div>
      <div class="stats-grid">
        <div class="stat-card">
          <span>{{ t("admin_total_users") }}</span>
          <strong>{{ userStats.total }}</strong>
        </div>
        <div class="stat-card">
          <span>{{ t("admin_admin_users") }}</span>
          <strong>{{ userStats.admins }}</strong>
        </div>
        <div class="stat-card">
          <span>{{ t("admin_active_users") }}</span>
          <strong>{{ userStats.active }}</strong>
        </div>
        <div class="stat-card">
          <span>{{ t("admin_blocked_users") }}</span>
          <strong>{{ userStats.blocked }}</strong>
        </div>
      </div>
      <p v-if="error" class="error-text">{{ error }}</p>
      <p v-else-if="success" class="success-text">{{ success }}</p>
    </article>

    <article class="panel-card wide-card">
      <div class="panel-head">
        <div>
          <h3>{{ t("admin_user_list") }}</h3>
          <p class="muted-text">{{ t("admin_user_list_desc") }}</p>
        </div>
      </div>

      <label class="field-label admin-search-field">
        {{ t("search") }}
        <input v-model.trim="search" class="text-input" :placeholder="t('admin_search_placeholder')" />
      </label>

      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th>{{ t("auth_register_username") }}</th>
              <th>{{ t("auth_register_display_name") }}</th>
              <th>{{ t("auth_register_email") }}</th>
              <th>{{ t("admin_role") }}</th>
              <th>{{ t("status") }}</th>
              <th>{{ t("admin_updated_at") }}</th>
              <th class="admin-actions-col">{{ t("admin_actions") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in filteredUsers" :key="user.id">
              <td>{{ user.username }}</td>
              <td>{{ user.display_name || "-" }}</td>
              <td>{{ user.email }}</td>
              <td>
                <span class="status-chip" :class="user.is_admin ? 'is-active' : ''">
                  {{ user.is_admin ? t("admin_role_admin") : t("admin_role_user") }}
                </span>
              </td>
              <td>
                <span class="status-chip" :class="user.is_blocked ? 'is-failed' : 'is-active'">
                  {{ user.is_blocked ? t("admin_status_blocked") : t("admin_status_active") }}
                </span>
              </td>
              <td>{{ formatDate(user.updated_at) }}</td>
              <td>
                <div class="admin-row-actions">
                  <button class="secondary-button slim-button" type="button" @click="openEditModal(user)">
                    {{ t("admin_action_edit") }}
                  </button>
                  <button class="secondary-button slim-button" type="button" @click="openResetModal(user)">
                    {{ t("admin_reset_password_submit") }}
                  </button>
                  <button class="secondary-button slim-button danger-button" type="button" @click="deleteUser(user)">
                    {{ t("delete") }}
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="!loading && !filteredUsers.length">
              <td colspan="7">
                <div class="chat-empty admin-empty">{{ t("admin_no_users") }}</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>

    <div v-if="createOpen" class="admin-modal-backdrop" @click="closeCreateModal">
      <section class="panel-card overlay-card admin-modal-card" @click.stop>
        <div class="panel-head">
          <div>
            <h3>{{ t("admin_create_user") }}</h3>
            <p class="muted-text">{{ t("admin_create_user_desc") }}</p>
          </div>
          <button class="secondary-button slim-button" type="button" @click="closeCreateModal">
            {{ t("close") }}
          </button>
        </div>
        <form class="form-stack compact-form" @submit.prevent="createUser">
          <label class="field-label">
            {{ t("auth_register_username") }}
            <input v-model.trim="createForm.username" class="text-input" required />
          </label>
          <label class="field-label">
            {{ t("auth_register_display_name") }}
            <input v-model.trim="createForm.display_name" class="text-input" required />
          </label>
          <label class="field-label">
            {{ t("auth_register_email") }}
            <input v-model.trim="createForm.email" class="text-input" type="email" required />
          </label>
          <label class="field-label">
            {{ t("auth_password") }}
            <input v-model="createForm.password" class="text-input" type="password" required />
          </label>
          <label class="checkbox-row">
            <input v-model="createForm.is_admin" type="checkbox" />
            <span>{{ t("admin_is_admin") }}</span>
          </label>
          <div class="button-row">
            <button class="primary-button" type="submit" :disabled="saving">
              {{ saving ? t("saving") : t("admin_create_user_submit") }}
            </button>
            <button class="secondary-button" type="button" :disabled="saving" @click="closeCreateModal">
              {{ t("close") }}
            </button>
          </div>
        </form>
      </section>
    </div>

    <div v-if="editOpen && editTarget" class="admin-modal-backdrop" @click="closeEditModal">
      <section class="panel-card overlay-card admin-modal-card" @click.stop>
        <div class="panel-head">
          <div>
            <h3>{{ t("admin_user_detail") }}</h3>
            <p class="muted-text">{{ editTarget.username }} · {{ formatDate(editTarget.created_at) }}</p>
          </div>
          <button class="secondary-button slim-button" type="button" @click="closeEditModal">
            {{ t("close") }}
          </button>
        </div>
        <form class="form-stack compact-form" @submit.prevent="saveUser">
          <label class="field-label">
            {{ t("auth_register_display_name") }}
            <input v-model.trim="editForm.display_name" class="text-input" required />
          </label>
          <label class="field-label">
            {{ t("auth_register_email") }}
            <input v-model.trim="editForm.email" class="text-input" type="email" required />
          </label>
          <label class="checkbox-row">
            <input v-model="editForm.is_admin" type="checkbox" />
            <span>{{ t("admin_is_admin") }}</span>
          </label>
          <label class="checkbox-row">
            <input v-model="editForm.is_blocked" type="checkbox" />
            <span>{{ t("admin_is_blocked") }}</span>
          </label>
          <div class="data-list">
            <div class="data-row">
              <span>{{ t("auth_register_username") }}</span>
              <strong>{{ editTarget.username }}</strong>
            </div>
            <div class="data-row">
              <span>{{ t("admin_updated_at") }}</span>
              <strong>{{ formatDate(editTarget.updated_at) }}</strong>
            </div>
          </div>
          <div class="button-row">
            <button class="primary-button" type="submit" :disabled="saving">
              {{ saving ? t("saving") : t("admin_action_save") }}
            </button>
            <button class="secondary-button" type="button" :disabled="saving" @click="closeEditModal">
              {{ t("close") }}
            </button>
          </div>
        </form>
      </section>
    </div>

    <div v-if="resetOpen && resetTarget" class="admin-modal-backdrop" @click="closeResetModal">
      <section class="panel-card overlay-card admin-modal-card" @click.stop>
        <div class="panel-head">
          <div>
            <h3>{{ t("admin_reset_password") }}</h3>
            <p class="muted-text">{{ resetTarget.username }} · {{ resetTarget.email }}</p>
          </div>
          <button class="secondary-button slim-button" type="button" @click="closeResetModal">
            {{ t("close") }}
          </button>
        </div>
        <form class="form-stack compact-form" @submit.prevent="submitResetPassword">
          <label class="field-label">
            {{ t("admin_reset_password") }}
            <input v-model="resetPassword" class="text-input" type="password" required />
          </label>
          <div class="button-row">
            <button class="primary-button" type="submit" :disabled="saving || !resetPassword">
              {{ saving ? t("saving") : t("admin_reset_password_submit") }}
            </button>
            <button class="secondary-button" type="button" :disabled="saving" @click="closeResetModal">
              {{ t("close") }}
            </button>
          </div>
        </form>
      </section>
    </div>
  </section>
</template>
