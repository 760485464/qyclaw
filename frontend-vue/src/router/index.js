import { createRouter, createWebHistory } from "vue-router";

import { useSessionStore } from "../stores/session";
import AdminI18nHomeView from "../views/admin/AdminI18nHomeView.vue";
import ConversationLiveI18nView from "../views/conversations/ConversationLiveI18nView.vue";
import GatewayI18nView from "../views/gateway/GatewayI18nView.vue";
import LoginI18nView from "../views/LoginI18nView.vue";
import McpWorkspaceI18nView from "../views/mcp/McpWorkspaceI18nView.vue";
import SkillMarketplaceI18nView from "../views/skills/SkillMarketplaceI18nView.vue";
import SkillWorkbenchI18nView from "../views/skills/SkillWorkbenchI18nView.vue";
import WorkbenchI18nView from "../views/WorkbenchI18nView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/workbench" },
    { path: "/login", name: "login", component: LoginI18nView, meta: { authless: true } },
    { path: "/workbench", name: "workbench", component: WorkbenchI18nView },
    { path: "/conversations/:id?", name: "conversations", component: ConversationLiveI18nView },
    { path: "/skills/mine", name: "skills-mine", component: SkillWorkbenchI18nView },
    { path: "/skills/marketplace", name: "skills-marketplace", component: SkillMarketplaceI18nView },
    { path: "/mcp", name: "mcp", component: McpWorkspaceI18nView },
    { path: "/gateway", name: "gateway", component: GatewayI18nView },
    { path: "/admin", name: "admin", component: AdminI18nHomeView, meta: { requiresAdmin: true } }
  ]
});

router.beforeEach(async (to) => {
  const session = useSessionStore();
  if (to.meta.authless) return true;
  if (!session.token) return "/login";
  if (!session.user) {
    await session.syncCurrentUser();
  }
  if (to.meta.requiresAdmin && !session.user?.is_admin) {
    return "/workbench";
  }
  return true;
});

export default router;
