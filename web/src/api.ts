import type {
  AppMeta,
  AppSettings,
  GatewayToken,
  GatewayUser,
  LedgerEntry,
  NodeRecord,
  RedeemCard,
  RequestLog,
  ShopInfo,
  UsageSummary,
} from "./types";

const TOKEN_KEY = "cursor-gateway-admin-token";

export function getAdminToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setAdminToken(token: string) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  const token = getUserSession() || getAdminToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(path, { ...init, headers });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!res.ok) {
    const err = (data && typeof data === "object" ? data : {}) as {
      error?: string;
      message?: string;
      detail?: string;
    };
    throw new Error(err.message || err.detail || err.error || `HTTP ${res.status}`);
  }
  return data as T;
}

export const api = {
  meta: () => req<AppMeta>("/api/meta"),
  settings: () => req<AppSettings>("/api/settings"),
  saveSettings: (body: Partial<AppSettings>) =>
    req<AppSettings>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  nodes: () => req<{ nodes: NodeRecord[] }>("/api/nodes"),
  node: (id: string) => req<NodeRecord>(`/api/nodes/${id}`),
  createNode: (body: Record<string, unknown>) =>
    req<NodeRecord>("/api/nodes", { method: "POST", body: JSON.stringify(body) }),
  updateNode: (id: string, body: Record<string, unknown>) =>
    req<NodeRecord>(`/api/nodes/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteNode: (id: string) =>
    req<{ ok: boolean }>(`/api/nodes/${id}`, { method: "DELETE" }),
  startNode: (id: string) =>
    req<NodeRecord>(`/api/nodes/${id}/start`, { method: "POST" }),
  stopNode: (id: string) =>
    req<NodeRecord>(`/api/nodes/${id}/stop`, { method: "POST" }),
  setDefault: (id: string) =>
    req<NodeRecord>(`/api/nodes/${id}/default`, { method: "POST" }),
  exchange: (id: string) =>
    req<{ ok: boolean; email: string; expires_at: number; node: NodeRecord }>(
      `/api/nodes/${id}/exchange`,
      { method: "POST" },
    ),
  provision: (id: string, body: Record<string, unknown> = {}) =>
    req<Record<string, unknown>>(`/api/nodes/${id}/provision`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  logs: (nodeId = "", limit = 20, offset = 0) =>
    req<{ logs: RequestLog[]; total: number }>(
      `/api/logs?limit=${limit}&offset=${offset}${nodeId ? `&node_id=${encodeURIComponent(nodeId)}` : ""}`,
    ),
  usage: (days = 30, tokenId = "", nodeId = "") => {
    const q = new URLSearchParams({ days: String(days) });
    if (tokenId) q.set("token_id", tokenId);
    if (nodeId) q.set("node_id", nodeId);
    return req<UsageSummary>(`/api/usage?${q.toString()}`);
  },
  tokens: () => req<{ tokens: GatewayToken[] }>("/api/tokens"),
  createToken: (name: string) =>
    req<GatewayToken>("/api/tokens", { method: "POST", body: JSON.stringify({ name }) }),
  updateToken: (id: string, body: { name?: string; enabled?: boolean }) =>
    req<GatewayToken>(`/api/tokens/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteToken: (id: string) => req<{ ok: boolean }>(`/api/tokens/${id}`, { method: "DELETE" }),
  users: () => req<{ users: GatewayUser[] }>("/api/users"),
  createUser: (body: { username: string; password: string; yuan?: number; role?: "admin" | "user" }) =>
    req<GatewayUser>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (id: string, body: { enabled?: boolean; password?: string; role?: "admin" | "user" }) =>
    req<GatewayUser>(`/api/users/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteUser: (id: string) => req<{ ok: boolean }>(`/api/users/${id}`, { method: "DELETE" }),
  rechargeUser: (id: string, yuan: number, note = "") =>
    req<GatewayUser>(`/api/users/${id}/recharge`, { method: "POST", body: JSON.stringify({ yuan, note }) }),
  cards: () => req<{ cards: RedeemCard[] }>("/api/cards"),
  createCards: (count: number, yuan: number, note = "") =>
    req<{ cards: RedeemCard[] }>("/api/cards", { method: "POST", body: JSON.stringify({ count, yuan, note }) }),
  updateCard: (id: string, body: { enabled: boolean }) =>
    req<RedeemCard>(`/api/cards/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteCard: (id: string) => req<{ ok: boolean }>(`/api/cards/${id}`, { method: "DELETE" }),
  deleteCards: (ids: string[]) =>
    req<{ ok: boolean; deleted: number }>("/api/cards/batch-delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  updateCards: (ids: string[], enabled: boolean) =>
    req<{ ok: boolean; updated: number; errors: string[] }>("/api/cards/batch-update", {
      method: "POST",
      body: JSON.stringify({ ids, enabled }),
    }),
};

const USER_KEY = "cursor-gateway-user-session";

export function getUserSession(): string {
  return localStorage.getItem(USER_KEY) || "";
}

export function setUserSession(token: string) {
  if (token) localStorage.setItem(USER_KEY, token);
  else localStorage.removeItem(USER_KEY);
}

async function userReq<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  const session = getUserSession();
  if (session) headers.set("Authorization", `Bearer ${session}`);
  const res = await fetch(path, { ...init, headers });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!res.ok) {
    const err = (data && typeof data === "object" ? data : {}) as {
      error?: string;
      message?: string;
      detail?: string;
    };
    throw new Error(err.message || err.detail || err.error || `HTTP ${res.status}`);
  }
  return data as T;
}

export const userApi = {
  shop: () => userReq<ShopInfo>("/api/user/shop"),
  captcha: () => userReq<{ id: string; image: string; ttl: number }>("/api/user/captcha"),
  setup: (username: string, password: string) =>
    userReq<GatewayUser>("/api/user/setup", { method: "POST", body: JSON.stringify({ username, password }) }),
  register: (username: string, password: string, captchaId: string, captcha: string) =>
    userReq<GatewayUser>("/api/user/register", {
      method: "POST",
      body: JSON.stringify({ username, password, captcha_id: captchaId, captcha }),
    }),
  login: (username: string, password: string, captchaId: string, captcha: string) =>
    userReq<GatewayUser>("/api/user/login", {
      method: "POST",
      body: JSON.stringify({ username, password, captcha_id: captchaId, captcha }),
    }),
  logout: () => userReq<{ ok: boolean }>("/api/user/logout", { method: "POST" }),
  me: () => userReq<GatewayUser>("/api/user/me"),
  redeem: (code: string) =>
    userReq<GatewayUser>("/api/user/redeem", { method: "POST", body: JSON.stringify({ code }) }),
  tokens: () => userReq<{ tokens: GatewayToken[] }>("/api/user/tokens"),
  createToken: (name: string) =>
    userReq<GatewayToken>("/api/user/tokens", { method: "POST", body: JSON.stringify({ name }) }),
  updateToken: (id: string, body: { name?: string; enabled?: boolean }) =>
    userReq<GatewayToken>(`/api/user/tokens/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteToken: (id: string) => userReq<{ ok: boolean }>(`/api/user/tokens/${id}`, { method: "DELETE" }),
  ledger: (limit = 50) => userReq<{ ledger: LedgerEntry[] }>(`/api/user/ledger?limit=${limit}`),
  usage: (days = 30) => userReq<UsageSummary & { estimated_cost_yuan?: number }>(`/api/user/usage?days=${days}`),
  logs: (limit = 20, offset = 0) =>
    userReq<{ logs: RequestLog[]; total: number }>(`/api/user/logs?limit=${limit}&offset=${offset}`),
  changePassword: (oldPassword: string, password: string) =>
    userReq<{ ok: boolean }>("/api/user/password", {
      method: "PUT",
      body: JSON.stringify({ old_password: oldPassword, password }),
    }),
  saveModelAliases: (aliases: Record<string, string>) =>
    userReq<{ ok: boolean; model_aliases: Record<string, string> }>("/api/user/model-aliases", {
      method: "PUT",
      body: JSON.stringify({ aliases }),
    }),
};
