from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

MODES = ("bot", "account", "sand-direct")
DEFAULT_BACKEND = "https://api2.cursor.sh"

DEFAULT_MODEL_LIST: tuple[str, ...] = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5-1",
    "grok-4.6",
    "gpt-5.6-luna-high",
    "gpt-5.6-sol",
    "gemini-3.8-flash",
)

# 内置各模式默认勾选。设置页可改，新建节点套用。
_DEFAULT_MODE_ENABLED = {"bot": True, "account": True, "sand-direct": False}
_DEFAULT_MODE_MODELS = {
    "bot": ("grok-4.6", "gpt-5.6-luna-high"),
    "account": DEFAULT_MODEL_LIST,
    "sand-direct": DEFAULT_MODEL_LIST,
}


def normalize_model_list(raw: Any = None) -> list[str]:
    if raw is None:
        return list(DEFAULT_MODEL_LIST)
    if isinstance(raw, str):
        items = [part.strip() for part in raw.replace(",", "\n").splitlines()]
    elif isinstance(raw, (list, tuple)):
        items = [str(item or "").strip() for item in raw]
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for name in items:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out or list(DEFAULT_MODEL_LIST)


def _price_num(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number < 0 or number != number:
        return float(default)
    return number


def normalize_model_prices(
    raw: Any = None,
    *,
    model_list: Any = None,
    default_input: float = 2.0,
    default_output: float = 8.0,
) -> dict[str, dict[str, float]]:
    catalog = normalize_model_list(model_list)
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, dict[str, float]] = {}
    for name in catalog:
        item = src.get(name)
        if not isinstance(item, dict):
            item = {}
        out[name] = {
            "input": _price_num(item.get("input", item.get("input_price_per_1m")), default_input),
            "output": _price_num(item.get("output", item.get("output_price_per_1m")), default_output),
            "official_input": _price_num(
                item.get("official_input", item.get("official_input_price_per_1m")), 0.0
            ),
            "official_output": _price_num(
                item.get("official_output", item.get("official_output_price_per_1m")), 0.0
            ),
        }
    return out


def _item_for_model(settings: Any, model: str = "") -> dict[str, Any]:
    prices = getattr(settings, "model_prices", None) or {}
    item = prices.get(model) if isinstance(prices, dict) else None
    return item if isinstance(item, dict) else {}


def price_for_model(settings: Any, model: str = "") -> tuple[float, float]:
    default_in = _price_num(getattr(settings, "input_price_per_1m", 2.0), 2.0)
    default_out = _price_num(getattr(settings, "output_price_per_1m", 8.0), 8.0)
    item = _item_for_model(settings, model)
    if item:
        return (
            _price_num(item.get("input"), default_in),
            _price_num(item.get("output"), default_out),
        )
    return default_in, default_out


def official_price_for_model(settings: Any, model: str = "") -> tuple[float, float]:
    item = _item_for_model(settings, model)
    return (
        _price_num(item.get("official_input"), 0.0),
        _price_num(item.get("official_output"), 0.0),
    )


def default_mode_config(model_list: Any = None) -> dict[str, dict[str, Any]]:
    catalog = normalize_model_list(model_list)
    allowed = set(catalog)
    out: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        picked = [name for name in _DEFAULT_MODE_MODELS[mode] if name in allowed]
        out[mode] = {
            "enabled": _DEFAULT_MODE_ENABLED[mode],
            "models": picked or (list(catalog) if _DEFAULT_MODE_ENABLED[mode] else []),
        }
    return out


def normalize_mode_config(
    raw: Any,
    *,
    model_list: Any = None,
    defaults: Optional[dict[str, Any]] = None,
    allowed: Optional[set[str]] = None,
) -> dict[str, dict[str, Any]]:
    catalog = normalize_model_list(model_list)
    base = defaults if isinstance(defaults, dict) else default_mode_config(catalog)
    if not isinstance(raw, dict):
        return normalize_mode_config(base, model_list=catalog, defaults=base, allowed=set(catalog))
    permit = set(allowed) if allowed is not None else None
    out: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        src = raw.get(mode)
        src = src if isinstance(src, dict) else {}
        seed = base.get(mode) if isinstance(base.get(mode), dict) else {}
        enabled = bool(src["enabled"]) if "enabled" in src else bool(seed.get("enabled"))
        models_raw = src.get("models")
        if models_raw is None:
            models_raw = seed.get("models") or []
        models: list[str] = []
        seen: set[str] = set()
        for item in models_raw if isinstance(models_raw, (list, tuple)) else []:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            if permit is not None and name not in permit:
                continue
            seen.add(name)
            models.append(name)
        out[mode] = {"enabled": enabled, "models": models}
    return out


def mode_enabled(config: Any, mode: str) -> bool:
    cfg = normalize_mode_config(config)
    return bool(mode in cfg and cfg[mode]["enabled"])


def enabled_modes(config: Any) -> list[str]:
    cfg = normalize_mode_config(config)
    return [mode for mode in MODES if cfg[mode]["enabled"]]


def models_for_mode(config: Any, mode: str) -> list[str]:
    cfg = normalize_mode_config(config)
    if mode not in cfg:
        return []
    return list(cfg[mode]["models"] if cfg[mode]["enabled"] else [])


def modes_for_model(config: Any, model: str = "", requested: str = "") -> list[str]:
    cfg = normalize_mode_config(config)
    scan = [requested] if requested else list(MODES)
    out: list[str] = []
    for mode in scan:
        if mode not in cfg or not cfg[mode]["enabled"]:
            continue
        if model and model not in cfg[mode]["models"]:
            continue
        out.append(mode)
    return out


