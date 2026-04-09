import { useSessionStore } from "../stores/session";

export const API_BASE =
  import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000/api/v1";

export const API_ORIGIN = API_BASE.replace(/\/api\/v1$/, "");
let redirectingToLogin = false;

export function handleUnauthorizedResponse(response) {
  if (response.status !== 401) {
    return false;
  }
  const session = useSessionStore();
  session.clearSession();
  if (!redirectingToLogin && window.location.pathname !== "/login") {
    redirectingToLogin = true;
    window.location.assign("/login");
  }
  return true;
}

export async function apiFetch(path, options = {}) {
  const session = useSessionStore();
  const headers = new Headers(options.headers || {});

  if (!headers.has("Content-Type") && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (session.token) {
    headers.set("Authorization", `Bearer ${session.token}`);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    handleUnauthorizedResponse(response);
    throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  }
  return data;
}

export async function apiUpload(path, formData, options = {}) {
  return apiFetch(path, {
    ...options,
    method: options.method || "POST",
    body: formData,
    headers: options.headers || {}
  });
}

export function toAssetUrl(path) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith("/")) return `${API_ORIGIN}${path}`;
  return `${API_ORIGIN}/${path}`;
}

export function useBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
