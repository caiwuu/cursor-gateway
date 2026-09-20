#!/usr/bin/env python3
"""RunInference 直连探针。

不依赖 Cursor 进程，自行拼装 Connect BiDi 流打 agentn.api5，验证服务端是否
接受非 IDE 来源的 sand 请求。

依赖：httpx[http2]
用法：python3 probe_runinference.py [-m MODEL] [-p PROMPT] [-v]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import sqlite3
import secrets
import struct
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

try:
    import httpx
except ImportError:
    sys.exit("缺少依赖，请先执行： pip install 'httpx[http2]'")


# 推理端点在 api2。IDE 的 sand 走 InferenceService/Stream（ServerStreaming），
# 不是 RunInference（后者对 sand 直接回 "Sand traffic is not supported"）。
DEFAULT_BACKEND = "https://api2.cursor.sh"
BACKEND = os.environ.get("CURSOR_API_ENDPOINT", DEFAULT_BACKEND)
CLIENT_VERSION = os.environ.get("CURSOR_CLIENT_VERSION", "3.18.9")

ROLE_USER = 1
ROLE_ASSISTANT = 2
ROLE_TOOL = 3
ROLE_SYSTEM = 4


# ---------------------------------------------------------------------------
# protobuf 最小编解码
#
# 只覆盖本探针用到的 wire type 0/2。字段编号取自 657.js 里的 proto3 fieldList，
# 与 aiserver.v1 保持一致。
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def tag(field_no: int, wire_type: int) -> bytes:
    return _varint(field_no << 3 | wire_type)


def pb_bytes(field_no: int, payload: bytes) -> bytes:
    return tag(field_no, 2) + _varint(len(payload)) + payload


def pb_str(field_no: int, text: str) -> bytes:
    return pb_bytes(field_no, text.encode("utf-8"))


def pb_bool(field_no: int, value: bool) -> bytes:
    return tag(field_no, 0) + _varint(1 if value else 0)


def pb_enum(field_no: int, value: int) -> bytes:
    return tag(field_no, 0) + _varint(value)


def pb_parse(buf: Optional[bytes]) -> dict[int, list[tuple[int, Any]]]:
    """解析成 {field_no: [(wire_type, value), ...]}，未知字段一并保留。

    子消息字段缺失时 first() 返回 None（或标量），这里统一按空消息处理，
    这样 pb_parse(first(...)) 的链式调用不必到处判空。
    """
    out: dict[int, list[tuple[int, Any]]] = {}
    if not isinstance(buf, (bytes, bytearray)):
        return out
    buf = bytes(buf)
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            value = buf[pos : pos + length]
            pos += length
        elif wire_type == 5:
            value = buf[pos : pos + 4]
            pos += 4
        elif wire_type == 1:
            value = buf[pos : pos + 8]
            pos += 8
        else:
            raise ValueError(f"不支持的 wire type {wire_type}（field {field_no}）")
        out.setdefault(field_no, []).append((wire_type, value))
    return out


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def first(parsed: dict[int, list[tuple[int, Any]]], field_no: int) -> Optional[Any]:
    entries = parsed.get(field_no)
    return entries[0][1] if entries else None


def as_text(value: Optional[bytes]) -> str:
    return value.decode("utf-8", "replace") if value else ""


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------


@dataclass
class Credentials:
    access_token: str
    machine_id: str
    mac_machine_id: str
    email: str = ""
    source: str = ""


def _sand_config_dir() -> pathlib.Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = pathlib.Path(base) if base else pathlib.Path.home() / "AppData/Local"
        return root / "SandClientModeStream" / "sand-client-cli"
    if sys.platform == "darwin":
        return (
            pathlib.Path.home()
            / "Library/Application Support/SandClientModeStream/sand-client-cli"
        )
    return pathlib.Path.home() / ".config/SandClientModeStream/sand-client-cli"


def _cursor_global_storage() -> pathlib.Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = pathlib.Path(base) if base else pathlib.Path.home() / "AppData/Roaming"
        return root / "Cursor/User/globalStorage"
    if sys.platform == "darwin":
        return (
            pathlib.Path.home()
            / "Library/Application Support/Cursor/User/globalStorage"
        )
    return pathlib.Path.home() / ".config/Cursor/User/globalStorage"


def _read_vscdb(keys: tuple[str, ...]) -> dict[str, str]:
    """只读方式取几个 key。state.vscdb 可能有 20GB+，绝不整库扫描。"""
    db = _cursor_global_storage() / "state.vscdb"
    if not db.exists():
        return {}
    placeholders = ",".join("?" * len(keys))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})", keys
        ).fetchall()
    finally:
        conn.close()
    return {k: v for k, v in rows}


# ---------------------------------------------------------------------------
# 独立配置：不寄生 Cursor 目录，自持久化机器码，可存 token
# ---------------------------------------------------------------------------


def _probe_config_dir() -> pathlib.Path:
    override = os.environ.get("SAND_PROBE_HOME")
    if override:
        return pathlib.Path(override).expanduser()
    # 默认放脚本所在目录，随项目走。含 token/api_key，务必 gitignore。
    return pathlib.Path(__file__).resolve().parent


def _probe_config_path() -> pathlib.Path:
    # SAND_PROBE_HOME 目录内用 config.json；默认落脚本目录时用专名避免与通用配置混。
    name = "config.json" if os.environ.get("SAND_PROBE_HOME") else "sand-probe.config.json"
    return _probe_config_dir() / name


def _load_probe_config() -> dict[str, str]:
    path = _probe_config_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_probe_config(cfg: dict[str, str]) -> None:
    path = _probe_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _inherit_cursor_identity() -> tuple[str, str]:
    """读本机 IDE 正在用的机器码，让脚本与 IDE 在服务端算同一台电脑。

    优先 toolkit 假身份（与 IDE 外发一致），回落 storage.json 的 telemetry。
    读不到返回空串，交由调用方随机生成。
    """
    toolkit = _sand_config_dir() / "config.json"
    if toolkit.exists():
        try:
            ident = json.loads(toolkit.read_text()).get("spoofedIdentity") or {}
            if ident.get("machineId"):
                return ident["machineId"], ident.get("macMachineId", "")
        except (json.JSONDecodeError, OSError):
            pass
    storage = _cursor_global_storage() / "storage.json"
    if storage.exists():
        try:
            data = json.loads(storage.read_text())
            if data.get("telemetry.machineId"):
                return (
                    data["telemetry.machineId"],
                    data.get("telemetry.macMachineId", ""),
                )
        except (json.JSONDecodeError, OSError):
            pass
    return "", ""


def _ensure_identity(cfg: dict[str, str]) -> tuple[str, str]:
    """机器码：env 覆盖 > 配置持久化 > 继承 IDE 那套 > 随机生成。

    默认继承本机 IDE 的机器码，避免同账号在服务端多出一台"新电脑"而触发
    "Too many computers" 风控。一旦写入配置即固定，之后每次复用同一套。
    """
    mid = os.environ.get("CURSOR_MACHINE_ID") or cfg.get("machine_id", "")
    mac = os.environ.get("CURSOR_MAC_MACHINE_ID") or cfg.get("mac_machine_id", "")
    if mid and mac:
        return mid, mac
    seed_mid, seed_mac = _inherit_cursor_identity()
    if not mid:
        mid = seed_mid or secrets.token_hex(32)
    if not mac:
        mac = seed_mac or secrets.token_hex(32)
    cfg["machine_id"] = mid
    cfg["mac_machine_id"] = mac
    _save_probe_config(cfg)
    return mid, mac


def _jwt_expired(token: str, skew_seconds: int = 300) -> bool:
    """解析 JWT 的 exp 判断是否（临近）过期；解析失败一律当未过期，交给服务端。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return exp is not None and time.time() > exp - skew_seconds
    except Exception:
        return False


