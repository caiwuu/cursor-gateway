#!/usr/bin/env python3
"""OpenAI / Anthropic 兼容的本地 API 服务，默认走 Grok Bot Box Relay。

请求编码仍复用 probe_runinference.py 的 Connect/protobuf 分帧，但不再直连
api2.cursor.sh：InferenceService.Stream 打到 installer 写入的
grok-box-relay.json（Box 网关 /sand-stream-relay/...），由云端 Box 换 token
后转发真后端。

  POST /v1/chat/completions     OpenAI Chat Completions（stream / 非 stream / tools / vision）
  POST /v1/messages             Anthropic Messages（stream / 非 stream / tool_use / vision）
  POST /v1/images/generations   出图（直连 api2 AiService.RunGenerateImage，不走 box-relay）
  POST /v1/images/edits         参考图改图（同一 RPC 的 reference_images）
  GET  /v1/models               模型列表

启动：
  .venv-probe/bin/python sand_server.py
  .venv-probe/bin/uvicorn sand_server:app --host 127.0.0.1 --port 8787

凭据：先跑 `python cursor-sand-toolkit.py install`（或保持 Grok Bot 已登录后
让本服务自动刷新 grok-box-relay.json）。可选 SAND_BACKEND=grokbot|cursor
回退到旧路径。
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import struct
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import probe_runinference as P
import grokbot_client as GB
import generate_image as GI

SERVER_API_KEY = os.environ.get("SAND_SERVER_API_KEY", "")
TTFB_TIMEOUT = float(os.environ.get("SAND_TTFB_TIMEOUT", "0") or 0)
MAX_RETRIES = int(os.environ.get("SAND_MAX_RETRIES", "2") or 2)
# 小于该值的 max_tokens 不往上游传：Riot 的连接测试带 max_tokens=16，思考模型会先把
# 额度烧完再报 exceeded。设为 0 则全部透传。
MIN_MAX_TOKENS = int(os.environ.get("SAND_MIN_MAX_TOKENS", "64") or 0)
IMAGE_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_EFFORT = (os.environ.get("SAND_DEFAULT_EFFORT") or "high").strip().lower()
_THINKING_MODEL_MARKERS = (
    "opus",
    "grok",
    "gpt-5",
    "gpt5",
    "sonnet",
    "fable",
    "composer",
    "gemini",
)
# 模型 id 已经带档位（如 gpt-5.6-luna-high）时，再传 effort 会被 Box 当成另一个不存在的模型。
_MODEL_EFFORT_SUFFIXES = (
    "-none-fast",
    "-low-fast",
    "-medium-fast",
    "-high-fast",
    "-xhigh-fast",
    "-max-fast",
    "-none",
    "-low",
    "-medium",
    "-high",
    "-xhigh",
    "-max",
    "-fast",
)
_PROVIDER_RETRY_HINTS = (
    "trouble connecting to the model provider",
    "rate limited by model provider",
    "overloaded",
    "服务方限流",
    "稍等一会儿再发",
)
# 默认 box-relay（与 cursor-sand-toolkit 同一条链路）。
# SAND_BACKEND=grokbot 或 SAND_BACKEND_GROKBOT=1：旧 GrokBotService 云 agent。
# SAND_BACKEND=cursor：旧 api2.cursor.sh（官方已关停，仅调试）。
_BACKEND_ENV = (os.environ.get("SAND_BACKEND") or "").strip().lower()
if _BACKEND_ENV in {"box-relay", "box", "relay"}:
    BACKEND_MODE = "box-relay"
elif _BACKEND_ENV in {"grokbot", "grok"} or os.environ.get("SAND_BACKEND_GROKBOT", "0") == "1":
    BACKEND_MODE = "grokbot"
elif _BACKEND_ENV in {"cursor", "api2"}:
    BACKEND_MODE = "cursor"
else:
    BACKEND_MODE = "box-relay"
USE_GROKBOT = BACKEND_MODE == "grokbot"
BOX_RELAY_PATH = "/sand-stream-relay/aiserver.v1.InferenceService/Stream"
BOX_CLIENT_VERSION = "0.44.0"

# Box 网关是 Node HTTP/1.1；直连 api2 才需要 h2。复用一条连接，避免每次 TCP+TLS。
# 上游按账号串行：并发两条 sand 流会互相饿死，甚至把别人的正文读到自己的流里。
_http: Optional[httpx.AsyncClient] = None
_upstream_lock = asyncio.Lock()
_creds: Optional[P.Credentials] = None
_creds_at = 0.0
_relay: Optional["BoxRelay"] = None
_relay_at = 0.0
_model_slugs: Optional[dict[str, str]] = None
_model_slugs_at = 0.0
_DEFAULT_SYSTEM = "You are a helpful assistant."

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _log(msg: str) -> None:
    print(f"[sand {time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class BoxRelay:
    url: str
    token: str
    extra_headers: dict[str, str]
    fingerprint: str = ""


def _relay_config_path() -> Path:
    override = os.environ.get("SAND_GROK_BOX_RELAY_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "SandClientModeStream" / "sand-client-cli" / "grok-box-relay.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "SandClientModeStream"
            / "sand-client-cli"
            / "grok-box-relay.json"
        )
    return Path.home() / ".config" / "SandClientModeStream" / "sand-client-cli" / "grok-box-relay.json"


def _resolve_model_slug(model: str) -> str:
    """把 IDE 侧别名解析成后端目录里的真实 model id。

    IDE 用 task-models.json 的 slug 表把 `gpt-5.6`→`gpt-5.6-sol`、`opus`→`claude-opus-5`
    等别名解析后再发；本服务原样透传时，别名（如 gpt-5.6）在后端不存在，会回
    ERROR_BAD_MODEL_NAME「isn't available」。这里加载同一张表做解析，表缺失或无该
    别名时原样返回，保证新模型 id 仍可直通。
    """
    global _model_slugs, _model_slugs_at
    if not model:
        return model
    now = time.time()
    if _model_slugs is None or now - _model_slugs_at > 60:
        path = _relay_config_path().with_name("task-models.json")
        slugs: dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("slugs") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                slugs = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            slugs = {}
        _model_slugs = slugs
        _model_slugs_at = now
    return _model_slugs.get(model, model)


def _join_box_url(base_url: str, path: str) -> str:
    base = urlsplit(base_url)
    if (
        base.scheme != "https"
        or not base.netloc
        or base.username is not None
        or base.password is not None
    ):
        raise UpstreamError(503, "Grok Bot Box gateway 地址无效", "relay_config_invalid")
    joined = base.path.rstrip("/") + "/" + path.lstrip("/")
    return urlunsplit((base.scheme, base.netloc, joined, "", ""))


def _relay_from_mapping(value: Any) -> BoxRelay:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("baseUrl"), str)
        or not str(value.get("baseUrl")).startswith("https://")
        or not isinstance(value.get("token"), str)
        or not value.get("token")
    ):
        raise UpstreamError(503, "本地 Grok Bot Box relay 配置缺少必要字段", "relay_config_invalid")
    extra: dict[str, str] = {}
    raw_headers = value.get("headers") or {}
    if isinstance(raw_headers, dict):
        blocked = {"authorization", "host", "content-length", "transfer-encoding"}
        for name, header_value in raw_headers.items():
            if (
                isinstance(name, str)
                and isinstance(header_value, str)
                and header_value
                and name.casefold() not in blocked
            ):
                extra[name] = header_value
    relay_path = value.get("relayPath")
    if not isinstance(relay_path, str) or not relay_path:
        relay_path = BOX_RELAY_PATH
    fingerprint = value.get("accountFingerprint")
    return BoxRelay(
        url=_join_box_url(str(value["baseUrl"]), relay_path),
        token=str(value["token"]),
        extra_headers=extra,
        fingerprint=fingerprint if isinstance(fingerprint, str) else "",
    )


def _refresh_relay_via_toolkit() -> dict[str, Any]:
    import importlib.util

    path = Path(__file__).resolve().parent / "cursor-sand-toolkit.py"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    spec = importlib.util.spec_from_file_location("sand_toolkit_relay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 cursor-sand-toolkit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sand_toolkit_relay"] = module
    spec.loader.exec_module(module)
    return dict(module._load_grok_relay_config(refresh=True))


def _load_box_relay(refresh: bool = False) -> BoxRelay:
    global _relay, _relay_at
    now = time.time()
    if not refresh and _relay is not None and now - _relay_at < 60:
        return _relay
    value: Optional[dict[str, Any]] = None
    if refresh:
        try:
            value = _refresh_relay_via_toolkit()
        except Exception as exc:
            _log(f"relay refresh via toolkit failed: {exc}")
    path = _relay_config_path()
    if value is None:
        if not path.is_file():
            if not refresh:
                return _load_box_relay(True)
            raise UpstreamError(
                503,
                f"未找到 {path}，请先运行 python cursor-sand-toolkit.py install，"
                "并保持 Grok Bot 已登录",
                "relay_config_missing",
            )
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise UpstreamError(503, f"无法读取 Box relay 配置：{exc}", "relay_config_invalid") from exc
        if not isinstance(loaded, dict):
            raise UpstreamError(503, "Box relay 配置格式无效", "relay_config_invalid")
        value = loaded
    _relay = _relay_from_mapping(value)
    _relay_at = now
    return _relay


def _box_stream_headers(relay: BoxRelay, client_key: str, session_id: str) -> dict[str, str]:
    headers = {
        "content-type": "application/connect+proto",
        "connect-protocol-version": "1",
        "authorization": f"Bearer {relay.token}",
        "x-cursor-client-type": "sand",
        "x-cursor-client-source": "sand-desktop",
        "x-cursor-client-version": BOX_CLIENT_VERSION,
        "x-sand-box-namespace": "prod",
        "x-cursor-client-layout": "editor",
        "x-ghost-mode": "true",
        "x-cursor-client-device-type": "desktop",
        "x-client-key": client_key,
        "x-session-id": session_id,
        "x-request-id": str(uuid.uuid4()),
        "user-agent": "connect-es/1.6.1",
    }
    try:
        machine_id, mac_machine_id = P._inherit_cursor_identity()
    except Exception:
        machine_id, mac_machine_id = "", ""
    if machine_id:
        headers["x-cursor-checksum"] = P.cursor_checksum(machine_id, mac_machine_id)
    for name, value in relay.extra_headers.items():
        if name.casefold() not in {"authorization", "content-type", "host"}:
            headers[name] = value
    return headers


def _new_http_client(timeout_read: float = 300.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=BACKEND_MODE == "cursor",
        timeout=httpx.Timeout(timeout_read, connect=15.0, read=timeout_read, write=30.0, pool=10.0),
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=32),
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _http
    _http = _new_http_client()
    _log(f"backend={BACKEND_MODE}")
    if BACKEND_MODE == "box-relay":
        try:
            relay = await asyncio.to_thread(_load_box_relay, True)
            _log(f"box relay ready {relay.url} fp={relay.fingerprint or '-'}")
        except Exception as exc:
            _log(f"box relay not ready: {exc}")
    try:
        yield
    finally:
        await _http.aclose()
        _http = None


app = FastAPI(title="sand-openai-anthropic-bridge", lifespan=_lifespan)

DEFAULT_MODELS = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5-1",
    "grok-4.6",
    "gpt-5.6-luna-high",
    "gpt-5.6-sol",
    "gemini-3.8-flash",
]


# ---------------------------------------------------------------------------
# 估算 token（无官方 tokenizer 时的近似）：中文按字、英文按 ~4 char/token
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


# ---------------------------------------------------------------------------
# 对外错误：4xx 请求错误与 5xx 上游错误共用，最终由各协议的格式化函数落地
# ---------------------------------------------------------------------------


class UpstreamError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# Connect / gRPC 错误码 → HTTP 状态；未列出的一律按 502 上游错误
_CONNECT_CODE_STATUS = {
    "invalid_argument": 400,
    "unauthenticated": 401,
    "permission_denied": 403,
    "not_found": 404,
    "resource_exhausted": 429,
    "unavailable": 503,
    "deadline_exceeded": 504,
}


def _openai_error_body(status: int, message: str, code: str = "") -> dict:
    if status == 401:
        etype, code = "invalid_request_error", code or "invalid_api_key"
    elif status == 429:
        etype, code = "rate_limit_error", code or "rate_limit_exceeded"
    elif 400 <= status < 500:
        etype = "invalid_request_error"
    else:
        etype = "server_error"
    return {
        "error": {
            "message": message,
            "type": etype,
            "param": None,
            "code": code or None,
            "status": status,
        }
    }


_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


def _anthropic_error_body(status: int, message: str) -> dict:
    return {
        "type": "error",
        "error": {
            "type": _ANTHROPIC_ERROR_TYPES.get(status, "api_error"),
            "message": message,
            "status": status,
        },
    }


def _openai_error_response(exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content=_openai_error_body(exc.status, exc.message, exc.code)
    )


def _anthropic_error_response(exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content=_anthropic_error_body(exc.status, exc.message)
    )


# ---------------------------------------------------------------------------
# 规范化的内部消息模型（协议无关）
# ---------------------------------------------------------------------------


@dataclass
class Part:
    """一段内容：text / image / tool_call / tool_result。"""

    kind: str
    text: str = ""
    data: str = ""
    mime: str = ""
    url: str = ""
    id: str = ""
    name: str = ""
    arguments: str = ""
    content: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    is_error: bool = False


class Msg:
    def __init__(self, role: str, parts: list[Part]):
        self.role = role  # system / user / assistant / tool
        self.parts = parts


# ---------------------------------------------------------------------------
# 图片：data URL 直接取 base64；http(s) 留到 _resolve_remote_images 异步拉取
# ---------------------------------------------------------------------------


def _split_data_url(url: str) -> tuple[str, str]:
    """data:image/png;base64,xxxx → (base64, mime)。"""
    head, _, data = url.partition(",")
    mime = head[5:].split(";")[0] or "image/png"
    return data, mime


def _image_part_from_url(url: str) -> Part:
    if url.startswith("data:"):
        data, mime = _split_data_url(url)
        return Part("image", data=data, mime=mime, url="")
    return Part("image", data="", mime="", url=url)


async def _resolve_remote_images(msgs: list[Msg]) -> None:
    for m in msgs:
        for p in m.parts:
            if p.kind == "image" and p.url:
                p.data, p.mime = await _fetch_image(p.url)
                p.url = ""


async def _fetch_image(url: str) -> tuple[str, str]:
    shown = url[:100]
    if not url.lower().startswith(("http://", "https://")):
        raise UpstreamError(400, f"unsupported image url: {shown}", "invalid_image_url")
    if _http is None:
        raise UpstreamError(503, "server not ready")
    try:
        resp = await _http.get(url, follow_redirects=True, timeout=httpx.Timeout(20.0))
    except httpx.HTTPError as exc:
        raise UpstreamError(
            400, f"failed to fetch image {shown}: {exc}", "invalid_image_url"
        ) from exc
    if resp.status_code != 200:
        raise UpstreamError(
            400, f"failed to fetch image {shown}: HTTP {resp.status_code}", "invalid_image_url"
        )
    if len(resp.content) > IMAGE_MAX_BYTES:
        raise UpstreamError(
            400, f"image too large (>{IMAGE_MAX_BYTES} bytes): {shown}", "invalid_image_url"
        )
    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        mime = mimetypes.guess_type(url.split("?", 1)[0])[0] or "image/png"
    return base64.b64encode(resp.content).decode("ascii"), mime


# ---------------------------------------------------------------------------
# OpenAI 请求 → 内部 Msg
# ---------------------------------------------------------------------------


def parse_openai(messages: list[dict]) -> list[Msg]:
    out: list[Msg] = []
    for m in messages:
        role = str(m.get("role", "user")).lower()
        if role == "developer":
            role = "system"
        parts: list[Part] = []

        if role == "tool":
            # OpenAI tool 结果消息：{role:tool, tool_call_id, content}
            parts.append(
                Part(
                    "tool_result",
                    tool_call_id=m.get("tool_call_id", ""),
                    tool_name=m.get("name", ""),
                    content=_flatten_text(m.get("content")),
                    is_error=False,
                )
            )
            out.append(Msg("tool", parts))
            continue

        content = m.get("content")
        if isinstance(content, str):
            if content:
                parts.append(Part("text", text=content))
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype in ("text", "input_text"):
                    parts.append(Part("text", text=item.get("text", "")))
                elif itype == "image_url":
                    image_url = item.get("image_url")
                    url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url or "")
                    if url:
                        parts.append(_image_part_from_url(url))

        # assistant 的 tool_calls
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            parts.append(
                Part(
                    "tool_call",
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments", "") or "{}",
                )
            )
        out.append(Msg(role, parts))
    return out


def parse_openai_tools(payload: dict) -> list[dict]:
    """OpenAI tools → [{name, description, parameters}]。"""
    tools = []
    for t in payload.get("tools") or []:
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            tools.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object"},
                }
            )
    # 兼容旧 functions 字段
    for fn in payload.get("functions") or []:
        tools.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object"},
            }
        )
    return tools


# ---------------------------------------------------------------------------
# Anthropic 请求 → 内部 Msg
# ---------------------------------------------------------------------------


def parse_anthropic(messages: list[dict], system: Any) -> list[Msg]:
    out: list[Msg] = []
    if system:
        out.append(Msg("system", [Part("text", text=_flatten_text(system))]))
    for m in messages:
        role = str(m.get("role", "user")).lower()
        content = m.get("content")
        parts: list[Part] = []
        tool_result_parts: list[Part] = []

        if isinstance(content, str):
            if content:
                parts.append(Part("text", text=content))
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "text":
                    parts.append(Part("text", text=item.get("text", "")))
                elif itype == "image":
                    src = item.get("source") or {}
                    if src.get("type") == "base64":
                        parts.append(
                            Part(
                                "image",
                                data=src.get("data", ""),
                                mime=src.get("media_type") or "image/png",
                                url="",
                            )
                        )
                    elif src.get("type") == "url" and src.get("url"):
                        parts.append(_image_part_from_url(src["url"]))
                elif itype == "tool_use":
                    parts.append(
                        Part(
                            "tool_call",
                            id=item.get("id", ""),
                            name=item.get("name", ""),
                            arguments=json.dumps(
                                item.get("input") or {}, ensure_ascii=False
                            ),
                        )
                    )
                elif itype == "tool_result":
                    tool_result_parts.append(
                        Part(
                            "tool_result",
                            tool_call_id=item.get("tool_use_id", ""),
                            tool_name="",
                            content=_flatten_text(item.get("content")),
                            is_error=bool(item.get("is_error")),
                        )
                    )
                # thinking / redacted_thinking：上游没有回填字段，历史里直接丢弃

        # Anthropic 把 tool_result 放在 user 消息里；单独拆成 tool 角色消息
        if tool_result_parts:
            out.append(Msg("tool", tool_result_parts))
        if parts:
            out.append(Msg(role, parts))
    return out


def parse_anthropic_tools(payload: dict) -> list[dict]:
    tools = []
    for t in payload.get("tools") or []:
        if isinstance(t, dict) and "name" in t:
            tools.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object"},
                }
            )
    return tools


def _flatten_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        buf = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text", "tool_result"):
                    buf.append(item.get("text", "") or _flatten_text(item.get("content")))
                elif "text" in item:
                    buf.append(item["text"])
            elif isinstance(item, str):
                buf.append(item)
        return "".join(buf)
    return str(content)


# ---------------------------------------------------------------------------
# 请求级参数：tool_choice / response_format / stop / max_tokens
# ---------------------------------------------------------------------------


def _apply_tool_choice(tools: list[dict], choice: Any) -> list[dict]:
    """上游没有强制调用开关：none → 不传工具；指定某个工具 → 只传该工具（近似）。"""
    if isinstance(choice, str):
        return [] if choice == "none" else tools
    if isinstance(choice, dict):
        ctype = choice.get("type")
        if ctype == "none":
            return []
        fn = choice.get("function")
        name = (fn.get("name") if isinstance(fn, dict) else None) or choice.get("name")
        if ctype in ("function", "tool") and name:
            picked = [t for t in tools if t["name"] == name]
            return picked or tools
    return tools


def _apply_response_format(msgs: list[Msg], fmt: Any) -> None:
    """上游没有 JSON mode，用 system 指令近似。"""
    if not isinstance(fmt, dict):
        return
    ftype = fmt.get("type")
    hint = "Respond with a single valid JSON object only, without markdown fences or any other text."
    if ftype == "json_schema":
        schema = (fmt.get("json_schema") or {}).get("schema") or {}
        hint += " It must conform to this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
    elif ftype != "json_object":
        return
    for m in msgs:
        if m.role == "system":
            m.parts.append(Part("text", text="\n\n" + hint))
            return
    msgs.insert(0, Msg("system", [Part("text", text=hint)]))


def _stops_of(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [s for s in value if isinstance(s, str) and s]
    return []


def _max_tokens_of(payload: dict) -> int:
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            n = int(value)
            return 0 if n < MIN_MAX_TOKENS else n
    return 0


def _args_to_obj(raw: Any) -> dict:
    """tool 参数 JSON → dict。非对象（数组 / 标量 / 坏 JSON）包进 _raw，避免编码时崩。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return obj if isinstance(obj, dict) else {"_raw": obj}