def effective_mode(
    default_mode: str, config: Any, requested: str = "", model: str = ""
) -> str:
    cfg = normalize_mode_config(config)
    candidates = modes_for_model(cfg, model, requested)
    if requested:
        if requested not in MODES:
            raise ValueError(f"未知推理模式 {requested}")
        if not cfg[requested]["enabled"]:
            raise ValueError(f"未启用 {requested} 模式")
        if model and requested not in candidates:
            raise ValueError(f"未开放模型 {model}")
        return requested
    fallback = default_mode if default_mode in MODES else "account"
    if fallback in candidates:
        return fallback
    if candidates:
        return candidates[0]
    if model:
        raise ValueError(f"没有已启用且开放模型 {model} 的推理模式")
    raise ValueError("没有已启用的推理模式")


DEFAULT_AGENT_HOST = "agentn.global.api5.cursor.sh"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_APP_PORT = 8788
DEFAULT_NODE_PORT_START = 8799


def mask_secret(value: str, *, kind: str = "token") -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if kind == "api_key" and text.startswith("crsr_") and len(text) > 14:
        return f"{text[:9]}…{text[-4:]}"
    if len(text) > 18:
        return f"{text[:6]}…{text[-4:]}"
    return "••••"


def is_placeholder_secret(value: Optional[str]) -> bool:
    if value is None:
        return True
    text = value.strip()
    if not text:
        return True
    return "…" in text or text.startswith("••••")


@dataclass
class NodeRecord:
    id: str
    name: str
    slug: str
    enabled: bool = True
    is_default: bool = False
    default_mode: str = "account"
    mode_config: dict[str, Any] = field(default_factory=default_mode_config)
    host: str = DEFAULT_LISTEN_HOST
    port: int = DEFAULT_NODE_PORT_START
    api_key: str = ""
    access_token: str = ""
    refresh_token: str = ""
    backend: str = DEFAULT_BACKEND
    machine_id: str = ""
    mac_machine_id: str = ""
    provision_on_missing: bool = True
    provision_on_start: bool = True
    provision_wait: float = 90.0
    provision_bg_wait: float = 300.0
    provision_prompt: str = ""
    account_client_type: str = "ide"
    account_client_version: str = "3.19.13"
    account_workspace: str = ""  # 空 = 不向 Agent 声明工作区
    agent_host: str = DEFAULT_AGENT_HOST
    provision_state: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    email: str = ""
    sub: str = ""
    fingerprint: str = ""
    created_at: int = 0
    updated_at: int = 0
    last_used_at: int = 0

    def public_dict(self, *, reveal: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["has_api_key"] = bool(self.api_key)
        data["has_access_token"] = bool(self.access_token)
        data["has_refresh_token"] = bool(self.refresh_token)
        return data


@dataclass
class AppSettings:
    host: str = DEFAULT_LISTEN_HOST
    port: int = DEFAULT_APP_PORT
    admin_token: str = ""
    default_node_id: str = ""
    model_list: list[str] = field(default_factory=lambda: list(DEFAULT_MODEL_LIST))
    mode_defaults: dict[str, Any] = field(default_factory=default_mode_config)
    allow_register: bool = True
    shop_url: str = ""
    input_price_per_1m: float = 2.0
    output_price_per_1m: float = 8.0
    model_prices: dict[str, dict[str, float]] = field(default_factory=dict)


def normalize_shop_url(raw: Any = "") -> str:
    url = str(raw or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return url[:500]


@dataclass
class RuntimeStatus:
    node_id: str
    enabled: bool
    listening: bool
    listen_error: str = ""
    listen_url: str = ""


@dataclass
class ApiToken:
    id: str
    name: str
    token: str
    enabled: bool = True
    created_at: int = 0
    last_used_at: int = 0
    request_count: int = 0
    user_id: str = ""
    username: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
            "user_id": self.user_id,
            "username": self.username,
        }


@dataclass
class UserRecord:
    id: str
    username: str
    password_hash: str = ""
    role: str = "user"
    enabled: bool = True
    balance: int = 0
    created_at: int = 0
    updated_at: int = 0
    token_count: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role if self.role in ("admin", "user") else "user",
            "enabled": self.enabled,
            "balance": self.balance,
            "balance_yuan": round(self.balance / 1_000_000, 6),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_count": self.token_count,
        }


@dataclass
class RedeemCard:
    id: str
    code: str
    amount: int
    used_by: str = ""
    used_name: str = ""
    used_at: int = 0
    created_at: int = 0
    note: str = ""
    enabled: bool = True

    def public_dict(self, *, reveal: bool = True) -> dict[str, Any]:
        code = self.code if reveal or not self.used_by else (self.code[:7] + "…")
        return {
            "id": self.id,
            "code": code,
            "amount": self.amount,
            "amount_yuan": round(self.amount / 1_000_000, 6),
            "used_by": self.used_by,
            "used_name": self.used_name,
            "used_at": self.used_at,
            "created_at": self.created_at,
            "note": self.note,
            "enabled": self.enabled,
        }


@dataclass
class LedgerEntry:
    id: int
    user_id: str
    kind: str
    amount: int
    balance: int
    note: str
    created_at: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "kind": self.kind,
            "amount": self.amount,
            "amount_yuan": round(self.amount / 1_000_000, 6),
            "balance": self.balance,
            "balance_yuan": round(self.balance / 1_000_000, 6),
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass
class RequestLog:
    id: int
    node_id: str
    node_name: str
    mode: str
    model: str
    protocol: str
    stream: bool
    status: int
    latency_ms: int
    error: str
    created_at: int
    token_id: str = ""
    token_name: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens or 0) + int(self.completion_tokens or 0)
