import { defineStore } from "pinia";

const LOCALE_KEY = "qyclaw_locale";

export const useLocaleStore = defineStore("locale", {
  state: () => ({
    locale: "zh-CN",
    ready: false
  }),
  actions: {
    bootstrap() {
      if (this.ready) return;
      const saved = localStorage.getItem(LOCALE_KEY);
      this.locale = saved || "zh-CN";
      this.ready = true;
    },
    setLocale(locale) {
      this.locale = locale;
      localStorage.setItem(LOCALE_KEY, locale);
    },
    toggleLocale() {
      this.setLocale(this.locale === "zh-CN" ? "en-US" : "zh-CN");
    }
  }
});