def _ensure_system(msgs: list[Msg]) -> list[Msg]:
    if any(m.role == "system" and any(p.kind == "text" and p.text for p in m.parts) for m in msgs):
        return msgs
    return [Msg("system", [Part("text", text=_DEFAULT_SYSTEM)]), *msgs]


# ---------------------------------------------------------------------------
# 内部 Msg → Cursor protobuf 消息
# ---------------------------------------------------------------------------

_ROLE_INT = {
    "system": P.ROLE_SYSTEM,
    "user": P.ROLE_USER,
    "assistant": P.ROLE_ASSISTANT,
    "tool": P.ROLE_TOOL,
}


def build_cursor_messages(msgs: list[Msg]) -> tuple[list[bytes], int]:
    """返回 (protobuf 消息列表, 估算输入 token)。"""
    result: list[bytes] = []
    in_tokens = 0
    # tool 结果通常不带工具名（OpenAI 的 role:tool 没有 name），从前面的 tool_call 反查
    call_names = {
        p.id: p.name for m in msgs for p in m.parts if p.kind == "tool_call" and p.id
    }
    i = 0
    while i < len(msgs):
        msg = msgs[i]
        role_int = _ROLE_INT.get(msg.role, P.ROLE_USER)

        if msg.role == "tool":
            # 连续的 tool 结果合并进一条 TOOL 消息（并行工具调用的结果要成组回填）
            trps = []
            while i < len(msgs) and msgs[i].role == "tool":
                for p in msgs[i].parts:
                    if p.kind != "tool_result":
                        continue
                    in_tokens += estimate_tokens(p.content)
                    name = p.tool_name or call_names.get(p.tool_call_id, "")
                    trps.append(P.tool_result_part(p.tool_call_id, name, p.content, p.is_error))
                i += 1
            if trps:
                result.append(P.message_tool_result(trps))
            continue
        i += 1

        tool_calls = [p for p in msg.parts if p.kind == "tool_call"]
        images = [p for p in msg.parts if p.kind == "image" and p.data]
        texts = [p for p in msg.parts if p.kind == "text"]

        if msg.role == "assistant" and tool_calls:
            text = "".join(t.text for t in texts)
            in_tokens += estimate_tokens(text)
            tc_pb = []
            for tc in tool_calls:
                in_tokens += estimate_tokens(tc.arguments)
                tc_pb.append(P.tool_call_pb(tc.id, tc.name, _args_to_obj(tc.arguments)))
            result.append(P.message_assistant_tools(text, tc_pb))
            continue

        if images:
            # 多模态：用 parts 承载 text + image
            content_parts = []
            for p in msg.parts:
                if p.kind == "text" and p.text:
                    in_tokens += estimate_tokens(p.text)
                    content_parts.append(P.content_part_text(p.text))
                elif p.kind == "image" and p.data:
                    in_tokens += 512  # 图片粗略计入
                    content_parts.append(P.content_part_image(p.data, p.mime or "image/png"))
            if content_parts:
                result.append(P.message_parts(role_int, content_parts))
            continue

        # 纯文本
        text = "".join(t.text for t in texts)
        if text:
            in_tokens += estimate_tokens(text)
            result.append(P.core_message(role_int, text))
    return result, in_tokens


