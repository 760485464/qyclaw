<script setup>
import { computed } from "vue";
import { useRouter } from "vue-router";

import { useSessionStore } from "../stores/session";
import brandLogo from "../assets/qyclaw_logo.png";

const router = useRouter();
const session = useSessionStore();

const navItems = computed(() => {
  const base = [
    { label: "工作台", to: "/workbench" },
    { label: "会话中心", to: "/conversations" },
    { label: "我的技能", to: "/skills/mine" },
    { label: "技能市场", to: "/skills/marketplace" },
    { label: "平台网关", to: "/gateway" },
    { label: "MCP 中心", to: "/mcp" }
  ];
  if (session.user?.is_admin) {
    base.push({ label: "用户管理", to: "/admin" });
  }
  return base;
});

function logout() {
  session.clearSession();
  router.push("/login");
}
</script>

<template>
  <div class="shell">
    <aside class="shell-sidebar">
      <div class="brand-block">
        <img class="brand-logo" :src="brandLogo" alt="Qyclaw logo" />
        <div>
          <div class="brand-name">Qyclaw</div>
          <div class="brand-subtitle">Vue Console</div>
        </div>
      </div>

      <nav class="nav-list">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="nav-item"
        >
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>

    <main class="shell-main">
      <header class="shell-header">
        <div>
          <h1 class="page-heading">Qyclaw 控制台</h1>
          <p class="page-subheading">
            面向会话、技能、MCP 与平台网关的一体化工作台
          </p>
        </div>
        <div class="header-actions">
          <div class="user-chip">
            {{ session.user?.display_name || session.user?.username || "Guest" }}
          </div>
          <button class="secondary-button" type="button" @click="logout">
            退出登录
          </button>
        </div>
      </header>

      <section class="shell-content">
        <slot />
      </section>
    </main>
  </div>
</template>