def load_credentials(use_cursor: bool = True) -> Credentials:
    cfg = _load_probe_config()
    env_token = os.environ.get("CURSOR_ACCESS_TOKEN")
    # token 来源优先级：环境变量 > 独立配置 > Cursor vscdb（便利回落）
    token = env_token or cfg.get("access_token", "")
    email = cfg.get("email", "")
    source = "env" if env_token else "独立配置"

    # 配置里存了 API key 时，token 缺失或过期就自动重换（key 长期有效）。
    if not env_token and cfg.get("api_key") and (not token or _jwt_expired(token)):
        refreshed = exchange_api_key(cfg["api_key"], BACKEND)
        if refreshed:
            token = refreshed["accessToken"]
            cfg["access_token"] = token
            cfg["refresh_token"] = refreshed["refreshToken"]
            _save_probe_config(cfg)
            source = "独立配置(API key 自动重换)"

    if not token and use_cursor:
        store = _read_vscdb(("cursorAuth/accessToken", "cursorAuth/cachedEmail"))
        token = store.get("cursorAuth/accessToken", "")
        email = store.get("cursorAuth/cachedEmail", "") or email
        source = "Cursor vscdb"
    if not token:
        raise SystemExit(
            "未找到 access token。三选一：设 CURSOR_ACCESS_TOKEN 环境变量、"
            "跑一次 --import-from-cursor 导入、或把 token 写进 "
            f"{_probe_config_path()}"
        )
    machine_id, mac_machine_id = _ensure_identity(cfg)
    return Credentials(
        access_token=token,
        machine_id=machine_id,
        mac_machine_id=mac_machine_id,
        email=email,
        source=source,
    )