# ---------------------------------------------------------------------------
# Cursor 调用：产出规范化事件（text / thinking / tool_call / usage / stop）
# ---------------------------------------------------------------------------


def _parse_trailer_info(trailer: str) -> dict[str, Any]:
    empty = {
        "message": "",
        "code": "",
        "debugError": "",
        "providerStatus": "",
        "retryable": False,
    }
    try:
        obj = json.loads(trailer)
    except json.JSONDecodeError:
        return empty
    err = obj.get("error") if isinstance(obj, dict) else None
    if not isinstance(err, dict):
        return empty
    debug = (err.get("details") or [{}])[0].get("debug", {}) if err.get("details") else {}
    detail = ""
    provider_status = ""
    retryable = False
    debug_error = ""
    if isinstance(debug, dict):
        debug_error = str(debug.get("error") or "")
        d = debug.get("details", {})
        if isinstance(d, dict):
            detail = str(d.get("detail") or d.get("title") or "")
            retryable = bool(d.get("isRetryable"))
            extra = d.get("additionalInfo")
            if isinstance(extra, dict):
                provider_status = str(extra.get("providerStatusCode") or "")
    message = detail or err.get("message") or debug_error
    return {
        "message": message or "upstream error",
        "code": str(err.get("code") or ""),
        "debugError": debug_error,
        "providerStatus": provider_status,
        "retryable": retryable,
    }


