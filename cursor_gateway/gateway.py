"""每个节点一套 OpenAI / Anthropic 兼容推理路由。"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent_run as AR
from . import provision as PV
from .credentials import (
    RELAY_PATH,
    SAND_BOX_RUN_STATE_RUNNING,
    BoxGateway,
    TenantCreds,
    UpstreamError,
)
from .accounts import usage_cost_micros
from .models import (
    effective_mode,
    enabled_modes,
    models_for_mode,
    normalize_mode_config,
    price_for_model,
)
from .paths import ensure_sys_path
from .runtime import NodeRuntime, Registry

ensure_sys_path()

import probe_runinference as P  # noqa: E402
import sand_server as SS  # noqa: E402

MODE_BOT = "bot"
MODE_ACCOUNT = "account"
MODE_SAND_DIRECT = "sand-direct"
MODES = (MODE_BOT, MODE_ACCOUNT, MODE_SAND_DIRECT)
DIRECT_MODES = (MODE_SAND_DIRECT,)
DEFAULT_MODEL = "claude-opus-5"
SAND_DIRECT_CLIENT_TYPE = "sand"
MAX_RETRIES = SS.MAX_RETRIES

_CTX_MODE: contextvars.ContextVar[str] = contextvars.ContextVar("cgw_mode", default=MODE_ACCOUNT)
_CTX_CREDS: contextvars.ContextVar[Optional[TenantCreds]] = contextvars.ContextVar(
    "cgw_creds", default=None
)
_CTX_RT: contextvars.ContextVar[Optional[NodeRuntime]] = contextvars.ContextVar(
    "cgw_rt", default=None
)

_stream_locks: dict[str, asyncio.Lock] = {}
_stream_locks_guard = asyncio.Lock()
_provision_tasks: dict[str, asyncio.Task] = {}
_provision_last: dict[str, float] = {}
_provision_guard = asyncio.Lock()
_PROVISION_COOLDOWN = 60.0


def _log(msg: str) -> None:
    SS._log(f"gw {msg}")


async def _stream_lock(key: str) -> asyncio.Lock:
    async with _stream_locks_guard:
        lock = _stream_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _stream_locks[key] = lock
        return lock


def _store_of(request: Request):
    store = getattr(request.app.state, "store", None)
    if store is not None:
        return store
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        return registry.shared.store
    return None


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def require_gateway_token(request: Request, *, count: bool = False) -> str:
    token_id = ""
    store = _store_of(request)
    if store is not None and store.has_api_tokens():
        rec = store.get_token(_bearer_token(request))
        if rec is None or not rec.enabled:
            raise UpstreamError(
                401,
                "需要有效的网关令牌。请在 Authorization: Bearer sk-… 中提供「令牌」页发放的密钥。",
                "invalid_gateway_token",
            )
        store.touch_token(rec.id, count=count)
        token_id = rec.id
        request.state.gateway_user_id = rec.user_id or ""
        if rec.user_id:
            user = store.get_user(rec.user_id)
            if user is None or not user.enabled:
                raise UpstreamError(403, "该令牌所属账号已停用", "user_disabled")
            if user.balance <= 0:
                raise UpstreamError(
                    402,
                    "账号余额不足，请充值或兑换卡密后再调用。",
                    "insufficient_balance",
                )
    request.state.gateway_token_id = token_id
    return token_id


def _token_id_of(request: Request) -> str:
    return str(getattr(request.state, "gateway_token_id", "") or "")


def _extract_credential(request: Request, body: Optional[dict] = None) -> tuple[str, str]:
    api_key = ""
    access_token = ""

    def classify(value: str) -> None:
        nonlocal api_key, access_token
        value = (value or "").strip()
        if not value:
            return
        if value.startswith("crsr_"):
            api_key = api_key or value
        elif value.startswith("eyJ") and value.count(".") == 2:
            access_token = access_token or value

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        classify(auth[7:])
    classify(request.headers.get("x-api-key", ""))
    classify(request.headers.get("x-cursor-api-key", ""))
    if body:
        classify(str(body.get("api_key") or ""))
        classify(str(body.get("access_token") or ""))
    return api_key, access_token


async def resolve_creds(rt: NodeRuntime, request: Request, mode: str, body: Optional[dict] = None) -> TenantCreds:
    rec = rt.record
    if mode == MODE_BOT:
        if not rec.api_key:
            raise UpstreamError(
                501,
                f"节点「{rec.name}」未配置 api_key，无法使用 bot 模式。",
                "bot_not_configured",
            )
        return await rt.cm.get(api_key=rec.api_key)
    if mode == MODE_SAND_DIRECT:
        if not rec.api_key and not rec.access_token:
            raise UpstreamError(
                501,
                f"节点「{rec.name}」未配置 api_key / access_token，无法使用 sand-direct。",
                "sand_direct_not_configured",
            )
        return await rt.cm.get(api_key=rec.api_key, access_token=rec.access_token)
    api_key, access_token = _extract_credential(request, body)
    if not api_key and not access_token:
        api_key, access_token = rec.api_key, rec.access_token
    if not api_key and not access_token:
        raise UpstreamError(
            401,
            "account 模式无可用凭据：请在 Authorization 里带 Cursor API key，或在节点配置里填写。",
            "no_credentials",
        )
    return await rt.cm.get(api_key=api_key, access_token=access_token)


async def _read_json(request: Request) -> dict:
    cached_err = getattr(request.state, "json_error", None)
    if isinstance(cached_err, UpstreamError):
        raise cached_err
    cached = getattr(request.state, "json_body", None)
    if isinstance(cached, dict):
        return cached
    try:
        payload = await request.json()
    except Exception as exc:
        err = UpstreamError(400, "request body must be valid JSON", "invalid_json")
        request.state.json_error = err
        raise err from exc
    if not isinstance(payload, dict):
        err = UpstreamError(400, "request body must be a JSON object", "invalid_request")
        request.state.json_error = err
        raise err
    request.state.json_body = payload
    return payload


async def _request_model(request: Request) -> str:
    cached = getattr(request.state, "resolved_model", None)
    if isinstance(cached, str):
        return cached
    try:
        payload = await _read_json(request)
    except UpstreamError:
        request.state.resolved_model = ""
        return ""
    raw = str(payload.get("model") or "").strip()
    model = SS._resolve_model_slug(raw) if raw else ""
    request.state.resolved_model = model
    return model


def _validate_messages(payload: dict) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise UpstreamError(400, "messages must be a non-empty array", "invalid_request")
    if not all(isinstance(m, dict) for m in messages):
        raise UpstreamError(400, "each message must be an object", "invalid_request")


def _box_stream_headers(box: BoxGateway, client_key: str, session_id: str, rt: NodeRuntime) -> dict[str, str]:
    mid, mac = rt.identity()
    headers = {
        "content-type": "application/connect+proto",
        "connect-protocol-version": "1",
        "authorization": f"Bearer {box.gateway_token}",
        "x-cursor-client-type": "sand",
        "x-cursor-client-source": "sand-desktop",
        "x-cursor-client-version": SS.BOX_CLIENT_VERSION,
        "x-sand-box-namespace": "prod",
        "x-cursor-client-layout": "editor",
        "x-ghost-mode": "true",
        "x-cursor-client-device-type": "desktop",
        "x-client-key": client_key,
        "x-session-id": session_id,
        "x-request-id": str(uuid.uuid4()),
        "user-agent": "connect-es/1.6.1",
    }
    if mid:
        headers["x-cursor-checksum"] = P.cursor_checksum(mid, mac)
    if box.network_token:
        headers["x-anyrun-network-token"] = box.network_token
    return headers


class _Retry(Exception):
    pass


async def _kick_provision(rt: NodeRuntime, creds: TenantCreds, mode: str) -> bool:
    shared = rt.shared
    fp = f"{rt.record.id}:{creds.fingerprint or 'default'}"
    async with _provision_guard:
        task = _provision_tasks.get(fp)
        if task is not None and not task.done():
            return False
        if time.monotonic() - _provision_last.get(fp, 0.0) < _PROVISION_COOLDOWN:
            return False
        _provision_last[fp] = time.monotonic()

        async def _run() -> None:
            try:
                result = await PV.provision_box(
                    shared.client,
                    shared.box_manager,
                    creds,
                    wait_seconds=rt.record.provision_bg_wait,
                    force=False,
                    get_state=rt.provision_state,
                    set_state=rt.set_provision_entry,
                    prompt=rt.record.provision_prompt,
                )
                _log(f"auto-provision node={rt.record.slug} mode={mode} -> {result.get('status')}")
            except Exception as exc:  # noqa: BLE001
                _log(f"auto-provision node={rt.record.slug} failed: {exc}")
            finally:
                async with _provision_guard:
                    _provision_tasks.pop(fp, None)

        _provision_tasks[fp] = asyncio.create_task(_run())
    return True


async def _stream_upstream(
    rt: NodeRuntime, creds: TenantCreds, model: str, prep: "SS.Prepared", mode: str = ""
):
    is_account = mode == MODE_ACCOUNT
    is_direct = mode in DIRECT_MODES
    client_key = uuid.uuid4().hex
    parameters = {"effort": prep.effort} if prep.effort else None
    max_tokens = prep.max_tokens
    started = time.perf_counter()
    lock_prefix = "account:" if is_account else ("direct:" if is_direct else "box:")
    lock = await _stream_lock(lock_prefix + (creds.fingerprint or "default"))
    thinking_chars = 0
    _log(
        f"stream node={rt.record.slug} mode={mode or '-'} model={model} "
        f"fp={creds.fingerprint or '-'} effort={prep.effort or '-'}"
    )
    async with lock:
        for attempt in range(MAX_RETRIES + 1):
            conversation_id = str(uuid.uuid4())
            session_id = str(uuid.uuid4())
            emitted = False
            limit_hit = False
            first_at: Optional[float] = None
            if attempt > 0:
                try:
                    creds = await rt.cm.refresh(creds)
                except UpstreamError:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    raise
            if is_account:
                # API key 换出的 token 直连 api2 Stream 会 401（ide / sand 身份都一样），
                # 只有 AgentService/Run 认它。调用方 tools 以 MCP 工具桥接过去。
                mid, mac = rt.identity()
                try:
                    async for ev in AR.stream_account_events(
                        creds.access_token,
                        creds.machine_id or mid,
                        creds.mac_machine_id or mac,
                        model,
                        prep.messages_pb,
                        client_type=rt.record.account_client_type or "ide",
                        agent_host=rt.record.agent_host,
                        client_version=rt.record.account_client_version,
                        workspace=rt.record.account_workspace,
                        msgs=getattr(prep, "msgs", None),
                        tools=getattr(prep, "tools", None),
                    ):
                        if first_at is None:
                            first_at = time.perf_counter()
                            _log(f"TTFB {first_at - started:.2f}s HTTP/2 path=agent")
                        if ev["type"] in ("thinking", "text", "tool_call", "stop"):
                            emitted = True
                        if ev["type"] == "thinking":
                            thinking_chars += len(ev.get("text") or "")
                        if ev["type"] == "stop":
                            limit_hit = True
                        yield ev
                    _log(
                        f"done {time.perf_counter() - started:.2f}s model={model} "
                        f"path=agent thinking={thinking_chars}"
                    )
                    return
                except UpstreamError as exc:
                    if not emitted and attempt < MAX_RETRIES and exc.status in (401, 403, 429):
                        _log(f"account {exc.status}，刷新凭据后重试")
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    raise
            client = None
            try:
                if is_direct:
                    mid, mac = rt.identity()
                    pc = P.Credentials(
                        access_token=creds.access_token,
                        machine_id=creds.machine_id or mid,
                        mac_machine_id=creds.mac_machine_id or mac,
                        email=creds.email,
                        source="gateway",
                    )
                    url = (rt.record.backend or P.BACKEND).rstrip("/") + P.RPC_STREAM
                    headers = P.build_headers(
                        pc, client_key, session_id, SAND_DIRECT_CLIENT_TYPE, ""
                    )
                    client = rt.shared.client_h2
                else:
                    box = await rt.shared.box_manager.get(creds, refresh=attempt > 0)
                    url = SS._join_box_url(box.gateway_url, RELAY_PATH)
                    headers = _box_stream_headers(box, client_key, session_id, rt)
                    client = rt.shared.client
            except UpstreamError:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                raise
            if client is None:
                raise UpstreamError(503, "server not ready", "not_ready")
            body = P.frame(
                P.inference_stream_request_full(
                    model,
                    prep.messages_pb,
                    prep.tools_pb,
                    conversation_id,
                    parameters,
                    max_tokens=max_tokens or None,
                    max_mode=True,
                )
            )
            try:
                async with client.stream("POST", url, content=body, headers=headers) as resp:
                    http_ver = getattr(resp, "http_version", "?")
                    if resp.status_code != 200:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                        _log(f"upstream {resp.status_code} {http_ver} {detail[:120]}")
                        if resp.status_code in (401, 403) and attempt < MAX_RETRIES:
                            await asyncio.sleep(0.4)
                            continue
                        if resp.status_code == 429 and attempt < MAX_RETRIES:
                            await asyncio.sleep(2.0 * (attempt + 1))
                            continue
                        if resp.status_code in P.GATEWAY_CODES and attempt < MAX_RETRIES:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        if not is_direct and resp.status_code in (404, 417):
                            if rt.record.provision_on_missing:
                                triggered = await _kick_provision(rt, creds, mode or MODE_BOT)
                                raise UpstreamError(
                                    503,
                                    "Box relay 未挂载，已在后台自动挂载，请稍后重试。"
                                    if triggered
                                    else "Box relay 未挂载，正在后台自动挂载中，请稍后重试。",
                                    "relay_provisioning",
                                )
                            raise UpstreamError(
                                resp.status_code,
                                "Box relay 未挂载：请对该节点执行 provision。",
                                "relay_not_mounted",
                            )
                        raise UpstreamError(resp.status_code, detail, "upstream_error")
                    async for flag, payload in SS.adeframe(resp.aiter_bytes()):
                        if first_at is None:
                            first_at = time.perf_counter()
                            _log(f"TTFB {first_at - started:.2f}s {http_ver} model={model}")
                        if flag & P.FLAG_END_STREAM:
                            trailer = payload.decode("utf-8", "replace").strip()
                            info = SS._parse_trailer_info(trailer) if trailer else {}
                            if trailer and trailer not in ("{}", ""):
                                msg = info.get("message") or ""
                                code = info.get("code") or ""
                                if msg and limit_hit:
                                    _log(f"trailer after token limit: {code} {msg}")
                                elif msg:
                                    status = SS._trailer_http_status(info)
                                    can_retry = (
                                        attempt < MAX_RETRIES
                                        and not emitted
                                        and info.get("retryable") is True
                                        and status != 400
                                    )
                                    if can_retry:
                                        await asyncio.sleep(1.2 * (attempt + 1))
                                        raise _Retry()
                                    raise UpstreamError(status, msg, code or "upstream_error")
                            _log(
                                f"done {time.perf_counter() - started:.2f}s model={model} "
                                f"thinking={thinking_chars}"
                            )
                            return
                        parsed = P.pb_parse(payload)
                        for ev in SS._events_from_payload(parsed):
                            if ev["type"] in ("thinking", "text", "tool_call", "stop"):
                                emitted = True
                            if ev["type"] == "thinking":
                                thinking_chars += len(ev.get("text") or "")
                            if ev["type"] == "stop":
                                limit_hit = True
                            yield ev
                _log(
                    f"done {time.perf_counter() - started:.2f}s model={model} "
                    f"thinking={thinking_chars}"
                )
                return
            except _Retry:
                continue
            except UpstreamError as exc:
                if not emitted and attempt < MAX_RETRIES and exc.status == 429:
                    await asyncio.sleep(1.8 * (attempt + 1))
                    continue
                raise
            except httpx.HTTPError as exc:
                if emitted:
                    raise UpstreamError(502, f"stream interrupted: {exc}", "upstream_error")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise UpstreamError(504, f"upstream unreachable: {exc}", "upstream_error") from exc
    raise UpstreamError(504, "exhausted retries", "upstream_error")


async def _tenant_stream_events(model: str, prep: "SS.Prepared"):
    creds = _CTX_CREDS.get()
    rt = _CTX_RT.get()
    if creds is None or rt is None:
        raise UpstreamError(401, "missing tenant credentials", "no_credentials")
    async for ev in _stream_upstream(rt, creds, model, prep, mode=_CTX_MODE.get()):
        yield ev


SS._stream_events = _tenant_stream_events


def _think_hint_wanted() -> bool:
    """不再注入 <think> 提示。Box relay 脱敏后也不用正文伪造思考链。"""
    return False


SS._think_hint_wanted = _think_hint_wanted


def _bind_ctx(gen_factory, mode: str, creds: TenantCreds, rt: NodeRuntime):
    async def _wrapped():
        tok_mode = _CTX_MODE.set(mode)
        tok_creds = _CTX_CREDS.set(creds)
        tok_rt = _CTX_RT.set(rt)
        try:
            async for item in gen_factory():
                yield item
        finally:
            _CTX_MODE.reset(tok_mode)
            _CTX_CREDS.reset(tok_creds)
            _CTX_RT.reset(tok_rt)

    return _wrapped()


def _node_headers(rt: NodeRuntime) -> dict[str, str]:
    return {
        "x-cursor-gateway-node": rt.record.slug,
        "x-cursor-gateway-node-id": rt.record.id,
    }


def _json_ok(payload: dict[str, Any], rt: NodeRuntime, status: int = 200):
    return JSONResponse(payload, status_code=status, headers=_node_headers(rt))


def _sse_failure(chunk: str) -> Optional[tuple[int, str]]:
    """流式接口 HTTP 仍是 200，失败写在 SSE 的 error 对象里。"""
    for raw in (chunk or "").split("\n"):
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        err = obj.get("error")
        if not isinstance(err, dict):
            continue
        msg = str(err.get("message") or "").strip()
        if not msg and obj.get("type") != "error":
            continue
        raw_status = err.get("status")
        if isinstance(raw_status, int) and raw_status >= 400:
            return raw_status, msg
        typ = str(err.get("type") or "")
        code = str(err.get("code") or "")
        if typ == "rate_limit_error" or code == "rate_limit_exceeded":
            return 429, msg
        if typ in ("authentication_error",) or code == "invalid_api_key":
            return 401, msg
        if typ in ("permission_error", "permission_denied"):
            return 403, msg
        if typ == "overloaded_error":
            return 529, msg
        if typ == "server_error" or typ == "api_error":
            return 500, msg
        return 400, msg or typ or "upstream error"
    return None


def _empty_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _sse_usage(chunk: str) -> dict[str, int]:
    found = _empty_usage()
    hit = False
    for raw in (chunk or "").split("\n"):
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        cache_read = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        details = usage.get("prompt_tokens_details")
        if cache_read is None and isinstance(details, dict):
            cache_read = details.get("cached_tokens")
        if prompt is not None:
            found["prompt_tokens"] = int(prompt or 0)
            hit = True
        if completion is not None:
            found["completion_tokens"] = int(completion or 0)
            hit = True
        if cache_read is not None:
            found["cache_read_tokens"] = int(cache_read or 0)
            hit = True
        if cache_write is not None:
            found["cache_write_tokens"] = int(cache_write or 0)
            hit = True
    return found if hit else {}


def _merge_usage(dst: dict[str, int], src: dict[str, int]) -> None:
    for key, value in src.items():
        if value:
            dst[key] = int(value)


def _text_tokens(text: str) -> int:
    return SS.estimate_tokens(text or "")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item.get("thinking") or ""))
        return "".join(parts)
    return ""


def _estimate_payload_tokens(payload: dict[str, Any]) -> int:
    """只按调用方请求体估算，不含 Cursor Agent / Box 内部上下文。"""
    total = 0
    system = payload.get("system")
    if isinstance(system, str):
        total += _text_tokens(system)
    elif isinstance(system, list):
        total += _text_tokens(_content_text(system))
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        total += _text_tokens(_content_text(msg.get("content")))
        total += _text_tokens(str(msg.get("reasoning_content") or ""))
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total += _text_tokens(str(fn.get("name") or ""))
                total += _text_tokens(str(fn.get("arguments") or ""))
    for tool in payload.get("tools") or []:
        try:
            total += _text_tokens(json.dumps(tool, ensure_ascii=False))
        except TypeError:
            continue
    return total


def _sse_output_delta(chunk: str) -> str:
    pieces: list[str] = []
    for raw in (chunk or "").split("\n"):
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            pieces.append(str(delta.get("content") or ""))
            pieces.append(str(delta.get("reasoning_content") or ""))
            for tc in delta.get("tool_calls") or []:
                if isinstance(tc, dict):
                    pieces.append(str((tc.get("function") or {}).get("arguments") or ""))
        delta = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
        pieces.append(str(delta.get("text") or ""))
        pieces.append(str(delta.get("thinking") or ""))
        pieces.append(str(delta.get("partial_json") or ""))
    return "".join(pieces)


def _billable_usage(caller_in: int, caller_out: int, upstream: dict[str, int]) -> dict[str, int]:
    """上游 usage 常含 Agent 系统提示。只有和请求内容同量级时才采用。"""
    up_in = int(upstream.get("prompt_tokens") or 0)
    up_out = int(upstream.get("completion_tokens") or 0)
    in_cap = max(int(caller_in) * 4, int(caller_in) + 256, 64)
    out_cap = max(int(caller_out) * 4, int(caller_out) + 256, 64)
    prompt = up_in if 0 < up_in <= in_cap else int(caller_in or 0)
    completion = up_out if 0 < up_out <= out_cap else int(caller_out or up_out or 0)
    billed = _empty_usage()
    billed["prompt_tokens"] = prompt
    billed["completion_tokens"] = completion
    billed["cache_read_tokens"] = 0
    billed["cache_write_tokens"] = 0
    return billed


def _replace_sse_usage(chunk: str, billed: dict[str, int]) -> str:
    prompt = int(billed.get("prompt_tokens") or 0)
    completion = int(billed.get("completion_tokens") or 0)
    total = prompt + completion

    def patch(obj: dict[str, Any]) -> bool:
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            return False
        if "prompt_tokens" in usage or "completion_tokens" in usage:
            usage["prompt_tokens"] = prompt
            usage["completion_tokens"] = completion
            usage["total_tokens"] = total
        if "input_tokens" in usage or "output_tokens" in usage:
            usage["input_tokens"] = prompt
            usage["output_tokens"] = completion
        return True

    lines = []
    changed = False
    for raw in (chunk or "").split("\n"):
        stripped = raw.strip()
        if stripped.startswith("data:"):
            data = stripped[5:].strip()
            if data and data != "[DONE]":
                try:
                    obj = json.loads(data)
                except Exception:
                    lines.append(raw)
                    continue
                if isinstance(obj, dict) and patch(obj):
                    prefix = raw[: raw.find("data:")]
                    lines.append(prefix + "data: " + json.dumps(obj, ensure_ascii=False))
                    changed = True
                    continue
        lines.append(raw)
    return "\n".join(lines) if changed else chunk


def _log_req(
    rt: NodeRuntime,
    mode: str,
    model: str,
    protocol: str,
    stream: bool,
    status: int,
    started: float,
    error: str = "",
    *,
    token_id: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
):
    try:
        rt.shared.store.log_request(
            rt.record.id,
            mode=mode,
            model=model,
            protocol=protocol,
            stream=stream,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=error,
            token_id=token_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        rt.shared.store.touch_node(rt.record.id)
        if 200 <= int(status or 0) < 400 and token_id:
            rec = rt.shared.store.get_token(token_id)
            if rec and rec.user_id:
                settings = rt.shared.store.get_app_settings()
                input_price, output_price = price_for_model(settings, model)
                cost = usage_cost_micros(
                    prompt_tokens,
                    completion_tokens,
                    input_price,
                    output_price,
                )
                if cost:
                    rt.shared.store.charge_user(
                        rec.user_id,
                        cost,
                        note=f"{model} {prompt_tokens}+{completion_tokens}",
                    )
    except Exception:
        pass


async def handle_chat(request: Request, mode: str, rt: NodeRuntime):
    require_gateway_token(request, count=True)
    rt.inflight += 1
    try:
        return await _handle_chat(request, mode, rt)
    finally:
        rt.inflight = max(0, rt.inflight - 1)


def _assert_mode_ready(rt: NodeRuntime, mode: str, model: str = "") -> str:
    rec = rt.record
    try:
        return effective_mode(rec.default_mode, rec.mode_config, mode, model)
    except ValueError as exc:
        text = str(exc)
        code = "model_not_allowed" if "模型" in text else "mode_disabled"
        status = 400 if code == "model_not_allowed" else 403
        raise UpstreamError(status, f"节点「{rec.name}」{text}", code) from exc


def _default_model_for(rt: NodeRuntime, mode: str) -> str:
    allowed = models_for_mode(rt.record.mode_config, mode)
    return allowed[0] if allowed else DEFAULT_MODEL


def _assert_model_allowed(rt: NodeRuntime, mode: str, model: str) -> None:
    allowed = models_for_mode(rt.record.mode_config, mode)
    if model in allowed:
        return
    hint = "、".join(allowed) if allowed else "（无）"
    raise UpstreamError(
        400,
        f"节点「{rt.record.name}」的 {mode} 模式未开放模型 {model}。可用：{hint}",
        "model_not_allowed",
    )


def _collect_models(records: list, mode: str = "") -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for rec in records:
        cfg = normalize_mode_config(getattr(rec, "mode_config", None))
        modes = [mode] if mode else enabled_modes(cfg)
        for item in modes:
            if mode and not cfg.get(item, {}).get("enabled"):
                continue
            for name in cfg.get(item, {}).get("models") or []:
                if name in seen:
                    continue
                seen.add(name)
                out.append(name)
    return out


async def _handle_chat(request: Request, mode: str, rt: NodeRuntime):
    started = time.perf_counter()
    model = DEFAULT_MODEL
    stream = False
    token_id = _token_id_of(request)
    try:
        peeked = await _request_model(request)
        mode = _assert_mode_ready(rt, mode, peeked)
        creds = await resolve_creds(rt, request, mode)
        payload = await _read_json(request)
        _validate_messages(payload)
    except UpstreamError as exc:
        _log_req(rt, mode, model, "openai", stream, exc.status, started, exc.message, token_id=token_id)
        return SS._openai_error_response(exc)

    model = SS._resolve_model_slug(payload.get("model") or _default_model_for(rt, mode))
    payload["model"] = model
    try:
        mode = _assert_mode_ready(rt, mode, model)
        _assert_model_allowed(rt, mode, model)
    except UpstreamError as exc:
        _log_req(rt, mode, model, "openai", stream, exc.status, started, exc.message, token_id=token_id)
        return SS._openai_error_response(exc)
    stream = bool(payload.get("stream"))
    cid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    _log(f"POST chat node={rt.record.slug} mode={mode} model={model} stream={stream}")

    caller_in = _estimate_payload_tokens(payload)
    if stream:
        async def _gen():
            status = 200
            err = ""
            upstream = _empty_usage()
            billed = _empty_usage()
            out_parts: list[str] = []
            try:
                async for chunk in _bind_ctx(
                    lambda: SS._openai_stream(payload, model, cid, created), mode, creds, rt
                ):
                    fail = _sse_failure(chunk)
                    if fail:
                        status, err = fail
                    out_parts.append(_sse_output_delta(chunk))
                    _merge_usage(upstream, _sse_usage(chunk))
                    billed = _billable_usage(
                        caller_in, _text_tokens("".join(out_parts)), upstream
                    )
                    if _sse_usage(chunk):
                        chunk = _replace_sse_usage(chunk, billed)
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status", status if status >= 400 else 500) or 500
                err = str(exc) or err
            finally:
                _log(
                    f"usage billed={billed['prompt_tokens']}+{billed['completion_tokens']} "
                    f"upstream={upstream['prompt_tokens']}+{upstream['completion_tokens']} "
                    f"caller~{caller_in}+{_text_tokens(''.join(out_parts))}"
                )
                _log_req(
                    rt, mode, model, "openai", True, status, started, err,
                    token_id=token_id, **billed,
                )

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={**SS.SSE_HEADERS, **_node_headers(rt)},
        )

    tok_mode = _CTX_MODE.set(mode)
    tok_creds = _CTX_CREDS.set(creds)
    tok_rt = _CTX_RT.set(rt)
    try:
        prep = await SS._prepare(SS._prepare_openai, payload)
        res = await SS.collect_events(model, prep)
    except UpstreamError as exc:
        _log_req(rt, mode, model, "openai", False, exc.status, started, exc.message, token_id=token_id)
        return SS._openai_error_response(exc)
    finally:
        _CTX_MODE.reset(tok_mode)
        _CTX_CREDS.reset(tok_creds)
        _CTX_RT.reset(tok_rt)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": res.text or (None if res.tool_calls else ""),
    }
    if res.thinking:
        message["reasoning_content"] = res.thinking
    if res.tool_calls:
        message["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["args"]},
            }
            for c in res.tool_calls
        ]
    caller_out = _text_tokens(
        res.text + res.thinking + "".join(c["args"] for c in res.tool_calls)
    )
    billed = _billable_usage(
        caller_in or prep.in_est,
        caller_out,
        {
            "prompt_tokens": res.usage["input"],
            "completion_tokens": res.usage["output"],
        },
    )
    in_tokens = billed["prompt_tokens"]
    out_tokens = billed["completion_tokens"]
    _log(
        f"usage billed={in_tokens}+{out_tokens} "
        f"upstream={res.usage['input']}+{res.usage['output']} caller~{caller_in}+{caller_out}"
    )
    _log_req(
        rt, mode, model, "openai", False, 200, started,
        token_id=token_id,
        prompt_tokens=in_tokens,
        completion_tokens=out_tokens,
    )
    return _json_ok(
        {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "logprobs": None,
                    "finish_reason": SS._openai_finish(res.finish, bool(res.tool_calls)),
                }
            ],
            "usage": {
                "prompt_tokens": in_tokens,
                "completion_tokens": out_tokens,
                "total_tokens": in_tokens + out_tokens,
                "prompt_tokens_details": {"cached_tokens": res.usage["cache_read"]},
            },
        },
        rt,
    )


async def handle_messages(request: Request, mode: str, rt: NodeRuntime):
    require_gateway_token(request, count=True)
    rt.inflight += 1
    try:
        return await _handle_messages(request, mode, rt)
    finally:
        rt.inflight = max(0, rt.inflight - 1)


async def _handle_messages(request: Request, mode: str, rt: NodeRuntime):
    started = time.perf_counter()
    model = DEFAULT_MODEL
    stream = False
    token_id = _token_id_of(request)
    try:
        peeked = await _request_model(request)
        mode = _assert_mode_ready(rt, mode, peeked)
        creds = await resolve_creds(rt, request, mode)
        payload = await _read_json(request)
        _validate_messages(payload)
    except UpstreamError as exc:
        _log_req(rt, mode, model, "anthropic", stream, exc.status, started, exc.message, token_id=token_id)
        return SS._anthropic_error_response(exc)

    model = SS._resolve_model_slug(payload.get("model") or _default_model_for(rt, mode))
    payload["model"] = model
    try:
        mode = _assert_mode_ready(rt, mode, model)
        _assert_model_allowed(rt, mode, model)
    except UpstreamError as exc:
        _log_req(rt, mode, model, "anthropic", stream, exc.status, started, exc.message, token_id=token_id)
        return SS._anthropic_error_response(exc)
    stream = bool(payload.get("stream"))
    mid = "msg_" + uuid.uuid4().hex
    _log(f"POST messages node={rt.record.slug} mode={mode} model={model} stream={stream}")
    caller_in = _estimate_payload_tokens(payload)

    if stream:
        async def _gen():
            status = 200
            err = ""
            upstream = _empty_usage()
            billed = _empty_usage()
            out_parts: list[str] = []
            try:
                async for chunk in _bind_ctx(
                    lambda: SS._anthropic_stream(payload, model, mid), mode, creds, rt
                ):
                    fail = _sse_failure(chunk)
                    if fail:
                        status, err = fail
                    out_parts.append(_sse_output_delta(chunk))
                    _merge_usage(upstream, _sse_usage(chunk))
                    billed = _billable_usage(
                        caller_in, _text_tokens("".join(out_parts)), upstream
                    )
                    if _sse_usage(chunk):
                        chunk = _replace_sse_usage(chunk, billed)
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status", status if status >= 400 else 500) or 500
                err = str(exc) or err
            finally:
                _log(
                    f"usage billed={billed['prompt_tokens']}+{billed['completion_tokens']} "
                    f"upstream={upstream['prompt_tokens']}+{upstream['completion_tokens']} "
                    f"caller~{caller_in}+{_text_tokens(''.join(out_parts))}"
                )
                _log_req(
                    rt, mode, model, "anthropic", True, status, started, err,
                    token_id=token_id, **billed,
                )

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={**SS.SSE_HEADERS, **_node_headers(rt)},
        )

    tok_mode = _CTX_MODE.set(mode)
    tok_creds = _CTX_CREDS.set(creds)
    tok_rt = _CTX_RT.set(rt)
    try:
        prep = await SS._prepare(SS._prepare_anthropic, payload)
        res = await SS.collect_events(model, prep)
    except UpstreamError as exc:
        _log_req(rt, mode, model, "anthropic", False, exc.status, started, exc.message, token_id=token_id)
        return SS._anthropic_error_response(exc)
    finally:
        _CTX_MODE.reset(tok_mode)
        _CTX_CREDS.reset(tok_creds)
        _CTX_RT.reset(tok_rt)

    content: list[dict[str, Any]] = []
    if res.thinking and prep.show_thinking:
        content.append({"type": "thinking", "thinking": res.thinking, "signature": ""})
    if res.text:
        content.append({"type": "text", "text": res.text})
    for c in res.tool_calls:
        content.append(
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": SS._args_to_obj(c["args"])}
        )
    caller_out = _text_tokens(
        res.text + res.thinking + "".join(c["args"] for c in res.tool_calls)
    )
    billed = _billable_usage(
        caller_in or prep.in_est,
        caller_out,
        {
            "prompt_tokens": res.usage["input"],
            "completion_tokens": res.usage["output"],
        },
    )
    in_tokens = billed["prompt_tokens"]
    out_tokens = billed["completion_tokens"]
    _log(
        f"usage billed={in_tokens}+{out_tokens} "
        f"upstream={res.usage['input']}+{res.usage['output']} caller~{caller_in}+{caller_out}"
    )
    _log_req(
        rt, mode, model, "anthropic", False, 200, started,
        token_id=token_id,
        prompt_tokens=in_tokens,
        completion_tokens=out_tokens,
    )
    return _json_ok(
        {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": SS._anthropic_stop_reason(res.finish, bool(res.tool_calls)),
            "stop_sequence": res.stop_sequence,
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_read_input_tokens": res.usage["cache_read"],
                "cache_creation_input_tokens": res.usage["cache_write"],
            },
        },
        rt,
    )


def _models_body(models: Optional[list[str]] = None) -> dict:
    now = int(time.time())
    items = models if models is not None else list(SS.DEFAULT_MODELS)
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "cursor-gateway"}
            for m in items
        ],
    }


def _gateway_view(box: BoxGateway) -> dict[str, Any]:
    relay_url = (
        SS._join_box_url(box.gateway_url, RELAY_PATH)
        if box.gateway_url.startswith("https://")
        else ""
    )
    return {
        "gateway_url": box.gateway_url,
        "gateway_token": box.gateway_token,
        "network_token": box.network_token,
        "sand_token": box.network_token,
        "run_state": box.run_state,
        "run_state_running": box.run_state == SAND_BOX_RUN_STATE_RUNNING,
        "relay_path": RELAY_PATH,
        "relay_url": relay_url,
        "minted_at_ms": box.minted_at_ms,
    }


async def handle_provision(request: Request, mode: str, rt: NodeRuntime):
    require_gateway_token(request)
    body: dict = {}
    try:
        creds = await resolve_creds(rt, request, mode)
        try:
            body = await _read_json(request)
        except UpstreamError:
            body = {}
    except UpstreamError as exc:
        return SS._openai_error_response(exc)

    wait_seconds = rt.record.provision_wait
    raw_wait = body.get("wait_seconds") if isinstance(body, dict) else None
    if isinstance(raw_wait, (int, float)) and raw_wait >= 0:
        wait_seconds = float(raw_wait)
    force = bool(body.get("refresh")) if isinstance(body, dict) else False
    try:
        result = await PV.provision_box(
            rt.shared.client,
            rt.shared.box_manager,
            creds,
            wait_seconds=wait_seconds,
            force=force,
            get_state=rt.provision_state,
            set_state=rt.set_provision_entry,
            prompt=rt.record.provision_prompt,
        )
    except UpstreamError as exc:
        return SS._openai_error_response(exc)
    result["mode"] = mode
    result["node"] = {"id": rt.record.id, "slug": rt.record.slug, "name": rt.record.name}
    return result


async def handle_tokens(request: Request, rt: NodeRuntime, want_gateway: bool):
    require_gateway_token(request)
    body: dict = {}
    if request.method == "POST":
        try:
            body = await _read_json(request)
        except UpstreamError:
            body = {}
    api_key, access_token = _extract_credential(request, body)
    if not api_key and not access_token:
        api_key, access_token = rt.record.api_key, rt.record.access_token
    try:
        creds = await rt.cm.get(api_key=api_key, access_token=access_token)
    except UpstreamError as exc:
        return SS._openai_error_response(exc)
    result: dict[str, Any] = {
        "account": {
            "sub": creds.sub,
            "email": creds.email,
            "fingerprint": creds.fingerprint,
        },
        "access_token": creds.access_token,
        "refresh_token": creds.refresh_token,
        "access_token_expires_at": creds.expires_at,
        "machine_id": creds.machine_id,
        "node": {"id": rt.record.id, "slug": rt.record.slug, "name": rt.record.name},
    }
    if want_gateway:
        force = bool(body.get("refresh")) if isinstance(body, dict) else False
        try:
            box = await rt.shared.box_manager.get(creds, refresh=force)
            result["gateway"] = _gateway_view(box)
        except UpstreamError as exc:
            result["gateway"] = {"error": exc.message, "code": exc.code, "status": exc.status}
    return result


def node_info(rt: NodeRuntime) -> dict[str, Any]:
    rec = rt.record
    return {
        "service": "cursor-gateway",
        "node": {
            "id": rec.id,
            "name": rec.name,
            "slug": rec.slug,
            "enabled": rec.enabled,
            "default_mode": rec.default_mode,
            "mode_config": normalize_mode_config(rec.mode_config),
            "backend": rec.backend,
            "bot_configured": bool(rec.api_key),
        },
        "endpoints": {
            "pool": ["POST /v1/chat/completions", "POST /v1/messages"],
            "tokens": ["POST /v1/tokens", "GET /v1/tokens", "POST /v1/tokens/account"],
            "provision": ["POST /v1/provision", "POST /bot/v1/provision", "POST /account/v1/provision"],
            "bot": ["POST /bot/v1/chat/completions", "POST /bot/v1/messages"],
            "account": ["POST /account/v1/chat/completions", "POST /account/v1/messages"],
            "sand-direct": ["POST /sand-direct/v1/chat/completions", "POST /sand-direct/v1/messages"],
            "default": ["POST /v1/chat/completions", "POST /v1/messages", "GET /v1/models"],
        },
        "routing": {
            "pool": "未指定节点的 /v1 会在已启用且有凭据的节点间做最少连接负载均衡",
            "sticky": "/n/{slug}/v1 或节点独立端口固定打到该账号",
        },
        "modes": {
            "bot": "节点 api_key → EnsureSandBox → Box relay",
            "account": "调用方 key 或节点凭据 → AgentService/Run（调用方 tools 以 MCP 工具桥接）",
            "sand-direct": "节点账号以 sand 身份直连 api2 Stream",
        },
    }


def build_gateway_router(get_rt, *, with_node_prefix: bool, include_root: bool = True) -> APIRouter:
    router = APIRouter()
    pre = "/n/{node_ref}" if with_node_prefix else ""

    async def _rt(
        request: Request,
        node_ref: Optional[str] = None,
        *,
        balance: bool = False,
        mode: str = "",
    ) -> NodeRuntime:
        registry: Registry = request.app.state.registry
        if with_node_prefix:
            return registry.resolve(node_ref, mode=mode)
        fixed: Optional[NodeRuntime] = getattr(request.app.state, "fixed_runtime", None)
        if fixed is not None:
            if not fixed.record.enabled:
                raise UpstreamError(503, f"节点「{fixed.record.name}」已停用", "node_disabled")
            return fixed
        if balance:
            model = await _request_model(request)
            return registry.resolve(None, mode=mode, model=model)
        return get_rt(node_ref)

    def _mode_or_default(rt: NodeRuntime, mode: str, model: str = "") -> str:
        return _assert_mode_ready(rt, mode if mode in MODES else "", model)

    @router.post(pre + "/bot/v1/chat/completions")
    async def bot_chat(request: Request, node_ref: Optional[str] = None):
        return await handle_chat(request, MODE_BOT, await _rt(request, node_ref, balance=True, mode=MODE_BOT))

    @router.post(pre + "/account/v1/chat/completions")
    async def account_chat(request: Request, node_ref: Optional[str] = None):
        return await handle_chat(request, MODE_ACCOUNT, await _rt(request, node_ref, balance=True, mode=MODE_ACCOUNT))

    @router.post(pre + "/sand-direct/v1/chat/completions")
    async def sand_direct_chat(request: Request, node_ref: Optional[str] = None):
        return await handle_chat(request, MODE_SAND_DIRECT, await _rt(request, node_ref, balance=True, mode=MODE_SAND_DIRECT))

    @router.post(pre + "/v1/chat/completions")
    async def default_chat(request: Request, node_ref: Optional[str] = None):
        rt = await _rt(request, node_ref, balance=True)
        return await handle_chat(request, _mode_or_default(rt, "", await _request_model(request)), rt)

    @router.post(pre + "/bot/v1/messages")
    async def bot_messages(request: Request, node_ref: Optional[str] = None):
        return await handle_messages(request, MODE_BOT, await _rt(request, node_ref, balance=True, mode=MODE_BOT))

    @router.post(pre + "/account/v1/messages")
    async def account_messages(request: Request, node_ref: Optional[str] = None):
        return await handle_messages(request, MODE_ACCOUNT, await _rt(request, node_ref, balance=True, mode=MODE_ACCOUNT))

    @router.post(pre + "/sand-direct/v1/messages")
    async def sand_direct_messages(request: Request, node_ref: Optional[str] = None):
        return await handle_messages(request, MODE_SAND_DIRECT, await _rt(request, node_ref, balance=True, mode=MODE_SAND_DIRECT))

    @router.post(pre + "/v1/messages")
    async def default_messages(request: Request, node_ref: Optional[str] = None):
        rt = await _rt(request, node_ref, balance=True)
        return await handle_messages(request, _mode_or_default(rt, "", await _request_model(request)), rt)

    def _mode_from_path(path: str) -> str:
        if "/bot/v1" in path:
            return MODE_BOT
        if "/account/v1" in path:
            return MODE_ACCOUNT
        if "/sand-direct/v1" in path:
            return MODE_SAND_DIRECT
        return ""

    @router.get(pre + "/v1/models")
    @router.get(pre + "/bot/v1/models")
    @router.get(pre + "/account/v1/models")
    @router.get(pre + "/sand-direct/v1/models")
    async def list_models(request: Request, node_ref: Optional[str] = None):
        require_gateway_token(request)
        mode = _mode_from_path(str(request.url.path))
        registry: Registry = request.app.state.registry
        try:
            if with_node_prefix or getattr(request.app.state, "fixed_runtime", None) is not None:
                rt = await _rt(request, node_ref, mode=mode)
                if mode:
                    _assert_mode_ready(rt, mode)
                    items = models_for_mode(rt.record.mode_config, mode)
                else:
                    items = _collect_models([rt.record], "")
                return _models_body(items)
        except UpstreamError as exc:
            return SS._openai_error_response(exc)
        records = [rt.record for rt in registry.runtimes.values() if registry.eligible(rt, mode)]
        return _models_body(_collect_models(records, mode))

    @router.post(pre + "/v1/provision")
    async def default_provision(request: Request, node_ref: Optional[str] = None):
        rt = await _rt(request, node_ref)
        return await handle_provision(request, _mode_or_default(rt, ""), rt)

    @router.post(pre + "/bot/v1/provision")
    async def bot_provision(request: Request, node_ref: Optional[str] = None):
        return await handle_provision(request, MODE_BOT, await _rt(request, node_ref))

    @router.post(pre + "/account/v1/provision")
    async def account_provision(request: Request, node_ref: Optional[str] = None):
        return await handle_provision(request, MODE_ACCOUNT, await _rt(request, node_ref))

    @router.post(pre + "/v1/tokens")
    @router.get(pre + "/v1/tokens")
    async def tokens(request: Request, node_ref: Optional[str] = None):
        return await handle_tokens(request, await _rt(request, node_ref), True)

    @router.post(pre + "/v1/tokens/account")
    async def tokens_account(request: Request, node_ref: Optional[str] = None):
        return await handle_tokens(request, await _rt(request, node_ref), False)

    @router.get(pre + "/healthz")
    async def healthz(request: Request, node_ref: Optional[str] = None):
        rt = await _rt(request, node_ref)
        return {"ok": True, "node": rt.record.slug, "enabled": rt.record.enabled}

    if include_root:
        if with_node_prefix:

            @router.get(pre)
            @router.get(pre + "/")
            async def node_root(request: Request, node_ref: Optional[str] = None):
                return node_info(await _rt(request, node_ref))

        else:

            @router.get("/")
            async def node_root(request: Request, node_ref: Optional[str] = None):
                try:
                    return node_info(await _rt(request, node_ref))
                except UpstreamError as exc:
                    return {"service": "cursor-gateway", "ok": False, "error": exc.message}

    return router


def create_node_app(rt: NodeRuntime) -> FastAPI:
    app = FastAPI(title=f"cursor-gateway:{rt.record.slug}", docs_url=None, redoc_url=None)
    app.state.fixed_runtime = rt
    app.state.registry = None
    app.state.store = rt.shared.store

    def _get_rt(_ref=None):
        return rt

    app.include_router(
        build_gateway_router(_get_rt, with_node_prefix=False, include_root=True)
    )
    return app


async def warmup_node(rt: NodeRuntime) -> None:
    rec = rt.record
    if not rec.provision_on_start or not rec.api_key:
        return
    try:
        creds = await rt.cm.get(api_key=rec.api_key)
        if await _kick_provision(rt, creds, "bot"):
            _log(f"warmup node={rec.slug}")
    except Exception as exc:  # noqa: BLE001
        _log(f"warmup node={rec.slug} failed: {exc}")