def import_from_cursor() -> int:
    """把 Cursor 当前登录态导入独立配置：之后可 --no-cursor 完全独立运行。"""
    store = _read_vscdb(("cursorAuth/accessToken", "cursorAuth/cachedEmail"))
    token = store.get("cursorAuth/accessToken", "")
    if not token:
        raise SystemExit("Cursor 里没找到 access token（未登录？）")
    cfg = _load_probe_config()
    cfg["access_token"] = token
    if store.get("cursorAuth/cachedEmail"):
        cfg["email"] = store["cursorAuth/cachedEmail"]
    # 机器码尽量继承 toolkit 假身份，让独立身份与 IDE 外发的一致；缺则随机生成。
    toolkit = _sand_config_dir() / "config.json"
    if toolkit.exists() and not cfg.get("machine_id"):
        try:
            ident = json.loads(toolkit.read_text()).get("spoofedIdentity") or {}
            if ident.get("machineId"):
                cfg["machine_id"] = ident["machineId"]
                cfg["mac_machine_id"] = ident.get("macMachineId", "")
        except (json.JSONDecodeError, OSError):
            pass
    _ensure_identity(cfg)
    _save_probe_config(cfg)
    print(f"已导入到 {_probe_config_path()}")
    print("之后可加 --no-cursor 完全脱离 Cursor 目录运行。")
    return 0


# ---------------------------------------------------------------------------
# 网页授权登录（复刻 cursor-agent CLI 的 deep-control + auth/poll 流程）
# ---------------------------------------------------------------------------