def _parse_trailer_error(trailer: str) -> tuple[str, str]:
    """Connect end-stream trailer → (message, code)；没有错误返回 ("", "")。"""
    info = _parse_trailer_info(trailer)
    return info["message"], info["code"]


def _trailer_http_status(info: dict[str, Any]) -> int:
    provider = info.get("providerStatus") or ""
    if provider == "400":
        return 400
    if provider == "429" or info.get("code") == "resource_exhausted" and info.get("retryable"):
        return 429
    return _CONNECT_CODE_STATUS.get(str(info.get("code") or ""), 502)


async def _cached_credentials() -> P.Credentials:
    global _creds, _creds_at
    now = time.time()
    if _creds is not None and now - _creds_at < 60:
        return _creds
    creds = await asyncio.to_thread(P.load_credentials, True)
    _creds = creds
    _creds_at = now
    return creds


async def adeframe(stream) -> AsyncIterator[tuple[int, bytes]]:
    """Connect 分帧的异步版：网络有多少吐多少，不攒齐 64KB。"""
    buf = bytearray()
    async for chunk in stream:
        if not chunk:
            continue
        buf.extend(chunk)
        while len(buf) >= 5:
            flag = buf[0]
            (length,) = struct.unpack(">I", bytes(buf[1:5]))
            if len(buf) < 5 + length:
                break
            payload = bytes(buf[5 : 5 + length])
            del buf[: 5 + length]
            yield flag, payload


def _thinking_text(parsed: dict) -> str:
    raw = P.first(parsed, 9)
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        inner = P.pb_parse(raw)
        t = P.as_text(P.first(inner, 1))
        if t.strip():
            return t
    return ""


def _unwrap_stream_payload(parsed: dict) -> dict:
    """Box relay 有时外套一层，thinking_part 在内层。"""
    if 9 in parsed or 2 in parsed or 8 in parsed:
        return parsed
    inner_raw = P.first(parsed, 1)
    if not isinstance(inner_raw, (bytes, bytearray)):
        return parsed
    inner = P.pb_parse(inner_raw)
    if 9 in inner or 2 in inner or 8 in inner or 3 in inner or 5 in inner:
        return inner
    return parsed


def _events_from_payload(parsed: dict) -> list[dict]:
    parsed = _unwrap_stream_payload(parsed)
    out: list[dict] = []
    t = _thinking_text(parsed)
    if t:
        out.append({"type": "thinking", "text": t})
    if 1 in parsed:  # text_part
        t = P.as_text(P.first(P.pb_parse(P.first(parsed, 1)), 1))
        if t:
            out.append({"type": "text", "text": t})
    if 2 in parsed:  # tool_call_part
        tc = P.pb_parse(P.first(parsed, 2))
        out.append(
            {
                "type": "tool_call",
                "id": P.as_text(P.first(tc, 1)),
                "name": P.as_text(P.first(tc, 2)),
                "args": P.as_text(P.first(tc, 3)),
                "complete": bool(P.first(tc, 4)),
                "index": P.first(tc, 5) or 0,
            }
        )
    if 3 in parsed:  # usage
        u = P.pb_parse(P.first(parsed, 3))
        out.append(
            {
                "type": "usage",
                "input": _int(P.first(u, 1)),
                "output": _int(P.first(u, 2)),
                "cache_read": 0,
                "cache_write": 0,
            }
        )
    if 5 in parsed:  # extended_usage
        u = P.pb_parse(P.first(parsed, 5))
        out.append(
            {
                "type": "usage",
                "input": _int(P.first(u, 1)),
                "output": _int(P.first(u, 2)),
                "cache_read": _int(P.first(u, 3)),
                "cache_write": _int(P.first(u, 4)),
            }
        )
    if 8 in parsed:  # error: message(1) code(2) is_input_token_limit(3) is_output_token_limit(4)
        err = P.pb_parse(P.first(parsed, 8))
        message = P.as_text(P.first(err, 1)) or "inference error"
        code = P.as_text(P.first(err, 2))
        if P.first(err, 4):
            # 输出额度用完不是错误：保留已生成内容，按 length / max_tokens 正常收尾
            out.append({"type": "stop", "reason": "length", "message": message})
            return out
        if P.first(err, 3):
            raise UpstreamError(400, message, "context_length_exceeded")
        if _provider_retryable(message):
            raise UpstreamError(429, message, code or "rate_limit_exceeded")
        raise UpstreamError(_CONNECT_CODE_STATUS.get(code, 502), message, code or "upstream_error")
    return out


async def stream_grokbot(
    model: str,
    prep: Prepared,
) -> AsyncGenerator[dict[str, Any], None]:
    """GrokBotService 云 agent 编排：发一条用户消息，产出 text 事件。

    云 agent 的 AI 回复是一次性完整文本（不是逐字流式），故 MVP 阶段每条
    send-message 文本作为一个 text 事件产出。
    """
    creds = await _cached_credentials()
    token = creds.access_token
    text = prep.user_text
    if not text:
        yield {"type": "stop", "reason": "stop", "message": ""}
        return
    try:
        agent_id = await asyncio.to_thread(GB.get_agent_id_sync, token)
    except Exception as exc:
        raise UpstreamError(502, f"GrokBotService agent lookup failed: {exc}", "upstream_error")
    _log(f"grokbot agent={agent_id} user={text[:60]!r}")
    try:
        async for piece in GB.send_and_collect(agent_id, text, token):
            if piece:
                yield {"type": "text", "text": piece}
    except Exception as exc:
        raise UpstreamError(502, f"GrokBotService turn failed: {exc}", "upstream_error")


