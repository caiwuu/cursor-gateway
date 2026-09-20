"""用户自助台 API：注册、登录、卡密兑换、自己的令牌。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from . import captcha as CP
from .accounts import micros_to_yuan, usage_cost_micros, verify_password
from .models import enabled_modes, models_for_mode, official_price_for_model, price_for_model
from .store import Store

router = APIRouter(prefix="/api/user")


def _store(request: Request) -> Store:
    return request.app.state.store


def _session_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-user-session") or "").strip()


def _current_user(request: Request, *, required: bool = True):
    user = _store(request).user_by_session(_session_token(request))
    if user is None and required:
        raise HTTPException(401, "请先登录")
    if user is not None and not user.enabled:
        raise HTTPException(403, "账号已停用")
    return user


def _available_models(store: Store, settings) -> list[str]:
    catalog = list(settings.model_list or [])
    allowed: set[str] = set()
    nodes = [n for n in store.list_nodes() if n.enabled and (n.api_key or n.access_token)]
    configs = [n.mode_config for n in nodes] if nodes else [settings.mode_defaults]
    for cfg in configs:
        for mode in enabled_modes(cfg):
            allowed.update(models_for_mode(cfg, mode))
    return [name for name in catalog if name in allowed]


def _shop_view(store: Store, settings) -> dict[str, Any]:
    models = _available_models(store, settings)
    priced = []
    for name in models:
        inp, outp = price_for_model(settings, name)
        official_in, official_out = official_price_for_model(settings, name)
        priced.append(
            {
                "id": name,
                "input_price_per_1m": inp,
                "output_price_per_1m": outp,
                "official_input_price_per_1m": official_in,
                "official_output_price_per_1m": official_out,
            }
        )
    return {
        "allow_register": bool(settings.allow_register),
        "need_setup": not store.has_admin(),
        "shop_url": settings.shop_url,
        "input_price_per_1m": settings.input_price_per_1m,
        "output_price_per_1m": settings.output_price_per_1m,
        "currency": "CNY",
        "model_list": models,
        "models": priced,
    }


def _usage_cost(settings, model: str, prompt: int, completion: int) -> int:
    inp, outp = price_for_model(settings, model)
    return usage_cost_micros(prompt, completion, inp, outp)


def _user_log(item, settings) -> dict[str, Any]:
    cost = _usage_cost(settings, item.model, item.prompt_tokens, item.completion_tokens)
    return {
        "id": item.id,
        "mode": item.mode,
        "model": item.model,
        "stream": bool(item.stream),
        "status": item.status,
        "latency_ms": item.latency_ms,
        "error": item.error,
        "created_at": item.created_at,
        "token_id": item.token_id,
        "token_name": item.token_name,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "total_tokens": item.total_tokens,
        "cost": cost,
        "cost_yuan": micros_to_yuan(cost),
    }


def _user_payload(store: Store, user, session: str = "") -> dict[str, Any]:
    settings = store.get_app_settings()
    data = user.public_dict()
    if session:
        data["session"] = session
    data["shop"] = _shop_view(store, settings)
    return data


def _require_captcha(body: dict[str, Any]) -> None:
    if not CP.consume(str(body.get("captcha_id") or ""), str(body.get("captcha") or "")):
        raise HTTPException(400, "验证码不正确或已过期，请刷新后再试")


@router.get("/captcha")
def captcha() -> dict[str, Any]:
    return CP.issue()


@router.get("/shop")
def shop(request: Request) -> dict[str, Any]:
    store = _store(request)
    return _shop_view(store, store.get_app_settings())


@router.post("/setup")
def setup(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    store = _store(request)
    if store.has_admin():
        raise HTTPException(400, "管理员已存在，请直接登录")
    try:
        user = store.create_user(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
            role="admin",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _user_payload(store, user, store.create_session(user.id))


@router.post("/register")
def register(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    store = _store(request)
    settings = store.get_app_settings()
    if not store.has_admin():
        raise HTTPException(400, "请先创建管理员账号")
    if not settings.allow_register:
        raise HTTPException(403, "当前未开放注册，请联系站长发卡密开户")
    _require_captcha(body)
    try:
        user = store.create_user(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
            role="user",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _user_payload(store, user, store.create_session(user.id))


@router.post("/login")
def login(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    store = _store(request)
    _require_captcha(body)
    try:
        user = store.authenticate(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _user_payload(store, user, store.create_session(user.id))


@router.post("/logout")
def logout(request: Request) -> dict[str, Any]:
    _store(request).delete_session(_session_token(request))
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    return _user_payload(_store(request), user)


@router.put("/password")
def change_password(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = _current_user(request)
    old = str(body.get("old_password") or "")
    new = str(body.get("password") or body.get("new_password") or "")
    if not verify_password(old, user.password_hash):
        raise HTTPException(400, "旧密码不对")
    try:
        updated = _store(request).update_user(user.id, {"password": new})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "用户不存在")
    return {"ok": True}


@router.post("/redeem")
def redeem(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = _current_user(request)
    try:
        updated = _store(request).redeem_card(user.id, str(body.get("code") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return updated.public_dict()


@router.get("/tokens")
def tokens(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    store = _store(request)
    usage = store.usage_by_token()
    items = []
    for rec in store.list_tokens(user.id):
        data = rec.public_dict()
        data.update(usage.get(rec.id, {}))
        items.append(data)
    return {"tokens": items}


@router.post("/tokens")
def create_token(request: Request, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    user = _current_user(request)
    name = str((body or {}).get("name") or "").strip() or "我的令牌"
    rec = _store(request).create_token(name, user_id=user.id)
    return rec.public_dict()


@router.put("/tokens/{token_id}")
def update_token(request: Request, token_id: str, body: dict[str, Any]) -> dict[str, Any]:
    user = _current_user(request)
    rec = _store(request).get_token(token_id)
    if rec is None or rec.user_id != user.id:
        raise HTTPException(404, "令牌不存在")
    updated = _store(request).update_token(token_id, body)
    if updated is None:
        raise HTTPException(404, "令牌不存在")
    return updated.public_dict()


@router.delete("/tokens/{token_id}")
def delete_token(request: Request, token_id: str) -> dict[str, Any]:
    user = _current_user(request)
    rec = _store(request).get_token(token_id)
    if rec is None or rec.user_id != user.id:
        raise HTTPException(404, "令牌不存在")
    _store(request).delete_token(token_id)
    return {"ok": True, "id": token_id}


@router.get("/ledger")
def ledger(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    user = _current_user(request)
    items = [x.public_dict() for x in _store(request).list_ledger(user.id, limit=limit)]
    return {"ledger": items}


@router.get("/logs")
def logs(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    user = _current_user(request)
    store = _store(request)
    items, total = store.list_logs(user_id=user.id, limit=limit, offset=offset)
    settings = store.get_app_settings()
    return {"logs": [_user_log(x, settings) for x in items], "total": total}


@router.get("/usage")
def usage(request: Request, days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
    user = _current_user(request)
    settings = _store(request).get_app_settings()
    summary = _store(request).usage_summary(days=days, user_id=user.id)
    cost = 0
    for bucket in summary.get("by_model") or []:
        cost += _usage_cost(
            settings,
            str(bucket.get("name") or bucket.get("id") or ""),
            int(bucket.get("prompt_tokens") or 0),
            int(bucket.get("completion_tokens") or 0),
        )
    summary["estimated_cost_yuan"] = micros_to_yuan(cost)
    return summary
