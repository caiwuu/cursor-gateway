"""管理 API：节点 CRUD、启停、换 token、provision。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from . import gateway as GW
from .credentials import (
    UpstreamError,
    account_fingerprint,
    email_of,
    exchange_profile,
    jwt_claims,
)
from .accounts import yuan_to_micros
from .models import (
    DEFAULT_BACKEND,
    MODES,
    enabled_modes,
    is_placeholder_secret,
    normalize_mode_config,
)
from .paths import data_dir, db_path
from .runtime import Registry
from .store import Store

router = APIRouter(prefix="/api")


def _store(request: Request) -> Store:
    return request.app.state.store


def _registry(request: Request) -> Registry:
    return request.app.state.registry


async def _apply_key_profile(body: dict[str, Any]) -> dict[str, Any]:
    api_key = str(body.get("api_key") or "").strip()
    if not api_key or is_placeholder_secret(api_key):
        return body
    try:
        profile = await exchange_profile(api_key, str(body.get("backend") or DEFAULT_BACKEND))
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    out = dict(body)
    out["api_key"] = api_key
    for key in ("access_token", "refresh_token", "email", "sub", "fingerprint"):
        value = profile.get(key)
        if value:
            out[key] = value
    display = str(profile.get("email") or profile.get("name") or "").strip()
    if display:
        out["name"] = display
        if not str(out.get("slug") or "").strip():
            out["slug"] = display
    return out


def _node_view(request: Request, rec, *, reveal: bool = True) -> dict[str, Any]:
    status = _registry(request).status_of(rec.id)
    data = rec.public_dict(reveal=True)
    data["runtime"] = {
        "enabled": status.enabled,
        "listening": status.listening,
        "listen_error": status.listen_error,
        "listen_url": status.listen_url,
        "path_base": f"/n/{rec.slug}",
    }
    return data


@router.get("/meta")
def meta(request: Request) -> dict[str, Any]:
    settings = _store(request).get_app_settings()
    return {
        "service": "cursor-gateway",
        "version": "1.1.0",
        "data_dir": str(data_dir()),
        "db_path": str(db_path()),
        "listen": {"host": settings.host, "port": settings.port},
        "admin_required": bool(settings.admin_token),
        "stats": _store(request).stats(),
        "modes": list(MODES),
        "models": [{"id": m} for m in settings.model_list],
        "model_list": settings.model_list,
        "mode_catalog": {mode: {"models": list(settings.model_list)} for mode in MODES},
        "default_mode_config": settings.mode_defaults,
        "allow_register": settings.allow_register,
        "shop_url": settings.shop_url,
        "input_price_per_1m": settings.input_price_per_1m,
        "output_price_per_1m": settings.output_price_per_1m,
        "model_prices": settings.model_prices,
    }


def _settings_view(s, *, note: str = "") -> dict[str, Any]:
    data = {
        "host": s.host,
        "port": s.port,
        "admin_token": s.admin_token,
        "default_node_id": s.default_node_id,
        "has_admin_token": bool(s.admin_token),
        "model_list": s.model_list,
        "mode_defaults": s.mode_defaults,
        "allow_register": s.allow_register,
        "shop_url": s.shop_url,
        "input_price_per_1m": s.input_price_per_1m,
        "output_price_per_1m": s.output_price_per_1m,
        "model_prices": s.model_prices,
    }
    if note:
        data["note"] = note
    return data


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return _settings_view(_store(request).get_app_settings())


@router.put("/settings")
def put_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    s = _store(request).update_app_settings(body)
    return _settings_view(s, note="host/port 需重启 cursor-gateway 进程后生效")


@router.get("/nodes")
def list_nodes(request: Request) -> dict[str, Any]:
    nodes = [_node_view(request, n) for n in _store(request).list_nodes()]
    return {"nodes": nodes}


def _node_input(body: dict[str, Any], *, creating: bool, settings) -> dict[str, Any]:
    """管理台只接受 API Key、默认模式和各推理模式的模型配置。"""
    out: dict[str, Any] = {}
    if creating or "api_key" in body:
        out["api_key"] = str(body.get("api_key") or "").strip()
    if creating or "default_mode" in body:
        out["default_mode"] = str(body.get("default_mode") or "account")
    if creating and "mode_config" not in body:
        out["mode_config"] = settings.mode_defaults
    elif creating or "mode_config" in body:
        out["mode_config"] = normalize_mode_config(
            body.get("mode_config"),
            model_list=settings.model_list,
            defaults=settings.mode_defaults,
            allowed=set(settings.model_list),
        )
    return out


def _validate_mode_fields(body: dict[str, Any], current=None, *, settings=None) -> dict[str, Any]:
    defaults = getattr(settings, "mode_defaults", None)
    catalog = getattr(settings, "model_list", None)
    allowed = set(catalog) if catalog else None
    cfg = (
        normalize_mode_config(
            body["mode_config"], model_list=catalog, defaults=defaults, allowed=allowed
        )
        if "mode_config" in body
        else normalize_mode_config(
            getattr(current, "mode_config", None),
            model_list=catalog,
            defaults=defaults,
        )
    )
    if "mode_config" in body:
        body["mode_config"] = cfg
    default = str(body.get("default_mode") or getattr(current, "default_mode", None) or "account")
    if default not in MODES:
        raise HTTPException(400, f"default_mode 必须是 {', '.join(MODES)}")
    active = enabled_modes(cfg)
    if not active:
        raise HTTPException(400, "至少启用一种推理模式")
    for mode in active:
        if not cfg[mode]["models"]:
            raise HTTPException(400, f"{mode} 已启用，请至少选择一个可用模型")
    if default not in active:
        body["default_mode"] = active[0]
    else:
        body["default_mode"] = default
    return body


@router.post("/nodes")
async def create_node(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    settings = _store(request).get_app_settings()
    body = _node_input(body, creating=True, settings=settings)
    body = _validate_mode_fields(body, settings=settings)
    api_key = str(body.get("api_key") or "").strip()
    if not api_key or is_placeholder_secret(api_key):
        raise HTTPException(400, "请填写有效的 API Key")
    body = await _apply_key_profile(body)
    if not str(body.get("name") or "").strip():
        body["name"] = str(body.get("email") or "").strip() or "未命名节点"
    rec = _store(request).create_node(body)
    _registry(request).reload_records()
    if rec.enabled:
        try:
            await _registry(request).start_node(rec.id, GW.create_node_app)
        except Exception:
            pass
    created = _store(request).get_node(rec.id)
    return _node_view(request, created)


@router.get("/nodes/{node_ref}")
def get_node(request: Request, node_ref: str, reveal: bool = False) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    return _node_view(request, rec, reveal=reveal)


@router.put("/nodes/{node_ref}")
async def update_node(request: Request, node_ref: str, body: dict[str, Any]) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    settings = _store(request).get_app_settings()
    body = _node_input(body, creating=False, settings=settings)
    if "default_mode" in body or "mode_config" in body:
        body = _validate_mode_fields(body, rec, settings=settings)
    if "api_key" in body:
        if not str(body.get("api_key") or "").strip() or is_placeholder_secret(body.get("api_key")):
            body.pop("api_key", None)
        else:
            body = await _apply_key_profile(body)
    _store(request).update_node(rec.id, body)
    _registry(request).reload_records()
    fresh = _store(request).get_node(rec.id)
    return _node_view(request, fresh)


@router.delete("/nodes/{node_ref}")
async def delete_node(request: Request, node_ref: str) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    await _registry(request).stop_node(rec.id, disable=False)
    _store(request).delete_node(rec.id)
    _registry(request).reload_records()
    return {"ok": True, "id": rec.id}


@router.post("/nodes/{node_ref}/start")
async def start_node(request: Request, node_ref: str) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    status = await _registry(request).start_node(rec.id, GW.create_node_app)
    fresh = _store(request).get_node(rec.id)
    view = _node_view(request, fresh)
    view["runtime"]["listen_error"] = status.listen_error
    return view


@router.post("/nodes/{node_ref}/stop")
async def stop_node(request: Request, node_ref: str) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    await _registry(request).stop_node(rec.id, disable=True)
    fresh = _store(request).get_node(rec.id)
    return _node_view(request, fresh)


@router.post("/nodes/{node_ref}/default")
def set_default(request: Request, node_ref: str) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    _store(request).update_app_settings({"default_node_id": rec.id})
    _store(request).update_node(rec.id, {"is_default": True})
    return _node_view(request, _store(request).get_node(rec.id))


@router.post("/nodes/{node_ref}/exchange")
async def exchange(request: Request, node_ref: str) -> dict[str, Any]:
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    rt = _registry(request).get(rec.id)
    if rt is None:
        _registry(request).reload_records()
        rt = _registry(request).get(rec.id)
    if rt is None:
        raise HTTPException(500, "节点运行时未就绪")
    if not rec.api_key:
        raise HTTPException(400, "该节点没有 api_key，无法换 token")
    try:
        creds = await rt.cm.get(api_key=rec.api_key, force=True)
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    rt._persist(creds)
    fresh = _store(request).get_node(rec.id)
    return {
        "ok": True,
        "email": creds.email,
        "sub": creds.sub,
        "fingerprint": creds.fingerprint,
        "expires_at": creds.expires_at,
        "node": _node_view(request, fresh),
    }


@router.post("/nodes/{node_ref}/provision")
async def provision(request: Request, node_ref: str, body: Optional[dict[str, Any]] = None):
    rec = _store(request).get_node(node_ref)
    if rec is None:
        raise HTTPException(404, "节点不存在")
    rt = _registry(request).resolve(rec.id, require_enabled=False)
    try:
        creds = await rt.cm.get(api_key=rec.api_key, access_token=rec.access_token)
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    wait = rec.provision_wait
    if isinstance(body, dict) and isinstance(body.get("wait_seconds"), (int, float)):
        wait = float(body["wait_seconds"])
    force = bool((body or {}).get("refresh"))
    try:
        from . import provision as PV

        result = await PV.provision_box(
            rt.shared.client,
            rt.shared.box_manager,
            creds,
            wait_seconds=wait,
            force=force,
            get_state=rt.provision_state,
            set_state=rt.set_provision_entry,
            prompt=rec.provision_prompt,
        )
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return result


def _log_view(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "node_id": item.node_id,
        "node_name": item.node_name,
        "mode": item.mode,
        "model": item.model,
        "protocol": item.protocol,
        "stream": item.stream,
        "status": item.status,
        "latency_ms": item.latency_ms,
        "error": item.error,
        "created_at": item.created_at,
        "token_id": item.token_id,
        "token_name": item.token_name,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "cache_read_tokens": item.cache_read_tokens,
        "cache_write_tokens": item.cache_write_tokens,
        "total_tokens": item.total_tokens,
    }


@router.get("/tokens")
def list_tokens(request: Request) -> dict[str, Any]:
    store = _store(request)
    usage = store.usage_by_token()
    tokens = []
    for rec in store.list_tokens():
        data = rec.public_dict()
        data.update(usage.get(rec.id, {
            "requests": rec.request_count,
            "success": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
        }))
        tokens.append(data)
    return {"tokens": tokens}


@router.post("/tokens")
def create_token(request: Request, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = body or {}
    name = str(payload.get("name") or "").strip() or "未命名令牌"
    user_id = str(payload.get("user_id") or "").strip()
    rec = _store(request).create_token(name, user_id=user_id)
    return rec.public_dict()


@router.put("/tokens/{token_id}")
def update_token(request: Request, token_id: str, body: dict[str, Any]) -> dict[str, Any]:
    rec = _store(request).update_token(token_id, body)
    if rec is None:
        raise HTTPException(404, "令牌不存在")
    return rec.public_dict()


@router.delete("/tokens/{token_id}")
def delete_token(request: Request, token_id: str) -> dict[str, Any]:
    if not _store(request).delete_token(token_id):
        raise HTTPException(404, "令牌不存在")
    return {"ok": True, "id": token_id}


@router.get("/logs")
def logs(
    request: Request,
    node_id: str = "",
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items, total = _store(request).list_logs(node_id=node_id, limit=limit, offset=offset)
    return {"logs": [_log_view(x) for x in items], "total": total}


@router.get("/usage")
def usage(
    request: Request,
    days: int = Query(30, ge=1, le=366),
    token_id: str = "",
    node_id: str = "",
) -> dict[str, Any]:
    return _store(request).usage_summary(days=days, token_id=token_id, node_id=node_id)


@router.get("/users")
def list_users(request: Request) -> dict[str, Any]:
    return {"users": [u.public_dict() for u in _store(request).list_users()]}


@router.post("/users")
def create_user(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        user = _store(request).create_user(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
            yuan=float(body.get("yuan") or 0),
            role=str(body.get("role") or "user"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return user.public_dict()


@router.put("/users/{user_id}")
def update_user(request: Request, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        rec = _store(request).update_user(user_id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if rec is None:
        raise HTTPException(404, "用户不存在")
    return rec.public_dict()


@router.delete("/users/{user_id}")
def delete_user(request: Request, user_id: str) -> dict[str, Any]:
    try:
        deleted = _store(request).delete_user(user_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "用户不存在")
    return {"ok": True}


@router.post("/users/{user_id}/recharge")
def recharge_user(request: Request, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    amount = yuan_to_micros(body.get("yuan"))
    if amount <= 0:
        raise HTTPException(400, "充值金额必须大于 0")
    try:
        rec = _store(request).adjust_balance(
            user_id, amount, kind="recharge", note=str(body.get("note") or "后台充值")
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return rec.public_dict()


@router.put("/users/{user_id}/balance")
def set_user_balance(request: Request, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if "yuan" not in body:
        raise HTTPException(400, "请填写余额")
    try:
        yuan = float(body.get("yuan"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "余额必须是数字") from exc
    if yuan < 0 or yuan != yuan:
        raise HTTPException(400, "余额不能为负数")
    try:
        rec = _store(request).set_balance(
            user_id,
            yuan_to_micros(yuan),
            note=str(body.get("note") or "后台调账"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return rec.public_dict()


@router.get("/cards")
def list_cards(request: Request) -> dict[str, Any]:
    return {"cards": [c.public_dict(reveal=True) for c in _store(request).list_cards()]}


@router.post("/cards")
def create_cards(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        cards = _store(request).create_cards(
            int(body.get("count") or 1),
            float(body.get("yuan") or 0),
            note=str(body.get("note") or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"cards": [c.public_dict(reveal=True) for c in cards]}


@router.put("/cards/{card_id}")
def update_card(request: Request, card_id: str, body: dict[str, Any]) -> dict[str, Any]:
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if enabled is None:
        raise HTTPException(400, "请提供 enabled")
    try:
        rec = _store(request).update_card(card_id, enabled=bool(enabled))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return rec.public_dict(reveal=True)


@router.delete("/cards/{card_id}")
def delete_card(request: Request, card_id: str) -> dict[str, Any]:
    deleted = _store(request).delete_cards([card_id])
    if not deleted:
        raise HTTPException(404, "卡密不存在")
    return {"ok": True}


@router.post("/cards/batch-delete")
def delete_cards(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        raise HTTPException(400, "请提供 ids")
    deleted = _store(request).delete_cards([str(x) for x in ids])
    return {"ok": True, "deleted": deleted}


@router.post("/cards/batch-update")
def update_cards(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    ids = body.get("ids") if isinstance(body, dict) else None
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(ids, list) or enabled is None:
        raise HTTPException(400, "请提供 ids 和 enabled")
    store = _store(request)
    updated = 0
    errors: list[str] = []
    for card_id in ids:
        try:
            store.update_card(str(card_id), enabled=bool(enabled))
            updated += 1
        except ValueError as exc:
            errors.append(str(exc))
    return {"ok": True, "updated": updated, "errors": errors[:8]}


@router.post("/preview-key")
def preview_key(body: dict[str, Any]) -> dict[str, Any]:
    """本地解析 JWT / 指纹，不访问上游。"""
    token = str(body.get("access_token") or "")
    claims = jwt_claims(token) if token else {}
    sub = str(claims.get("sub") or "")
    return {
        "email": email_of(claims),
        "sub": sub,
        "fingerprint": account_fingerprint(sub, token) if token else "",
        "exp": claims.get("exp"),
    }