async def stream_cursor(
    model: str,
    messages_pb: list[bytes],
    tools_pb: list[bytes],
    effort: str = "",
    max_tokens: int = 0,
    isolated: bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
    """向 Box Relay（或调试用的 api2）发一次推理，产出事件。全程异步。

    消费方提前 break（命中 stop 序列 / 客户端断开）时，内层生成器在 finally 里被
    确定性关闭，立刻释放上游连接与锁，不等 GC。
    """
    if _http is None:
        raise UpstreamError(503, "server not ready")
    client_key = uuid.uuid4().hex
    parameters = {"effort": effort} if effort else None
    started = time.perf_counter()
    _log(
        f"stream model={model} effort={effort or '-'} "
        f"max_tokens={max_tokens or '-'} isolated={isolated}"
    )

    if isolated:
        async with _new_http_client(120.0) as client:
            inner = _stream_cursor_locked(
                client, model, messages_pb, tools_pb, parameters, client_key, max_tokens, started
            )
            try:
                async for ev in inner:
                    yield ev
            finally:
                await inner.aclose()
        return

    async with _upstream_lock:
        inner = _stream_cursor_locked(
            _http, model, messages_pb, tools_pb, parameters, client_key, max_tokens, started
        )
        try:
            async for ev in inner:
                yield ev
        finally:
            await inner.aclose()


async def _stream_target(client_key: str, session_id: str, refresh_relay: bool = False) -> tuple[str, dict[str, str]]:
    if BACKEND_MODE == "cursor":
        creds = await _cached_credentials()
        return f"{P.BACKEND}{P.RPC_STREAM}", P.build_headers(
            creds, client_key, session_id, "sand", ""
        )
    relay = await asyncio.to_thread(_load_box_relay, refresh_relay)
    return relay.url, _box_stream_headers(relay, client_key, session_id)


def _stream_events(model: str, prep: Prepared) -> AsyncGenerator[dict[str, Any], None]:
    if USE_GROKBOT:
        return stream_grokbot(model, prep)
    return stream_cursor(
        model,
        prep.messages_pb,
        prep.tools_pb,
        prep.effort,
        prep.max_tokens,
        isolated=prep.isolated,
    )


async def _stream_cursor_locked(
    client,
    model,
    messages_pb,
    tools_pb,
    parameters,
    client_key,
    max_tokens,
    started,
) -> AsyncGenerator[dict[str, Any], None]:
    for attempt in range(MAX_RETRIES + 1):
        conversation_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        url, headers = await _stream_target(client_key, session_id, refresh_relay=attempt > 0)
        body = P.frame(
            P.inference_stream_request_full(
                model,
                messages_pb,
                tools_pb,
                conversation_id,
                parameters,
                max_tokens=max_tokens or None,
                max_mode=True,
            )
        )
        emitted = False
        limit_hit = False
        first_at = None
        try:
            async with client.stream(
                "POST", url, content=body, headers=headers
            ) as resp:
                http_ver = getattr(resp, "http_version", "?")
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    _log(
                        f"upstream {resp.status_code} {http_ver} attempt={attempt + 1} {detail[:120]}"
                    )
                    if (
                        BACKEND_MODE == "box-relay"
                        and resp.status_code in (401, 403)
                        and attempt < MAX_RETRIES
                    ):
                        _log("box relay 401/403, refreshing grok-box-relay.json")
                        await asyncio.to_thread(_load_box_relay, True)
                        await asyncio.sleep(0.4)
                        continue
                    if resp.status_code == 429 and attempt < MAX_RETRIES:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    if resp.status_code in P.GATEWAY_CODES and attempt < MAX_RETRIES:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if BACKEND_MODE == "box-relay" and resp.status_code == 404:
                        raise UpstreamError(
                            404,
                            "Box relay 未挂载。请在 Grok Bot 当前 Box 重发 relay 指令，"
                            "或运行 python sand_stream_installer_v134.py provision-box",
                            "relay_not_mounted",
                        )
                    raise UpstreamError(resp.status_code, detail, "upstream_error")
                frame_n = 0
                async for flag, payload in adeframe(resp.aiter_bytes()):
                    if first_at is None:
                        first_at = time.perf_counter()
                        _log(
                            f"upstream TTFB {first_at - started:.2f}s "
                            f"{http_ver} model={model} attempt={attempt + 1}"
                        )
                    parsed = P.pb_parse(payload) if not (flag & P.FLAG_END_STREAM) else {}
                    frame_n += 1
                    if flag & P.FLAG_END_STREAM:
                        trailer = payload.decode("utf-8", "replace").strip()
                        info = _parse_trailer_info(trailer) if trailer else {}
                        if trailer and trailer not in ("{}", ""):
                            msg, code = info.get("message") or "", info.get("code") or ""
                            if msg and limit_hit:
                                # 已按 length 收尾，trailer 里重复报的 exceeded 只记日志
                                _log(f"upstream trailer after token limit: {code} {msg}")
                            elif msg:
                                status = _trailer_http_status(info)
                                _log(
                                    f"upstream trailer error attempt={attempt + 1} "
                                    f"code={code or '-'} provider={info.get('providerStatus') or '-'} "
                                    f"emitted={emitted}: {msg[:200]}"
                                )
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
                        _log(f"upstream done {time.perf_counter() - started:.2f}s model={model}")
                        return
                    for ev in _events_from_payload(parsed):
                        if ev["type"] in ("thinking", "text", "tool_call", "stop"):
                            emitted = True
                        if ev["type"] == "stop":
                            limit_hit = True
                        yield ev
            return
        except _Retry:
            continue
        except UpstreamError as exc:
            if not emitted and attempt < MAX_RETRIES and exc.status == 429:
                _log(f"upstream provider retry attempt={attempt + 1}: {exc.message[:160]}")
                await asyncio.sleep(1.8 * (attempt + 1))
                continue
            raise
        except httpx.HTTPError as exc:
            if emitted:
                raise UpstreamError(502, f"stream interrupted: {exc}", "upstream_error")
            if attempt < MAX_RETRIES:
                _log(f"upstream {type(exc).__name__}: {exc}; retry {attempt + 1}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(504, f"upstream unreachable: {exc}", "upstream_error") from exc
    raise UpstreamError(504, "exhausted retries", "upstream_error")


class _Retry(Exception):
    pass


def _int(v: Any) -> int:
    return int(v) if isinstance(v, int) else 0


# ---------------------------------------------------------------------------
# 工具调用分帧归并：流式 / 非流式共用同一套语义
# ---------------------------------------------------------------------------


class ToolCallTracker:
    """把上游 tool_call_part 分帧归并成完整调用。

    归并键优先用 tool_call_id：实测 grok 经 Cursor 返回的并行调用所有帧 tool_index 都是 0，
    只有 id 不同（且每帧都带 id）；只按 index 会把两个调用拼成一个非法 JSON。帧上没有 id
    时，tool_index 能唯一对应某个调用就用它，否则归到最近打开的调用（分帧在时间上连续）。

    feed() 返回本帧应当追加发出的参数片段：非 complete 帧原样追加；complete 帧带全量
    args 时只补发尚未发出的后缀（前面没有增量帧时即补发全量）；complete 帧 args 为空
    则保留已累积内容。这样"首帧带 args"、"只有 name 帧 + complete 帧"、"单帧 complete"、
    "complete 帧为空"几种上游分帧方式都不会丢参数。
    """

    def __init__(self, id_prefix: str = "call_") -> None:
        self.id_prefix = id_prefix
        self.order: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._by_index: dict[int, list[dict]] = {}
        self._current: Optional[dict] = None

    def _open(self, ev: dict) -> dict:
        call = {
            "pos": len(self.order),
            "id": ev["id"] or (self.id_prefix + uuid.uuid4().hex[:24]),
            "name": ev["name"],
            "args": "",
        }
        self.order.append(call)
        self._by_id[call["id"]] = call
        self._by_index.setdefault(ev["index"], []).append(call)
        return call

    def _resolve(self, ev: dict) -> tuple[dict, bool]:
        if ev["id"]:
            call = self._by_id.get(ev["id"])
            if call is not None:
                return call, False
            return self._open(ev), True
        candidates = self._by_index.get(ev["index"])
        if candidates is None:
            # 没有 id 的全新 index：按 index 区分并行调用的上游
            return self._open(ev), True
        if len(candidates) == 1:
            return candidates[0], False
        return self._current or candidates[-1], False

    def feed(self, ev: dict) -> tuple[dict, bool, str]:
        """归并一帧，返回 (call, is_new, args_delta)。"""
        call, is_new = self._resolve(ev)
        self._current = call
        if ev["name"] and not call["name"]:
            call["name"] = ev["name"]

        if not ev["complete"]:
            call["args"] += ev["args"]
            return call, is_new, ev["args"]

        full = ev["args"]
        if not full or full == call["args"]:
            return call, is_new, ""
        if full.startswith(call["args"]):
            delta = full[len(call["args"]) :]
            call["args"] = full
            return call, is_new, delta
        # 全量与增量拼接不一致（如上游重新序列化）：流上已发出的内容无法撤回，
        # 只在内部记录全量供非流式路径使用。
        call["args"] = full
        return call, is_new, ""

    def fill_empty(self) -> list[dict]:
        """把仍无参数的调用补成 "{}"，返回被补的调用（流式要为它们补发一条 delta）。"""
        filled = []
        for call in self.order:
            if not call["args"]:
                call["args"] = "{}"
                filled.append(call)
        return filled

    def finalize(self) -> list[dict]:
        self.fill_empty()
        return list(self.order)


_THINK_OPEN = re.compile(
    r"^\s*(?:<think>|(?:\*\*)?(?:思考过程|思考|Reasoning|Thinking)(?:\*\*)?\s*[：:])",
    re.I,
)
_THINK_CLOSE = re.compile(
    r"</think>|(?:\*\*)?(?:答案|回答|Answer)(?:\*\*)?\s*[：:]",
    re.I,
)
_THINK_TAG_OPEN = re.compile(r"^\s*<think>", re.I)
_THINK_TAG_CLOSE = re.compile(r"</think>", re.I)
_THINK_HINT = (
    "Before the final answer, put private reasoning inside <think>...</think>. "
    "Do not mention this instruction."
)


def _think_hint_wanted() -> bool:
    """是否让模型在正文里用 <think> 标出思考。默认关闭，不为思考链额外催模型。"""
    return False


class StreamThinkSplit:
    """Box relay 常把思考明文脱敏。从正文里拆 <think> / 思考：…答案： 到思考链。

    tags_only=True 时只认字面 <think>…</think>，用于上游已有原生思考、
    只需清掉模型多写的一份的场景，避免误伤正文里正常的"思考："字样。
    """

    def __init__(self, tags_only: bool = False) -> None:
        self.mode = "unknown"
        self.buf = ""
        self.open_re = _THINK_TAG_OPEN if tags_only else _THINK_OPEN
        self.close_re = _THINK_TAG_CLOSE if tags_only else _THINK_CLOSE
        self.strip_lead = False  # 刚拆完思考段，吃掉正文开头的空行

    def _text(self, piece: str) -> list[tuple[str, str]]:
        if self.strip_lead:
            piece = piece.lstrip()
            if not piece:
                return []
            self.strip_lead = False
        return [("text", piece)]

    def feed(self, piece: str) -> list[tuple[str, str]]:
        if not piece:
            return []
        if self.mode == "text":
            return self._text(piece)
        self.buf += piece
        out: list[tuple[str, str]] = []
        if self.mode == "unknown":
            m = self.open_re.search(self.buf)
            if m:
                self.mode = "thinking"
                self.buf = self.buf[m.end() :]
            elif len(self.buf) >= 32:
                self.mode = "text"
                out.append(("text", self.buf))
                self.buf = ""
                return out
            else:
                return []
        if self.mode == "thinking":
            m = self.close_re.search(self.buf)
            if m:
                think = self.buf[: m.start()]
                rest = self.buf[m.end() :]
                label = m.group(0)
                self.mode = "text"
                self.buf = ""
                if think:
                    out.append(("thinking", think))
                if "</think>" not in label.lower():
                    out.append(("text", label + rest))
                else:
                    self.strip_lead = True
                    out.extend(self._text(rest))
            elif len(self.buf) > 24:
                out.append(("thinking", self.buf[:-16]))
                self.buf = self.buf[-16:]
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        kind = "thinking" if self.mode == "thinking" else "text"
        text, self.buf = self.buf, ""
        return [(kind, text)] if text else []


def split_think_text(text: str, tags_only: bool = False) -> tuple[str, str]:
    """非流式：把整段正文开头的思考段拆出来，返回 (thinking, text)。"""
    if not text:
        return "", ""
    splitter = StreamThinkSplit(tags_only=tags_only)
    parts = splitter.feed(text) + splitter.flush()
    thinking = "".join(t for k, t in parts if k == "thinking")
    body = "".join(t for k, t in parts if k == "text")
    return thinking, body


def _append_system(msgs: list[Msg], extra: str) -> list[Msg]:
    for m in msgs:
        if m.role != "system":
            continue
        for p in m.parts:
            if p.kind == "text":
                if extra not in (p.text or ""):
                    p.text = ((p.text or "").rstrip() + "\n\n" + extra).strip()
                return msgs
        m.parts.append(Part("text", text=extra))
        return msgs
    return [Msg("system", [Part("text", text=extra)]), *msgs]


class StopSequenceFilter:
    """按 stop 序列截断正文。序列可能跨块，先扣住可能是前缀的尾部再放行。"""

    def __init__(self, stops: list[str]) -> None:
        self.stops = [s for s in stops if s]
        self.holdback = max((len(s) for s in self.stops), default=1) - 1
        self.pending = ""
        self.matched: Optional[str] = None

    def feed(self, text: str) -> str:
        """返回可以安全发出的文本；命中后 matched 记录序列，之后不再放行。"""
        if self.matched is not None:
            return ""
        if not self.stops:
            return text
        self.pending += text
        best: Optional[tuple[int, str]] = None
        for s in self.stops:
            pos = self.pending.find(s)
            if pos != -1 and (best is None or pos < best[0]):
                best = (pos, s)
        if best is not None:
            out = self.pending[: best[0]]
            self.pending = ""
            self.matched = best[1]
            return out
        cut = max(0, len(self.pending) - self.holdback)
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return out

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return out


# ---------------------------------------------------------------------------
# 请求准备 / 结果收集
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    messages_pb: list[bytes]
    tools_pb: list[bytes]
    in_est: int
    effort: str
    max_tokens: int
    isolated: bool
    stops: list[str]
    show_thinking: bool = True
    user_text: str = ""  # grokbot 后端只吃最后一条 user 文本
    # 原始 Msg / 工具定义：account（AgentService/Run）链路按 MCP 工具重新编码
    msgs: list = field(default_factory=list)
    tools: list = field(default_factory=list)


@dataclass
class Collected:
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(
        default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    )
    finish: str = "stop"  # stop / length / stop_sequence
    stop_sequence: Optional[str] = None


def _provider_retryable(message: str) -> bool:
    text = (message or "").lower()
    return any(hint.lower() in text for hint in _PROVIDER_RETRY_HINTS)


def _log_encode(proto: str, msgs: list[Msg], tools: list[dict], in_est: int, isolated: bool) -> None:
    preview = ""
    for m in reversed(msgs):
        if m.role == "user":
            preview = "".join(p.text for p in m.parts if p.kind == "text")[:80]
            break
    _log(
        f"encode {proto} msgs={len(msgs)} tools={len(tools)} "
        f"in_tokens~{in_est} isolated={isolated} user={preview!r}"
    )


async def _prepare(fn: Callable[[dict], Awaitable[Prepared]], payload: dict) -> Prepared:
    try:
        return await fn(payload)
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(400, f"failed to encode request: {exc}", "invalid_request") from exc


async def collect_events(model: str, prep: Prepared) -> Collected:
    tracker = ToolCallTracker()
    stopper = StopSequenceFilter(prep.stops)
    res = Collected()
    thinking: list[str] = []
    answer: list[str] = []
    agen = _stream_events(model, prep)
    try:
        async for ev in agen:
            if ev["type"] == "thinking":
                thinking.append(ev["text"])
            elif ev["type"] == "text":
                answer.append(stopper.feed(ev["text"]))
                if stopper.matched is not None:
                    res.finish = "stop_sequence"
                    res.stop_sequence = stopper.matched
                    break
            elif ev["type"] == "tool_call":
                tracker.feed(ev)
            elif ev["type"] == "usage":
                res.usage.update(
                    {k: ev[k] for k in ("input", "output", "cache_read", "cache_write")}
                )
            elif ev["type"] == "stop":
                res.finish = ev["reason"]
    finally:
        await agen.aclose()
    answer.append(stopper.flush())
    res.thinking = "".join(thinking)
    # 正文开头若带 <think>…</think>，拆到思考链；已有原生思考时只认字面标签并丢弃重复的一份
    split_think, res.text = split_think_text("".join(answer), tags_only=bool(res.thinking))
    if split_think and not res.thinking:
        res.thinking = split_think
    res.tool_calls = tracker.finalize()
    return res


# ---------------------------------------------------------------------------
# 鉴权 / 请求体校验
# ---------------------------------------------------------------------------


def _check_auth(request: Request) -> None:
    if not SERVER_API_KEY:
        return
    auth = request.headers.get("authorization", "")
    key = (
        auth[7:]
        if auth.lower().startswith("bearer ")
        else request.headers.get("x-api-key", "")
    )
    if key != SERVER_API_KEY:
        raise UpstreamError(401, "invalid api key", "invalid_api_key")


async def _read_request(request: Request) -> dict:
    _check_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise UpstreamError(400, "request body must be valid JSON", "invalid_json") from exc
    if not isinstance(payload, dict):
        raise UpstreamError(400, "request body must be a JSON object", "invalid_request")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise UpstreamError(400, "messages must be a non-empty array", "invalid_request")
    if not all(isinstance(m, dict) for m in messages):
        raise UpstreamError(400, "each message must be an object", "invalid_request")
    return payload


@app.get("/v1/models")
def list_models() -> dict:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "cursor-sand"}
            for m in DEFAULT_MODELS
        ],
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "sand-openai-anthropic-bridge",
        "endpoints": [
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/images/generations",
            "/v1/images/edits",
            "/v1/models",
        ],
        "backend": BACKEND_MODE,
        "upstream": (
            BOX_RELAY_PATH
            if BACKEND_MODE == "box-relay"
            else "GrokBotService"
            if BACKEND_MODE == "grokbot"
            else f"{P.BACKEND}{P.RPC_STREAM}"
        ),
    }


