<script setup>
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useI18n } from "../i18n";
import { apiFetch } from "../services/api";
import { useSessionStore } from "../stores/session";

const router = useRouter();
const session = useSessionStore();
const { t, toggleLocale } = useI18n();

const mode = ref("login");
const loading = ref(false);
const error = ref("");

const loginForm = reactive({
  username: "",
  password: ""
});

const registerForm = reactive({
  username: "",
  display_name: "",
  email: "",
  password: ""
});

async function submitLogin() {
  loading.value = true;
  error.value = "";
  try {
    const data = await apiFetch("/auth/login", {
      method: "POST",
      body: JSON.stringify(loginForm)
    });
    session.setSession(data.access_token, data.user);
    router.push("/workbench");
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function submitRegister() {
  loading.value = true;
  error.value = "";
  try {
    const data = await apiFetch("/auth/register", {
      method: "POST",
      body: JSON.stringify(registerForm)
    });
    session.setSession(data.access_token, data.user);
    router.push("/workbench");
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="auth-screen">
    <div class="auth-card">
      <div class="brand-block auth-brand">
        <div class="brand-mark">Q</div>
        <div>
          <div class="brand-name">Qyclaw</div>
          <div class="brand-subtitle">{{ t("auth_entry_subtitle") }}</div>
        </div>
      </div>

      <div class="header-actions" style="justify-content: flex-end; margin-bottom: 16px;">
        <button class="secondary-button" type="button" @click="toggleLocale">
          {{ t("shell_lang_toggle") }}
        </button>
      </div>

      <div class="segment-group">
        <button class="segment-button" :class="{ active: mode === 'login' }" type="button" @click="mode = 'login'">
          {{ t("auth_login_tab") }}
        </button>
        <button class="segment-button" :class="{ active: mode === 'register' }" type="button" @click="mode = 'register'">
          {{ t("auth_register_tab") }}
        </button>
      </div>

      <form v-if="mode === 'login'" class="form-stack" @submit.prevent="submitLogin">
        <label class="field-label">
          {{ t("auth_login_id") }}
          <input v-model="loginForm.username" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("auth_password") }}
          <input v-model="loginForm.password" class="text-input" type="password" />
        </label>
        <button class="primary-button" type="submit" :disabled="loading">
          {{ loading ? t("auth_login_loading") : t("auth_login_submit") }}
        </button>
      </form>

      <form v-else class="form-stack" @submit.prevent="submitRegister">
        <label class="field-label">
          {{ t("auth_register_username") }}
          <input v-model="registerForm.username" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("auth_register_display_name") }}
          <input v-model="registerForm.display_name" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("auth_register_email") }}
          <input v-model="registerForm.email" class="text-input" />
        </label>
        <label class="field-label">
          {{ t("auth_password") }}
          <input v-model="registerForm.password" class="text-input" type="password" />
        </label>
        <button class="primary-button" type="submit" :disabled="loading">
          {{ loading ? t("auth_register_loading") : t("auth_register_submit") }}
        </button>
      </form>

      <p v-if="error" class="error-text">{{ error }}</p>
    </div>
  </div>
</template>
