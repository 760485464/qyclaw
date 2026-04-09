<script setup>
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useI18n } from "../i18n";
import { useSessionStore } from "../stores/session";
import brandLogo from "../assets/qyclaw_logo.png";

const router = useRouter();
const route = useRoute();
const session = useSessionStore();
const { t, toggleLocale } = useI18n();

const navItems = computed(() => {
  const base = [
    { label: t("nav_workbench"), to: "/workbench" },
    { label: t("nav_conversations"), to: "/conversations" },
    { label: t("nav_skills_mine"), to: "/skills/mine" },
    { label: t("nav_skills_marketplace"), to: "/skills/marketplace" },
    { label: t("nav_mcp"), to: "/mcp" },
    { label: t("nav_gateway"), to: "/gateway" }
  ];
  if (session.user?.is_admin) {
    base.push({ label: t("nav_admin"), to: "/admin" });
  }
  return base;
});

function logout() {
  session.clearSession();
  router.push("/login");
}

function isNavActive(target) {
  const currentPath = route.path || "";
  if (target === "/conversations") {
    return currentPath === target || currentPath.startsWith("/conversations/");
  }
  return currentPath === target || currentPath.startsWith(`${target}/`);
}
</script>

<template>
  <div class="shell">
    <aside class="shell-sidebar">
      <div class="brand-block">
        <img class="brand-logo" :src="brandLogo" alt="Qyclaw logo" />
        <div>
          <div class="brand-name">Qyclaw</div>
          <div class="brand-subtitle">{{ t("shell_brand_subtitle") }}</div>
        </div>
      </div>

      <nav class="nav-list">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :class="['nav-item', isNavActive(item.to) ? 'is-active' : '']"
        >
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>

    <main class="shell-main">
      <header class="shell-header">
        <div class="shell-header-title">
          <h1 class="page-heading">{{ t("shell_title") }}</h1>
          <p class="page-subheading shell-inline-subheading">{{ t("shell_description") }}</p>
        </div>
        <div class="header-actions">
          <button class="secondary-button" type="button" @click="toggleLocale">
            {{ t("shell_lang_toggle") }}
          </button>
          <div class="user-chip">
            {{ session.user?.display_name || session.user?.username || t("shell_guest") }}
          </div>
          <button class="secondary-button" type="button" @click="logout">
            {{ t("shell_logout") }}
          </button>
        </div>
      </header>

      <section class="shell-content">
        <slot />
      </section>
    </main>
  </div>
</template>