# ---------------------------------------------------------------------------
# OpenAI: /v1/chat/completions
# ---------------------------------------------------------------------------


def _is_thinking_model(model: str) -> bool:
    name = (model or "").lower()
    return any(marker in name for marker in _THINKING_MODEL_MARKERS)


def _model_has_effort_slug(model: str) -> bool:
    name = (model or "").lower()
    return any(name.endswith(suffix) for suffix in _MODEL_EFFORT_SUFFIXES)


def _normalize_effort(model: str, explicit: str = "") -> str:
    if _model_has_effort_slug(model):
        return ""
    raw = (explicit or "").strip().lower()
    aliases = {
        "xhigh": "xhigh",
        "extra-high": "xhigh",
        "extra_high": "xhigh",
        "max": "high",
        "none": "",
        "off": "",
        "disabled": "",
    }
    if raw:
        return aliases.get(raw, raw)
    fallback = (DEFAULT_EFFORT or "").strip().lower()
    if fallback in ("", "none", "off", "disabled"):
        return ""
    if _is_thinking_model(model):
        return aliases.get(fallback, fallback)
    return ""


def _openai_effort(payload: dict, model: str) -> str:
    eff = payload.get("reasoning_effort")
    if isinstance(eff, str):
        return _normalize_effort(model, eff)
    if isinstance(payload.get("reasoning"), dict):
        return _normalize_effort(model, str(payload["reasoning"].get("effort", "") or ""))
    return _normalize_effort(model)


def _last_user_text(msgs: list[Msg]) -> str:
    for m in reversed(msgs):
        if m.role == "user":
            txt = "".join(p.text for p in m.parts if p.kind == "text")
            if txt.strip():
                return txt
    return ""


