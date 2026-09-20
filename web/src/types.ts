export type Mode = "bot" | "account" | "sand-direct";

export interface ModeConfigEntry {
  enabled: boolean;
  models: string[];
}

export type ModeConfig = Record<Mode, ModeConfigEntry>;

export type ModeCatalog = Record<Mode, { models: string[] }>;

export interface RuntimeInfo {
  enabled: boolean;
  listening: boolean;
  listen_error: string;
  listen_url: string;
  path_base: string;
}

export interface NodeRecord {
  id: string;
  name: string;
  slug: string;
  enabled: boolean;
  is_default: boolean;
  default_mode: Mode;
  mode_config?: ModeConfig;
  host: string;
  port: number;
  api_key: string;
  access_token: string;
  refresh_token: string;
  backend: string;
  machine_id: string;
  mac_machine_id: string;
  provision_on_missing: boolean;
  provision_on_start: boolean;
  provision_wait: number;
  provision_bg_wait: number;
  provision_prompt: string;
  account_client_type: string;
  account_client_version: string;
  account_workspace: string;
  agent_host: string;
  notes: string;
  email: string;
  sub: string;
  fingerprint: string;
  created_at: number;
  updated_at: number;
  last_used_at: number;
  has_api_key: boolean;
  has_access_token: boolean;
  has_refresh_token: boolean;
  runtime: RuntimeInfo;
}

export interface AppMeta {
  service: string;
  version: string;
  data_dir: string;
  db_path: string;
  listen: { host: string; port: number };
  admin_required: boolean;
  stats: {
    nodes: number;
    enabled: number;
    requests_24h: number;
    errors_24h: number;
    prompt_tokens_24h: number;
    completion_tokens_24h: number;
    total_tokens_24h: number;
    tokens: number;
    users?: number;
    default_node_id: string;
    default_node_name: string;
  };
  modes: Mode[];
  models: { id: string }[];
  model_list?: string[];
  mode_catalog?: ModeCatalog;
  default_mode_config?: ModeConfig;
  allow_register?: boolean;
  shop_url?: string;
  input_price_per_1m?: number;
  output_price_per_1m?: number;
  model_prices?: ModelPrices;
}

export interface AppSettings {
  host: string;
  port: number;
  admin_token: string;
  default_node_id: string;
  has_admin_token: boolean;
  model_list: string[];
  mode_defaults: ModeConfig;
  allow_register: boolean;
  shop_url?: string;
  input_price_per_1m: number;
  output_price_per_1m: number;
  model_prices: ModelPrices;
}

export interface UsageNums {
  requests: number;
  success: number;
  errors: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

export interface UsageBucket extends UsageNums {
  id: string;
  name: string;
}

export interface UsageRow extends UsageNums {
  day: string;
  token_id: string;
  token_name: string;
  node_id: string;
  node_name: string;
  model: string;
}

export interface UsageSummary {
  from: string;
  to: string;
  days: number;
  totals: UsageNums;
  by_day: UsageBucket[];
  by_token: UsageBucket[];
  by_node: UsageBucket[];
  by_model: UsageBucket[];
  rows: UsageRow[];
}

export interface GatewayToken extends Partial<UsageNums> {
  id: string;
  name: string;
  token: string;
  enabled: boolean;
  created_at: number;
  last_used_at: number;
  request_count: number;
  user_id?: string;
  username?: string;
}

export interface ModelPrice {
  input: number;
  output: number;
  official_input?: number;
  official_output?: number;
}

export type ModelPrices = Record<string, ModelPrice>;

export interface ShopModel {
  id: string;
  input_price_per_1m: number;
  output_price_per_1m: number;
  official_input_price_per_1m?: number;
  official_output_price_per_1m?: number;
}

export interface ShopInfo {
  allow_register: boolean;
  need_setup?: boolean;
  shop_url?: string;
  input_price_per_1m: number;
  output_price_per_1m: number;
  currency: string;
  model_list?: string[];
  mode_defaults?: ModeConfig;
  models?: ShopModel[];
}

export interface GatewayUser {
  id: string;
  username: string;
  role: "admin" | "user";
  enabled: boolean;
  balance: number;
  balance_yuan: number;
  created_at: number;
  updated_at?: number;
  token_count: number;
  session?: string;
  shop?: ShopInfo;
}

export interface RedeemCard {
  id: string;
  code: string;
  amount: number;
  amount_yuan: number;
  used_by: string;
  used_name: string;
  used_at: number;
  created_at: number;
  note: string;
  enabled: boolean;
}

export interface LedgerEntry {
  id: number;
  user_id: string;
  kind: string;
  amount: number;
  amount_yuan: number;
  balance: number;
  balance_yuan: number;
  note: string;
  created_at: number;
}

export interface RequestLog {
  id: number;
  node_id: string;
  node_name: string;
  mode: string;
  model: string;
  protocol: string;
  stream: boolean;
  status: number;
  latency_ms: number;
  error: string;
  created_at: number;
  token_id?: string;
  token_name?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  total_tokens?: number;
  cost?: number;
  cost_yuan?: number;
}

export const MODES: Mode[] = ["bot", "account", "sand-direct"];

export const MODE_LABEL: Record<Mode, string> = {
  bot: "Bot / Box relay",
  account: "Account / Agent",
  "sand-direct": "Sand direct",
};

export const MODE_HINT: Record<Mode, string> = {
  bot: "走 Box relay，目前主要是 grok 系列。",
  account: "走 Agent 推理，覆盖 Cursor 账号侧模型。",
  "sand-direct": "以 sand 身份直连 api2。当前不可用，默认关闭。",
};

export const DEFAULT_MODEL_LIST = [
  "claude-opus-5",
  "claude-sonnet-5",
  "claude-fable-5-1",
  "grok-4.6",
  "gpt-5.6-luna-high",
  "gpt-5.6-sol",
  "gemini-3.8-flash",
];

const BOT_DEFAULT_MODELS = ["grok-4.6", "gpt-5.6-luna-high"];

export function defaultModeConfig(modelList: string[] = DEFAULT_MODEL_LIST): ModeConfig {
  const catalog = modelList.length ? modelList : DEFAULT_MODEL_LIST;
  return {
    bot: { enabled: true, models: catalog.filter((m) => BOT_DEFAULT_MODELS.includes(m)) },
    account: { enabled: true, models: [...catalog] },
    "sand-direct": { enabled: false, models: [...catalog] },
  };
}

export function normalizeModeConfig(
  raw?: Partial<ModeConfig> | null,
  modelList: string[] = DEFAULT_MODEL_LIST,
  defaults?: ModeConfig,
): ModeConfig {
  const catalog = modelList.length ? modelList : DEFAULT_MODEL_LIST;
  const allow = new Set(catalog);
  const base = defaults || defaultModeConfig(catalog);
  const out: ModeConfig = { ...base };
  for (const mode of MODES) {
    const src = raw?.[mode];
    const seed = base[mode];
    if (!src) {
      out[mode] = { enabled: seed.enabled, models: (seed.models || []).filter((m) => allow.has(m)) };
      continue;
    }
    const models = (src.models || []).filter((m) => allow.has(m));
    out[mode] = { enabled: !!src.enabled, models };
  }
  return out;
}

export function enabledModesOf(config?: Partial<ModeConfig> | null): Mode[] {
  const cfg = normalizeModeConfig(config);
  return MODES.filter((m) => cfg[m].enabled);
}

export const emptyNodeDraft = (): { api_key: string; default_mode: Mode; mode_config: ModeConfig } => ({
  api_key: "",
  default_mode: "account",
  mode_config: defaultModeConfig(),
});