WEBSITE = os.environ.get("CURSOR_WEBSITE_URL", "https://cursor.com")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def start_login() -> tuple[str, str, str]:
    """生成 PKCE 三元组与登录 URL。

    verifier = base64url(random32)；challenge = base64url(sha256(verifier))；
    redirectTarget=cli 让服务端把结果投递到 auth/poll，而不是回调本地 URI。
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    login_uuid = str(uuid.uuid4())
    login_url = (
        f"{WEBSITE}/loginDeepControl"
        f"?challenge={challenge}&uuid={login_uuid}&mode=login&redirectTarget=cli"
    )
    return login_uuid, verifier, login_url


def poll_login(login_uuid: str, verifier: str, backend: str) -> Optional[dict]:
    """轮询 auth/poll：404=等待授权，200+{accessToken,refreshToken}=成功。"""
    url = f"{backend}/auth/poll?uuid={login_uuid}&verifier={verifier}"
    fails = 0
    with httpx.Client(timeout=15.0) as client:
        for i in range(150):
            wait = min(1.0 * (1.2**i), 10.0)
            try:
                resp = client.get(url, headers={"Content-Type": "application/json"})
            except httpx.HTTPError:
                fails += 1
                if fails >= 3:
                    return None
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                fails = 0
                time.sleep(wait)
                continue
            if not resp.is_success:
                fails += 1
                if fails >= 3:
                    return None
                time.sleep(wait)
                continue
            data = resp.json()
            if (
                isinstance(data, dict)
                and "accessToken" in data
                and "refreshToken" in data
            ):
                return data
            return None
    return None


def cmd_login(backend: str) -> int:
    login_uuid, verifier, login_url = start_login()
    print("在浏览器打开以下链接完成登录（授权后自动继续）：\n")
    print(f"  {login_url}\n")
    try:
        import webbrowser

        webbrowser.open(login_url)
    except Exception:
        pass
    print("等待授权中…（最长约 3 分钟，Ctrl-C 取消）")
    try:
        result = poll_login(login_uuid, verifier, backend)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 1
    if not result:
        print("登录未完成或超时。")
        return 1
    cfg = _load_probe_config()
    cfg["access_token"] = result["accessToken"]
    cfg["refresh_token"] = result["refreshToken"]
    _ensure_identity(cfg)
    _save_probe_config(cfg)
    print(f"\n登录成功，token 已写入 {_probe_config_path()}")
    print("之后可加 --no-cursor 完全独立运行。")
    return 0


def exchange_api_key(api_key: str, backend: str) -> Optional[dict]:
    """用 Cursor API key 换 {accessToken, refreshToken}，无需浏览器。"""
    url = f"{backend}/auth/exchange_user_api_key"
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            content="{}",
        )
    if not resp.is_success:
        return None
    data = resp.json()
    if isinstance(data, dict) and "accessToken" in data and "refreshToken" in data:
        return data
    return None


def cmd_login_with_key(api_key: str, backend: str) -> int:
    result = exchange_api_key(api_key, backend)
    if not result:
        print("换取失败：API key 无效或被接口拒绝。")
        return 1
    cfg = _load_probe_config()
    cfg["access_token"] = result["accessToken"]
    cfg["refresh_token"] = result["refreshToken"]
    # 存下 API key：token 过期时可自动重换（key 长期有效，比 refresh 更省事）。
    cfg["api_key"] = api_key
    _ensure_identity(cfg)
    _save_probe_config(cfg)
    print(f"已用 API key 换取 token，写入 {_probe_config_path()}")
    print("token 过期时会用保存的 API key 自动重换；--no-cursor 即可独立运行。")
    return 0


def cursor_checksum(machine_id: str, mac_machine_id: str) -> str:
    """复刻 extensionHostProcess.js 的 JDe/YDe。

    时间戳 /1e6 取 6 字节大端，跑滚动异或后 base64url 无填充，再拼机器码。
    """
    stamp = int(time.time() * 1000) // 1_000_000
    raw = bytearray(
        (stamp >> shift) & 0xFF for shift in (40, 32, 24, 16, 8, 0)
    )
    prev = 165
    for i in range(len(raw)):
        raw[i] = ((raw[i] ^ prev) + i % 256) & 0xFF
        prev = raw[i]
    prefix = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    if not mac_machine_id:
        return f"{prefix}{machine_id}"
    return f"{prefix}{machine_id}/{mac_machine_id}"


def build_headers(
    creds: Credentials,
    client_key: str,
    session_id: str,
    client_type: str = "sand",
    config_version: str = "",
) -> dict[str, str]:
    # 与 IDE 抓包对齐（extensionHostProcess.js 的 JDe 实际发出的头）：
    # layout=unifiedAgent、ghost-mode=true、带 timezone / config-version，
    # 不发 x-cursor-streaming（IDE 不发）。
    headers = {
        "content-type": "application/connect+proto",
        "connect-protocol-version": "1",
        "authorization": f"Bearer {creds.access_token}",
        "x-cursor-checksum": cursor_checksum(creds.machine_id, creds.mac_machine_id),
        "x-cursor-client-version": CLIENT_VERSION,
        "x-cursor-client-type": client_type,
        "x-cursor-client-layout": "unifiedAgent",
        "x-cursor-client-device-type": "desktop",
        "x-cursor-client-os": "darwin" if sys.platform == "darwin" else sys.platform,
        "x-cursor-client-arch": "arm64",
        "x-client-key": client_key,
        "x-session-id": session_id,
        "x-ghost-mode": "true",
        "x-new-onboarding-completed": "false",
        "x-request-id": str(uuid.uuid4()),
        "user-agent": "connect-es/1.6.1",
    }
    if config_version:
        headers["x-cursor-config-version"] = config_version
    return headers


# ---------------------------------------------------------------------------
# Connect 流式分帧：1 字节 flag + 4 字节大端长度 + payload
# ---------------------------------------------------------------------------

FLAG_MESSAGE = 0x00
FLAG_END_STREAM = 0x02


def frame(payload: bytes, flag: int = FLAG_MESSAGE) -> bytes:
    return struct.pack(">BI", flag, len(payload)) + payload


def deframe(stream: Iterator[bytes]) -> Iterator[tuple[int, bytes]]:
    buf = bytearray()
    for chunk in stream:
        buf.extend(chunk)
        while len(buf) >= 5:
            flag = buf[0]
            (length,) = struct.unpack(">I", bytes(buf[1:5]))
            if len(buf) < 5 + length:
                break
            payload = bytes(buf[5 : 5 + length])
            del buf[: 5 + length]
            yield flag, payload


# ---------------------------------------------------------------------------
# 消息构造
# ---------------------------------------------------------------------------


def requested_model(
    model_id: str,
    max_mode: bool = True,
    parameters: Optional[dict[str, str]] = None,
) -> bytes:
    body = pb_str(1, model_id) + pb_bool(2, max_mode)
    # parameters(3) repeated InferenceModelParameterValue{id(1),value(2)}，
    # reasoning effort 就通过 id="effort" 传（对齐 direct stream 的 r.get("effort")）。
    for pid, value in (parameters or {}).items():
        body += pb_bytes(3, pb_str(1, pid) + pb_str(2, value))
    body += pb_bool(4, True)
    return body


def run_request(conversation_id: str, model_id: str) -> bytes:
    body = pb_str(1, conversation_id) + pb_bytes(3, requested_model(model_id))
    return pb_bytes(1, body)


def core_message(role: int, text: str) -> bytes:
    return pb_enum(1, role) + pb_str(2, text)


def inference_stream_request(
    model_id: str,
    prompt: str,
    system: str,
    conversation_id: str,
    parameters: Optional[dict[str, str]] = None,
) -> bytes:
    """InferenceStreamRequest —— InferenceService/Stream 的顶层单请求。

    字段编号取自 657.js：messages(1) / model_id(5) / requested_model(7) /
    conversation_id(8)。这是 IDE sand 走的那条 ServerStreaming 方法，
    不是 RunInference（后者对 sand 直接拒）。
    """
    return (
        pb_bytes(1, core_message(ROLE_SYSTEM, system))
        + pb_bytes(1, core_message(ROLE_USER, prompt))
        + pb_str(5, model_id)
        + pb_bytes(7, requested_model(model_id, parameters=parameters))
        + pb_str(8, conversation_id)
    )


def inference_stream_request_messages(
    model_id: str,
    messages: "list[tuple[int, str]]",
    conversation_id: str,
    parameters: Optional[dict[str, str]] = None,
) -> bytes:
    """多轮版 InferenceStreamRequest：messages 为 [(role_int, text), …] 有序对话。"""
    body = b"".join(pb_bytes(1, core_message(role, text)) for role, text in messages)
    body += pb_str(5, model_id)
    body += pb_bytes(7, requested_model(model_id, parameters=parameters))
    body += pb_str(8, conversation_id)
    return body


# ---------------------------------------------------------------------------
# google.protobuf.Struct / Value 编码（工具 schema、tool_call 参数、tool 结果）
# ---------------------------------------------------------------------------


def pb_double(field_no: int, value: float) -> bytes:
    return tag(field_no, 1) + struct.pack("<d", float(value))


def pb_value(v: Any) -> bytes:
    """google.protobuf.Value 的 body（oneof kind）。"""
    if v is None:
        return pb_enum(1, 0)  # null_value
    if isinstance(v, bool):  # 必须在 int 之前判断
        return pb_bool(4, v)  # bool_value
    if isinstance(v, (int, float)):
        return pb_double(2, float(v))  # number_value
    if isinstance(v, str):
        return pb_str(3, v)  # string_value
    if isinstance(v, dict):
        return pb_bytes(5, pb_struct(v))  # struct_value
    if isinstance(v, (list, tuple)):
        return pb_bytes(6, b"".join(pb_bytes(1, pb_value(x)) for x in v))  # list_value
    return pb_str(3, str(v))


def pb_struct(d: "dict[str, Any]") -> bytes:
    """google.protobuf.Struct 的 body：fields = map<string, Value>（field 1）。"""
    out = b""
    for key, val in d.items():
        out += pb_bytes(1, pb_str(1, str(key)) + pb_bytes(2, pb_value(val)))
    return out


# ---------------------------------------------------------------------------
# 消息 / 工具 / 多模态构造原语（字段号取自 657.js）
# ---------------------------------------------------------------------------


def agent_tool(name: str, description: str, parameters: Optional[dict]) -> bytes:
    """InferenceAgentTool：name(1) description(2) parameters(3=Struct)。

    IDE/Box 的 agentToolToProto 是 JSON.stringify(AI SDK Schema 对象)，Struct 里
    实际是 {"jsonSchema": <JSON Schema>}。后端只认这个壳：裸 schema 会让 Claude
    适配层拿不到 input_schema 直接 400，grok/gemini 则看不到参数从不调工具。
    """
    schema = parameters or {"type": "object"}
    if not (isinstance(schema, dict) and set(schema) == {"jsonSchema"}):
        schema = {"jsonSchema": schema}
    body = pb_str(1, name) + pb_str(2, description or "")
    body += pb_bytes(3, pb_struct(schema))
    return body


def content_part_text(text: str) -> bytes:
    """InferenceContentPart{ text = InferenceTextPart{ text(1) } }（field 1）。"""
    return pb_bytes(1, pb_str(1, text))


def content_part_image(data_b64: str, mime_type: str) -> bytes:
    """InferenceContentPart{ image = InferenceImagePart{ data(1), mime_type(2) } }（field 2）。"""
    return pb_bytes(2, pb_str(1, data_b64) + pb_str(2, mime_type or "image/png"))


def content_part_file(data_b64: str, media_type: str, filename: str = "") -> bytes:
    """InferenceContentPart{ file = InferenceFilePart{ data(1), media_type(2), filename(3) } }（field 3）。"""
    inner = pb_str(1, data_b64) + pb_str(2, media_type or "application/octet-stream")
    if filename:
        inner += pb_str(3, filename)
    return pb_bytes(3, inner)


def message_parts(role: int, parts: "list[bytes]") -> bytes:
    """InferenceCoreMessage：role(1) + parts(3 = InferenceContentParts{ parts(1) repeated })。"""
    content_parts = b"".join(pb_bytes(1, p) for p in parts)
    return pb_enum(1, role) + pb_bytes(3, content_parts)


def tool_call_pb(tool_call_id: str, tool_name: str, args: Any) -> bytes:
    """InferenceToolCall：tool_call_id(1) tool_name(2) args(3=Struct)。"""
    body = pb_str(1, tool_call_id) + pb_str(2, tool_name)
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {"_raw": args}
    body += pb_bytes(3, pb_struct(args or {}))
    return body


def message_assistant_tools(text: str, tool_calls: "list[bytes]") -> bytes:
    """assistant 消息：role(1)=ASSISTANT，可选 text(2)，tool_calls(4) repeated。"""
    body = pb_enum(1, ROLE_ASSISTANT)
    if text:
        body += pb_str(2, text)
    for tc in tool_calls:
        body += pb_bytes(4, tc)
    return body


def tool_result_part(
    tool_call_id: str, tool_name: str, result: Any, is_error: bool = False
) -> bytes:
    """InferenceToolResultPart：tool_call_id(1) tool_name(2) result(3=Value) is_error(4)。"""
    body = pb_str(1, tool_call_id) + pb_str(2, tool_name or "")
    body += pb_bytes(3, pb_value(result))
    if is_error:
        body += pb_bool(4, True)
    return body


def message_tool_result(parts: "list[bytes]") -> bytes:
    """role(1)=TOOL + tool_content(6 = InferenceToolResultContent{ parts(1) repeated })。"""
    content = b"".join(pb_bytes(1, p) for p in parts)
    return pb_enum(1, ROLE_TOOL) + pb_bytes(6, content)


def inference_stream_request_full(
    model_id: str,
    messages_pb: "list[bytes]",
    tools_pb: "list[bytes]",
    conversation_id: str,
    parameters: Optional[dict[str, str]] = None,
    max_tokens: Optional[int] = None,
    max_mode: bool = True,
) -> bytes:
    """完整 InferenceStreamRequest：预构造的 messages(1) + tools(2) + model/requested/conv。"""
    body = b"".join(pb_bytes(1, m) for m in messages_pb)
    body += b"".join(pb_bytes(2, t) for t in tools_pb)
    # model_config(4) = InferenceModelConfig{ max_tokens(1) }
    if max_tokens and max_tokens > 0:
        body += pb_bytes(4, pb_enum(1, int(max_tokens)))
    body += pb_str(5, model_id)
    body += pb_bytes(7, requested_model(model_id, max_mode=max_mode, parameters=parameters))
    body += pb_str(8, conversation_id)
    return body


def invoke_model(invocation_id: str, prompt: str, system: str) -> bytes:
    """只带 messages。

    requested_model / model_id / conversation_id / invocation_id 都是 run 级字段，
    由 run_request 固定；在这里重复设置会被服务端以 invalid_argument 拒绝。
    """
    stream_request = pb_bytes(1, core_message(ROLE_SYSTEM, system)) + pb_bytes(
        1, core_message(ROLE_USER, prompt)
    )
    body = pb_str(1, invocation_id) + pb_bytes(2, stream_request)
    return pb_bytes(2, body)


def finish_run() -> bytes:
    return pb_bytes(4, b"")


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    ready: bool = False
    resolved_model: str = ""
    text: str = ""
    thinking: str = ""
    errors: list[str] = field(default_factory=list)
    ended: bool = False
    heartbeats: int = 0
    phase: Optional[str] = None  # 当前正在输出 thinking 还是 text，用于分段标题


_DIM = "\033[2m"
_RESET = "\033[0m"


def _emit(out: Outcome, phase: str, chunk: str) -> None:
    """流式打印，thinking / text 切换时打一次分段标题；thinking 用暗色。"""
    if not chunk:
        return
    if out.phase != phase:
        title = "思考" if phase == "thinking" else "回答"
        print(f"\n{_DIM}===== {title} ====={_RESET}")
        out.phase = phase
    if phase == "thinking":
        out.thinking += chunk
        print(f"{_DIM}{chunk}{_RESET}", end="", flush=True)
    else:
        out.text += chunk
        print(chunk, end="", flush=True)


def handle_stream_response(payload: bytes, out: Outcome, verbose: bool) -> None:
    parsed = pb_parse(payload)
    # 先 thinking 再 text：reasoning 帧通常先于正文到达。
    if 9 in parsed:  # thinking_part -> InferenceThinkingStreamPart.text(1)
        _emit(out, "thinking", as_text(first(pb_parse(first(parsed, 9)), 1)))
    if 1 in parsed:  # text_part -> InferenceTextStreamPart.text(1)
        _emit(out, "text", as_text(first(pb_parse(first(parsed, 1)), 1)))
    if 8 in parsed:  # error
        err = pb_parse(first(parsed, 8))
        message, code = as_text(first(err, 1)), as_text(first(err, 2))
        out.errors.append(f"{code or 'unknown'}: {message}")
    if 4 in parsed and verbose:
        info = first(parsed, 4)
        if isinstance(info, (bytes, bytearray)):
            print("\n[response_info]", bytes(info[:80]).hex(), file=sys.stderr)


def handle_server_message(payload: bytes, out: Outcome, verbose: bool) -> None:
    parsed = pb_parse(payload)
    if 1 in parsed:
        out.heartbeats += 1
    if 2 in parsed:  # run_ready
        ready = pb_parse(first(parsed, 2))
        out.ready = True
        model = first(ready, 1)
        if model:
            out.resolved_model = as_text(first(pb_parse(model), 1))
        display = first(ready, 3)
        if display:
            out.resolved_model = as_text(display) or out.resolved_model
        if verbose:
            print(f"[run_ready] resolved={out.resolved_model}", file=sys.stderr)
    if 3 in parsed:  # invocation_response
        inner = pb_parse(first(parsed, 3))
        response = first(inner, 2)
        if response:
            handle_stream_response(response, out, verbose)
    if 4 in parsed:  # invocation_end
        out.ended = True


# ---------------------------------------------------------------------------


RPC_STREAM = "/aiserver.v1.InferenceService/Stream"
RPC_RUNINFERENCE = "/aiserver.v1.InferenceService/RunInference"


GATEWAY_CODES = {500, 502, 503, 504}


def probe(
    model: str,
    prompt: str,
    system: str,
    verbose: bool,
    client_type: str = "sand",
    backend: str = BACKEND,
    method: str = "stream",
    config_version: str = "",
    effort: str = "",
    max_retries: int = 2,
    use_cursor: bool = True,
    ttfb_timeout: float = 0.0,
) -> int:
    creds = load_credentials(use_cursor)
    conversation_id = str(uuid.uuid4())
    client_key = uuid.uuid4().hex
    rpc_path = RPC_STREAM if method == "stream" else RPC_RUNINFERENCE
    parameters = {"effort": effort} if effort else None

    print(f"账号     : {creds.email or '(未知)'}")
    print(f"token来源 : {creds.source}")
    print(f"端点     : {backend}{rpc_path}")
    print(f"client-type: {client_type}")
    print(f"模型     : {model}{'  effort=' + effort if effort else ''}")
    print("-" * 60)

    if method == "stream":
        # ServerStreaming：单条 enveloped 请求，服务端流式返回 InferenceStreamResponse。
        body = frame(
            inference_stream_request(
                model, prompt, system, conversation_id, parameters
            )
        )
    else:
        # BiDi 对照：建流 → 投喂 → 关流（sand 会被网关拒）。
        invocation_id = str(uuid.uuid4())
        body = (
            frame(run_request(conversation_id, model))
            + frame(invoke_model(invocation_id, prompt, system))
            + frame(finish_run())
        )

    out = Outcome()
    started = time.time()
    for attempt in range(max_retries + 1):
        # 每次尝试刷新 request-id 与 checksum 时间戳；一旦开始收正文就不再重试。
        headers = build_headers(
            creds, client_key, str(uuid.uuid4()), client_type, config_version
        )
        out = Outcome()
        # 首帧软超时：read 超时即"迟迟没吐第一个字"，触发 ReadTimeout → 下方重发。
        # 首帧到达后生成极快（帧间隔远小于该值），基本不会误杀生成中的流。
        read_timeout = ttfb_timeout if ttfb_timeout > 0 else 120.0
        try:
            with httpx.Client(
                http2=True,
                timeout=httpx.Timeout(120.0, connect=20.0, read=read_timeout),
            ) as client:
                with client.stream(
                    "POST", f"{backend}{rpc_path}", content=body, headers=headers
                ) as response:
                    tag = f"  (第{attempt + 1}次)" if attempt else ""
                    print(
                        f"HTTP     : {response.http_version}"
                        f"  status={response.status_code}{tag}"
                    )
                    if verbose:
                        for key, value in response.headers.items():
                            print(f"  < {key}: {value}", file=sys.stderr)
                    if response.status_code != 200:
                        detail = response.read().decode("utf-8", "replace")[:600]
                        if (
                            response.status_code in GATEWAY_CODES
                            and attempt < max_retries
                        ):
                            wait = 1.5 * (attempt + 1)
                            print(f"网关 {response.status_code}，{wait:.0f}s 后重试…")
                            time.sleep(wait)
                            continue
                        print(f"\n请求被拒：{detail}")
                        return 1
                    print("-" * 60)
                    first_byte_at = first_text_at = None
                    for flag, payload in deframe(response.iter_bytes()):
                        if first_byte_at is None:
                            first_byte_at = time.time()
                        if flag & FLAG_END_STREAM:
                            trailer = payload.decode("utf-8", "replace").strip()
                            if trailer and trailer != "{}":
                                print(f"\n[trailer] {trailer}")
                            break
                        if method == "stream":
                            handle_stream_response(payload, out, verbose)
                        else:
                            handle_server_message(payload, out, verbose)
                        if first_text_at is None and (out.text or out.thinking):
                            first_text_at = time.time()
                    if verbose:
                        fb = (first_byte_at - started) if first_byte_at else -1
                        ft = (first_text_at - started) if first_text_at else -1
                        print(
                            f"\n[timing] 首帧(TTFB) {fb:.1f}s  "
                            f"首字/思考(TTFT) {ft:.1f}s  总 {time.time() - started:.1f}s",
                            file=sys.stderr,
                        )
            break
        except httpx.HTTPError as exc:
            # 已经开始输出正文/思考再重发会导致重复，此时直接抛出不重试。
            if out.text or out.thinking:
                raise
            if attempt < max_retries:
                is_ttfb = ttfb_timeout > 0 and isinstance(exc, httpx.ReadTimeout)
                reason = (
                    f"首帧超过 {ttfb_timeout:.0f}s 未到"
                    if is_ttfb
                    else f"网络错误 {type(exc).__name__}: {exc}"
                )
                wait = 1.5 * (attempt + 1)
                print(f"{reason}，{wait:.0f}s 后重发…")
                time.sleep(wait)
                continue
            raise

    elapsed = time.time() - started
    print("\n" + "-" * 60)
    if method != "stream":
        print(f"run_ready   : {out.ready}  resolved={out.resolved_model or '-'}")
    print(f"正文字符    : {len(out.text)}")
    print(f"思考字符    : {len(out.thinking)}")
    print(f"耗时        : {elapsed:.1f}s")
    if out.errors:
        print("错误        :")
        for err in out.errors:
            print(f"  - {err}")
        return 1
    if not out.text.strip():
        print("\n结论：连接建立但没有正文，需要进一步排查。")
        return 1
    print(f"\n结论：脱离 IDE，以 client-type={client_type} 直连 {method} 成功。")
    return 0


def _load_config_version() -> str:
    """从本地 serverConfig 缓存取 config_version，对齐 IDE 的 x-cursor-config-version。"""
    try:
        row = _read_vscdb(("cursorai/serverConfig",)).get("cursorai/serverConfig")
        if row:
            return str(json.loads(row).get("configVersion") or "")
    except Exception:
        pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Cursor sand 推理直连探针")
    parser.add_argument("-m", "--model", default="claude-opus-5", help="模型 id")
    parser.add_argument(
        "-p", "--prompt", default="用一句话回答：1+1 等于几？", help="用户消息"
    )
    parser.add_argument(
        "-s",
        "--system",
        default="You are a helpful assistant. Answer concisely.",
        help="system 消息",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印响应头等细节")
    parser.add_argument(
        "-c",
        "--client-type",
        default="sand",
        help="x-cursor-client-type，默认 sand（Stream 方法接受 sand）",
    )
    parser.add_argument(
        "--method",
        choices=["stream", "runinference"],
        default="stream",
        help="stream=IDE 走的 ServerStreaming（默认）；runinference=BiDi 对照",
    )
    parser.add_argument(
        "--backend", default=BACKEND, help="推理后端，默认 api2.cursor.sh"
    )
    parser.add_argument(
        "--config-version",
        default="auto",
        help="x-cursor-config-version，auto=读本地缓存，空串=不发",
    )
    parser.add_argument(
        "-e",
        "--effort",
        default="",
        help="reasoning effort（如 low/medium/high），空=不带、由服务端定",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="网关 5xx(500/502/503/504) 与首帧超时的自动重发次数，默认 2",
    )
    parser.add_argument(
        "--ttfb-timeout",
        type=float,
        default=0.0,
        help="首帧软超时(秒)：迟迟不吐第一个字就断开重发，0=关闭。缓解服务端排队",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="网页授权登录，把 access/refresh token 写入独立配置（无需 Cursor）",
    )
    parser.add_argument(
        "--login-with-key",
        metavar="CURSOR_API_KEY",
        default="",
        help="用 Cursor API key(crsr_…) 直接换 token，无浏览器；并存 key 供自动重换",
    )
    parser.add_argument(
        "--import-from-cursor",
        action="store_true",
        help="把 Cursor 当前 token 导入独立配置，之后可 --no-cursor 独立运行",
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        help="不读 Cursor 目录，只用环境变量 / 独立配置（token 需自备）",
    )
    args = parser.parse_args()
    if args.login:
        return cmd_login(args.backend)
    if args.login_with_key:
        return cmd_login_with_key(args.login_with_key, args.backend)
    if args.import_from_cursor:
        return import_from_cursor()
    # config_version 仅在读 Cursor 时自动取；纯独立模式默认不发。
    if args.config_version == "auto":
        config_version = "" if args.no_cursor else _load_config_version()
    else:
        config_version = args.config_version
    try:
        return probe(
            args.model,
            args.prompt,
            args.system,
            args.verbose,
            args.client_type,
            args.backend,
            args.method,
            config_version,
            args.effort,
            args.max_retries,
            not args.no_cursor,
            args.ttfb_timeout,
        )
    except httpx.HTTPError as exc:
        print(f"\n网络层失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