async def _prepare_openai(payload: dict) -> Prepared:
    model = str(payload.get("model") or "claude-opus-5")
    msgs = _ensure_system(parse_openai(payload.get("messages") or []))
    _apply_response_format(msgs, payload.get("response_format"))
    await _resolve_remote_images(msgs)
    tools = _apply_tool_choice(parse_openai_tools(payload), payload.get("tool_choice"))
    if _is_thinking_model(model) and not tools and _think_hint_wanted():
        msgs = _append_system(msgs, _THINK_HINT)
    messages_pb, in_est = await asyncio.to_thread(build_cursor_messages, msgs)
    tools_pb = [P.agent_tool(t["name"], t["description"], t["parameters"]) for t in tools]
    effort = _openai_effort(payload, model)
    isolated = not tools and in_est < 64 and not effort
    _log_encode("openai", msgs, tools, in_est, isolated)
    return Prepared(
        messages_pb=messages_pb,
        tools_pb=tools_pb,
        in_est=in_est,
        effort=effort,
        max_tokens=_max_tokens_of(payload),
        isolated=isolated,
        stops=_stops_of(payload.get("stop")),
        user_text=_last_user_text(msgs),
        msgs=msgs,
        tools=tools,
    )


def _openai_finish(finish: str, has_tools: bool) -> str:
    if finish == "length":
        return "length"
    return "tool_calls" if has_tools else "stop"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        payload = await _read_request(request)
    except UpstreamError as exc:
        return _openai_error_response(exc)
    model = _resolve_model_slug(payload.get("model") or "claude-opus-5")
    payload["model"] = model
    stream = bool(payload.get("stream"))
    cid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    _log(
        f"POST /v1/chat/completions model={model} stream={stream} "
        f"msgs={len(payload['messages'])} max_tokens={_max_tokens_of(payload)}"
    )

    if stream:
        return StreamingResponse(
            _openai_stream(payload, model, cid, created),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    try:
        prep = await _prepare(_prepare_openai, payload)
        res = await collect_events(model, prep)
    except UpstreamError as exc:
        return _openai_error_response(exc)

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
    out_tokens = res.usage["output"] or estimate_tokens(
        res.text + res.thinking + "".join(c["args"] for c in res.tool_calls)
    )
    in_tokens = res.usage["input"] or prep.in_est
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": _openai_finish(res.finish, bool(res.tool_calls)),
            }
        ],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
            "prompt_tokens_details": {"cached_tokens": res.usage["cache_read"]},
        },
    }


async def _openai_stream(payload: dict, model: str, cid: str, created: int):
    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def error_chunk(exc: UpstreamError) -> str:
        body = _openai_error_body(exc.status, exc.message, exc.code)
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

    def tool_chunk(call: dict, args: str, first: bool) -> str:
        item: dict[str, Any] = {"index": call["pos"], "function": {"arguments": args}}
        if first:
            item["id"] = call["id"]
            item["type"] = "function"
            item["function"]["name"] = call["name"]
        return chunk({"tool_calls": [item]})

    # 先把首帧刷给 Riot，再做 protobuf / 上游建连。连接测试等的是完整
    # Message，但至少 HTTP/SSE 不会 30 秒毫无字节。
    yield chunk({"role": "assistant", "content": ""})
    try:
        prep = await _prepare(_prepare_openai, payload)
    except UpstreamError as exc:
        yield error_chunk(exc)
        return

    tracker = ToolCallTracker()
    stopper = StopSequenceFilter(prep.stops)
    # 原生思考帧通常先于正文到达；到第一段正文时再建 splitter，
    # 已有原生思考就只认字面 <think> 标签，拆出来的重复思考直接丢弃。
    splitter: Optional[StreamThinkSplit] = None
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    out_len = 0
    finish = "stop"
    got_thinking = False

    def split_text(piece: str) -> list[str]:
        nonlocal splitter, out_len
        if splitter is None:
            splitter = StreamThinkSplit(tags_only=got_thinking)
        out: list[str] = []
        for kind, text in splitter.feed(piece):
            if not text:
                continue
            out_len += len(text)
            if kind == "thinking":
                if not got_thinking:
                    out.append(chunk({"reasoning_content": text}))
            else:
                out.append(chunk({"content": text}))
        return out

    agen = _stream_events(model, prep)
    try:
        async for ev in agen:
            if ev["type"] == "thinking":
                got_thinking = True
                out_len += len(ev["text"])
                yield chunk({"reasoning_content": ev["text"]})
            elif ev["type"] == "text":
                for s in split_text(stopper.feed(ev["text"])):
                    yield s
                if stopper.matched is not None:
                    break
            elif ev["type"] == "tool_call":
                call, is_new, delta = tracker.feed(ev)
                if is_new:
                    yield tool_chunk(call, delta, first=True)
                elif delta:
                    yield tool_chunk(call, delta, first=False)
            elif ev["type"] == "usage":
                usage.update(
                    {k: ev[k] for k in ("input", "output", "cache_read", "cache_write")}
                )
            elif ev["type"] == "stop":
                finish = ev["reason"]
    except UpstreamError as exc:
        yield error_chunk(exc)
        return
    finally:
        await agen.aclose()

    for s in split_text(stopper.flush()):
        yield s
    if splitter is not None:
        for kind, text in splitter.flush():
            if not text:
                continue
            out_len += len(text)
            if kind == "thinking":
                if not got_thinking:
                    yield chunk({"reasoning_content": text})
            else:
                yield chunk({"content": text})
    # 没收到任何参数的工具补一个 "{}"，客户端拼出来才是合法 JSON
    for call in tracker.fill_empty():
        yield tool_chunk(call, "{}", first=False)

    in_tokens = usage["input"] or prep.in_est
    out_tokens = usage["output"] or max(1, out_len // 4)
    final = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "logprobs": None,
                "finish_reason": _openai_finish(finish, bool(tracker.order)),
            }
        ],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
            "prompt_tokens_details": {"cached_tokens": usage["cache_read"]},
        },
    }
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# OpenAI: /v1/images/generations
# 直连 api2 AiService.RunGenerateImage，不走 box-relay。
# ---------------------------------------------------------------------------


def _image_payload_refs(payload: dict) -> list[Any]:
    refs: list[Any] = []
    for key in ("image", "images", "reference_images"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            refs.extend(value)
        else:
            refs.append(value)
    return refs


def _image_gen_kwargs(payload: dict, *, require_prompt: bool = True) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if require_prompt and not prompt:
        raise UpstreamError(400, "prompt is required", "invalid_request")
    n = payload.get("n", 1)
    try:
        n = int(n)
    except (TypeError, ValueError) as exc:
        raise UpstreamError(400, "n must be 1", "invalid_request") from exc
    if n != 1:
        raise UpstreamError(400, "only n=1 is supported", "invalid_request")
    fmt = str(payload.get("response_format") or "b64_json").strip().lower()
    if fmt not in {"b64_json", "b64", ""}:
        raise UpstreamError(400, "only response_format=b64_json is supported", "invalid_request")
    aspect = payload.get("aspect_ratio") or payload.get("size") or None
    if aspect is not None:
        aspect = str(aspect).strip() or None
    quality = payload.get("quality")
    if quality is not None:
        quality = str(quality).strip() or None
    max_mode = payload.get("max_mode")
    if max_mode is None:
        use_max_mode = True
    else:
        use_max_mode = bool(max_mode)
    timeout = payload.get("timeout")
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "model": str(payload.get("model") or GI.DEFAULT_MODEL),
        "max_mode": use_max_mode,
        "quality": quality,
        "aspect_ratio": aspect,
        "reference_images": _image_payload_refs(payload) or None,
    }
    if timeout is not None:
        try:
            kwargs["timeout"] = float(timeout)
        except (TypeError, ValueError) as exc:
            raise UpstreamError(400, "timeout must be a number", "invalid_request") from exc
    return kwargs


