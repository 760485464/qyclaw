<script setup>
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { apiFetch } from "../services/api";
import { useSessionStore } from "../stores/session";

const router = useRouter();
const session = useSessionStore();

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
          <div class="brand-subtitle">Vue3 重构版入口</div>
        </div>
      </div>

      <div class="segment-group">
        <button
          class="segment-button"
          :class="{ active: mode === 'login' }"
          type="button"
          @click="mode = 'login'"
        >
          登录
        </button>
        <button
          class="segment-button"
          :class="{ active: mode === 'register' }"
          type="button"
          @click="mode = 'register'"
        >
          注册
        </button>
      </div>

      <form v-if="mode === 'login'" class="form-stack" @submit.prevent="submitLogin">
        <label class="field-label">
          用户名或邮箱
          <input v-model="loginForm.username" class="text-input" />
        </label>
        <label class="field-label">
          密码
          <input v-model="loginForm.password" class="text-input" type="password" />
        </label>
        <button class="primary-button" type="submit" :disabled="loading">
          {{ loading ? "登录中..." : "登录" }}
        </button>
      </form>

      <form v-else class="form-stack" @submit.prevent="submitRegister">
        <label class="field-label">
          用户名
          <input v-model="registerForm.username" class="text-input" />
        </label>
        <label class="field-label">
          显示名
          <input v-model="registerForm.display_name" class="text-input" />
        </label>
        <label class="field-label">
          邮箱
          <input v-model="registerForm.email" class="text-input" />
        </label>
        <label class="field-label">
          密码
          <input v-model="registerForm.password" class="text-input" type="password" />
        </label>
        <button class="primary-button" type="submit" :disabled="loading">
          {{ loading ? "注册中..." : "注册并进入" }}
        </button>
      </form>

      <p v-if="error" class="error-text">{{ error }}</p>
    </div>
  </div>
</template>
