<script setup>
import { computed, onMounted } from "vue";
import { RouterView, useRoute } from "vue-router";

import AppShell from "./layouts/AppShellI18n.vue";
import { useLocaleStore } from "./stores/locale";
import { useSessionStore } from "./stores/session";

const route = useRoute();
const session = useSessionStore();
const localeStore = useLocaleStore();
const isAuthRoute = computed(() => route.meta.authless === true);

onMounted(() => {
  localeStore.bootstrap();
  session.bootstrap();
});
</script>

<template>
  <RouterView v-if="isAuthRoute" />
  <AppShell v-else>
    <RouterView />
  </AppShell>
</template>
