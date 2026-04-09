import { defineStore } from "pinia";

import { apiFetch } from "../services/api";

const TOKEN_KEY = "qyclaw_token";
const USER_KEY = "qyclaw_user";

export const useSessionStore = defineStore("session", {
  state: () => ({
    token: "",
    user: null,
    ready: false
  }),
  actions: {
    bootstrap() {
      if (this.ready) return;
      this.token = localStorage.getItem(TOKEN_KEY) || "";
      const rawUser = localStorage.getItem(USER_KEY);
      this.user = rawUser ? JSON.parse(rawUser) : null;
      this.ready = true;
    },
    setSession(token, user) {
      this.token = token;
      this.user = user;
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, JSON.stringify(user));
    },
    clearSession() {
      this.token = "";
      this.user = null;
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
    },
    async syncCurrentUser() {
      if (!this.token) {
        this.clearSession();
        return null;
      }
      try {
        const me = await apiFetch("/auth/me");
        this.user = me;
        localStorage.setItem(USER_KEY, JSON.stringify(me));
        return me;
      } catch (error) {
        this.clearSession();
        throw error;
      }
    }
  }
});