async def _run_generate_image(payload: dict) -> dict[str, Any]:
    kwargs = _image_gen_kwargs(payload)
    _log(
        "POST image "
        f"model={kwargs['model']} prompt_len={len(kwargs['prompt'])} "
        f"aspect={kwargs.get('aspect_ratio') or '-'} "
        f"quality={kwargs.get('quality') or '-'} "
        f"refs={len(kwargs['reference_images'] or [])}"
    )
    return await asyncio.to_thread(GI.generate_image, **kwargs)


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    try:
        _check_auth(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise UpstreamError(400, "request body must be valid JSON", "invalid_json") from exc
        if not isinstance(payload, dict):
            raise UpstreamError(400, "request body must be a JSON object", "invalid_request")
        result = await _run_generate_image(payload)
    except UpstreamError as exc:
        return _openai_error_response(exc)
    except GI.GenerateImageError as exc:
        return _openai_error_response(UpstreamError(exc.status, exc.args[0], exc.code))
    image_b64 = base64.b64encode(result["bytes"]).decode("ascii")
    return {
        "created": int(time.time()),
        "data": [{"b64_json": image_b64, "revised_prompt": None}],
    }


@app.post("/v1/images/edits")
async def images_edits(request: Request):
    try:
        _check_auth(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise UpstreamError(400, "request body must be valid JSON", "invalid_json") from exc
        if not isinstance(payload, dict):
            raise UpstreamError(400, "request body must be a JSON object", "invalid_request")
        if not _image_payload_refs(payload):
            raise UpstreamError(400, "image is required", "invalid_request")
        result = await _run_generate_image(payload)
    except UpstreamError as exc:
        return _openai_error_response(exc)
    except GI.GenerateImageError as exc:
        return _openai_error_response(UpstreamError(exc.status, exc.args[0], exc.code))
    image_b64 = base64.b64encode(result["bytes"]).decode("ascii")
    return {
        "created": int(time.time()),
        "data": [{"b64_json": image_b64, "revised_prompt": None}],
    }


# ---------------------------------------------------------------------------
# Anthropic: /v1/messages
# ---------------------------------------------------------------------------


def _anthropic_effort(payload: dict, model: str) -> str:
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").lower() == "enabled":
        return _normalize_effort(model, "high")
    return ""


async def _prepare_anthropic(payload: dict) -> Prepared:
    model = str(payload.get("model") or "claude-opus-5")
    msgs = _ensure_system(parse_anthropic(payload.get("messages") or [], payload.get("system")))
    await _resolve_remote_images(msgs)
    tools = _apply_tool_choice(parse_anthropic_tools(payload), payload.get("tool_choice"))
    messages_pb, in_est = await asyncio.to_thread(build_cursor_messages, msgs)
    tools_pb = [P.agent_tool(t["name"], t["description"], t["parameters"]) for t in tools]
    effort = _anthropic_effort(payload, model)
    isolated = not tools and in_est < 64 and not effort
    _log_encode("anthropic", msgs, tools, in_est, isolated)
    return Prepared(
        messages_pb=messages_pb,
        tools_pb=tools_pb,
        in_est=in_est,
        effort=effort,
        max_tokens=_max_tokens_of(payload),
        isolated=isolated,
        stops=_stops_of(payload.get("stop_sequences")),
        show_thinking=True,
        user_text=_last_user_text(msgs),
        msgs=msgs,
        tools=tools,
    )


def _anthropic_stop_reason(finish: str, has_tools: bool) -> str:
    if finish == "length":
        return "max_tokens"
    if finish == "stop_sequence":
        return "stop_sequence"
    return "tool_use" if has_tools else "end_turn"


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    try:
        payload = await _read_request(request)
    except UpstreamError as exc:
        return _anthropic_error_response(exc)
    model = _resolve_model_slug(payload.get("model") or "claude-opus-5")
    payload["model"] = model
    stream = bool(payload.get("stream"))
    mid = "msg_" + uuid.uuid4().hex
    _log(
        f"POST /v1/messages model={model} stream={stream} "
        f"msgs={len(payload['messages'])} max_tokens={_max_tokens_of(payload)}"
    )

    if stream:
        return StreamingResponse(
            _anthropic_stream(payload, model, mid),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    try:
        prep = await _prepare(_prepare_anthropic, payload)
        res = await collect_events(model, prep)
    except UpstreamError as exc:
        return _anthropic_error_response(exc)

    content: list[dict[str, Any]] = []
    if res.thinking and prep.show_thinking:
        content.append({"type": "thinking", "thinking": res.thinking, "signature": ""})
    if res.text:
        content.append({"type": "text", "text": res.text})
    for c in res.tool_calls:
        content.append(
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": _args_to_obj(c["args"])}
        )
    in_tokens = res.usage["input"] or prep.in_est
    out_tokens = res.usage["output"] or estimate_tokens(
        res.text + res.thinking + "".join(c["args"] for c in res.tool_calls)
    )
    return {
        "id": mid,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": _anthropic_stop_reason(res.finish, bool(res.tool_calls)),
        "stop_sequence": res.stop_sequence,
        "usage": {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "cache_read_input_tokens": res.usage["cache_read"],
            "cache_creation_input_tokens": res.usage["cache_write"],
        },
    }


async def _anthropic_stream(payload: dict, model: str, mid: str):
    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def error_event(exc: UpstreamError) -> str:
        return sse("error", _anthropic_error_body(exc.status, exc.message))

    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": mid,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    )
    try:
        prep = await _prepare(_prepare_anthropic, payload)
    except UpstreamError as exc:
        yield error_event(exc)
        return

    block_index = -1
    block_kind: Optional[str] = None  # thinking / text / tool
    block_call: Optional[dict] = None  # 当前 tool block 对应的调用
    tool_blocks: dict[int, int] = {}  # call["pos"] → content block index
    tracker = ToolCallTracker(id_prefix="toolu_")
    stopper = StopSequenceFilter(prep.stops)
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    out_len = 0
    finish = "stop"
    started_ping = False

    def delta_event(delta: dict, index: Optional[int] = None) -> str:
        return sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": block_index if index is None else index,
                "delta": delta,
            },
        )

    def start_block(kind: str, content_block: dict) -> str:
        nonlocal block_index, block_kind
        block_index += 1
        block_kind = kind
        return sse(
            "content_block_start",
            {"type": "content_block_start", "index": block_index, "content_block": content_block},
        )

    def stop_block() -> list[str]:
        nonlocal block_kind, block_call
        out: list[str] = []
        if block_kind == "thinking":
            out.append(delta_event({"type": "signature_delta", "signature": ""}))
        elif block_kind == "tool" and block_call is not None and not block_call["args"]:
            block_call["args"] = "{}"
            out.append(delta_event({"type": "input_json_delta", "partial_json": "{}"}))
        out.append(sse("content_block_stop", {"type": "content_block_stop", "index": block_index}))
        block_kind = None
        block_call = None
        return out

    def emit_text(kind: str, text: str) -> list[str]:
        nonlocal out_len
        out: list[str] = []
        if not text:
            return out
        if block_kind != kind:
            if block_kind is not None:
                out.extend(stop_block())
            block = (
                {"type": "thinking", "thinking": "", "signature": ""}
                if kind == "thinking"
                else {"type": "text", "text": ""}
            )
            out.append(start_block(kind, block))
        out_len += len(text)
        delta = (
            {"type": "thinking_delta", "thinking": text}
            if kind == "thinking"
            else {"type": "text_delta", "text": text}
        )
        out.append(delta_event(delta))
        return out

    agen = _stream_events(model, prep)
    try:
        async for ev in agen:
            if not started_ping:
                started_ping = True
                yield sse("ping", {"type": "ping"})

            if ev["type"] == "thinking":
                if prep.show_thinking:
                    for s in emit_text("thinking", ev["text"]):
                        yield s
            elif ev["type"] == "text":
                for s in emit_text("text", stopper.feed(ev["text"])):
                    yield s
                if stopper.matched is not None:
                    finish = "stop_sequence"
                    break
            elif ev["type"] == "tool_call":
                call, is_new, delta = tracker.feed(ev)
                if is_new:
                    if block_kind is not None:
                        for s in stop_block():
                            yield s
                    yield start_block(
                        "tool",
                        {"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}},
                    )
                    block_call = call
                    tool_blocks[call["pos"]] = block_index
                if delta:
                    yield delta_event(
                        {"type": "input_json_delta", "partial_json": delta},
                        index=tool_blocks[call["pos"]],
                    )
            elif ev["type"] == "usage":
                usage.update(
                    {k: ev[k] for k in ("input", "output", "cache_read", "cache_write")}
                )
            elif ev["type"] == "stop":
                finish = ev["reason"]
    except UpstreamError as exc:
        if block_kind is not None:
            for s in stop_block():
                yield s
        yield error_event(exc)
        return
    finally:
        await agen.aclose()

    for s in emit_text("text", stopper.flush()):
        yield s
    if block_kind is not None:
        for s in stop_block():
            yield s

    in_tokens = usage["input"] or prep.in_est
    out_tokens = usage["output"] or max(1, out_len // 4)
    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": _anthropic_stop_reason(finish, bool(tracker.order)),
                "stop_sequence": stopper.matched,
            },
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_read_input_tokens": usage["cache_read"],
                "cache_creation_input_tokens": usage["cache_write"],
            },
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("SAND_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("SAND_SERVER_PORT", "8787"))
    uvicorn.run(app, host=host, port=port)
