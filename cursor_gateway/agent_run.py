"""account 模式：官方 3.19 AgentService/Run（HTTP/2 双向流）。

调用方的 function tools 以 MCP 工具声明给 Agent（AgentRunRequest.mcp_tools +
RequestContext.tools）。服务端要调工具时下发 ExecServerMessage.mcp_args，这里
不在 Box 内执行，而是转成 tool_call 事件回给调用方并结束本轮；调用方把结果作为
tool 消息发回来时，按 ConversationHistory 的 assistant.tool_call / tool 消息回填，
再开一轮 run 让模型继续。整个过程无状态，网关重启不影响。
"""

from __future__ import annotations

import base64
import json
import queue
import socket
import ssl
import struct
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Iterator, Optional

import h2.connection
import h2.events

from .models import DEFAULT_AGENT_HOST
from .paths import ensure_sys_path

ensure_sys_path()

import probe_runinference as P  # noqa: E402

from .credentials import UpstreamError  # noqa: E402

AGENT_PATH = "/agent.v1.AgentService/Run"
AGENT_CLIENT_VERSION = "3.19.13"
# agent.v1.AgentMode：1=AGENT 2=ASK 3=PLAN。Ask 会禁止一切写操作，模型直接拒绝
# 调用方的 Write/Edit 类工具，所以固定用 AGENT。
AGENT_MODE_AGENT = 1
AGENT_MODE_ASK = 2
AGENT_MODE = AGENT_MODE_AGENT
# 默认不声明工作区：API 调用方没有本地目录，声明了模型就会拿这个假路径去调
# 调用方的 Glob/Read。旧节点里存的占位路径同样视为"没有工作区"。
DEFAULT_WORKSPACE = ""
LEGACY_PLACEHOLDER_WORKSPACE = "/tmp/sand-account"
# 调用方工具在 Agent 侧的 MCP provider 标识；exec 回来时据此识别
MCP_PROVIDER = "client-tools"
# 请求以 tool 结果收尾时，本轮 run 仍需要一条 user 消息
CONTINUE_TEXT = (
    "上面 <conversation_history> 里最后的工具调用已经返回结果。"
    "请直接基于这些结果继续完成任务并回答；不要用相同参数重复调用同一工具。"
)
HISTORY_PREFACE = "以下是本次对话此前的记录（含工具调用与结果），请直接延续："
IMAGE_NOTE = (
    "注意：这次对话中出现过的全部图片（包括此前消息里的）都已作为附件附在本消息中，"
    "按出现顺序编号为 [image N]，文本里的 [image N] 就对应这些附件。"
    "它们此刻就在你眼前，请直接查看后回答；不要用工具去查找图片文件，也不要回答看不到图。"
)
# 附图上限：超出时丢最早的，文本里标 [image 已省略]
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 收到第一个 mcp exec 后再等一小段，把同一步的并行工具调用收齐再断流
TOOL_GRACE_S = 0.6

_tls = threading.local()


def pb_u32(field_no: int, value: int) -> bytes:
    return P.tag(field_no, 0) + P._varint(int(value))


def pb_u64(field_no: int, value: int) -> bytes:
    return P.tag(field_no, 0) + P._varint(int(value))


def _first_int(parsed: dict, no: int, default: int = 0) -> int:
    v = P.first(parsed, no)
    return v if isinstance(v, int) else default


def _requested_model(model_id: str) -> bytes:
    return P.pb_str(1, model_id) + P.pb_bool(2, True) + P.pb_bool(7, True)


def _conv_state() -> bytes:
    now = int(time.time() * 1000)
    return P.pb_enum(10, AGENT_MODE) + pb_u64(26, now) + P.pb_str(27, "Asia/Shanghai")


def _workspace() -> str:
    ws = (getattr(_tls, "workspace", None) or DEFAULT_WORKSPACE).strip()
    return "" if ws == LEGACY_PLACEHOLDER_WORKSPACE else ws


def _req_env() -> bytes:
    workspace = _workspace()
    body = P.pb_str(1, "darwin") + P.pb_str(3, "/bin/zsh") + P.pb_bool(5, False)
    body += P.pb_str(10, "Asia/Shanghai")
    if workspace:
        # workspace_paths(2) / project_folder(11) / process_working_directory(21)
        body += P.pb_str(2, workspace) + P.pb_str(11, workspace) + P.pb_str(21, workspace)
    return body


def _tool_defs() -> list[bytes]:
    return list(getattr(_tls, "tool_defs", None) or [])


def _request_context() -> bytes:
    body = P.pb_bytes(4, _req_env())
    # RequestContext.tools(7)：IDE 每次也把可用 MCP 工具放在这里
    for d in _tool_defs():
        body += P.pb_bytes(7, d)
    for f in (33, 36, 39, 40, 41, 42, 43, 44, 45):
        body += P.pb_bool(f, True)
    return body


def _mcp_tool_defs(tools: Optional[list[dict]]) -> list[bytes]:
    """OpenAI/Anthropic 工具 → agent.v1.McpToolDefinition 列表。"""
    out: list[bytes] = []
    for t in tools or []:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        schema = t.get("parameters") or {"type": "object"}
        if isinstance(schema, dict) and set(schema) == {"jsonSchema"}:
            schema = schema["jsonSchema"]
        if not isinstance(schema, dict):
            schema = {"type": "object"}
        body = P.pb_str(1, name) + P.pb_str(2, str(t.get("description") or ""))
        body += P.pb_bytes(3, P.pb_value(schema))
        body += P.pb_str(4, MCP_PROVIDER) + P.pb_str(5, name)
        body += P.pb_str(6, json.dumps(schema, ensure_ascii=False))
        out.append(body)
    return out


def _text_item(text: str) -> bytes:
    """ConversationHistory{User,Assistant,ToolResult}Content 的 text 分支。"""
    return P.pb_bytes(1, P.pb_str(1, text))


def _msg_text(msg: Any) -> str:
    return "".join(p.text for p in msg.parts if p.kind == "text" and p.text)


def _image_bytes(part: Any) -> tuple[bytes, str]:
    """image Part（data 为 base64）→ (原始字节, mime)。解不出来返回空字节。"""
    raw = (getattr(part, "data", "") or "").strip()
    if not raw:
        return b"", ""
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
    except Exception:  # noqa: BLE001
        return b"", ""
    return data, (getattr(part, "mime", "") or "image/png")


def _select_images(
    dialog: list[Any], current: Optional[Any]
) -> tuple[list[tuple[bytes, str]], dict[int, int], set[int]]:
    """按时间顺序编号可附带的图片。

    返回 (附图列表, id(part)→编号, 被省略的 id(part))。超出 MAX_IMAGES /
    MAX_IMAGE_BYTES 时优先保留最近的（当前消息里的最优先）。
    """
    ordered: list[tuple[Any, bytes, str]] = []
    for m in [*dialog, *([current] if current is not None else [])]:
        if m.role != "user":
            continue
        for p in m.parts:
            if p.kind != "image":
                continue
            data, mime = _image_bytes(p)
            ordered.append((p, data, mime))
    keep: list[tuple[Any, bytes, str]] = []
    total = 0
    for p, data, mime in reversed(ordered):
        if not data or len(keep) >= MAX_IMAGES or total + len(data) > MAX_IMAGE_BYTES:
            continue
        keep.append((p, data, mime))
        total += len(data)
    keep.reverse()
    index = {id(p): n for n, (p, _, _) in enumerate(keep, start=1)}
    dropped = {id(p) for p, _, _ in ordered if id(p) not in index}
    return [(data, mime) for _, data, mime in keep], index, dropped


def _user_text_with_images(msg: Any, index: dict[int, int]) -> str:
    """用户消息文本；图片位置放 [image N] 占位。"""
    parts: list[str] = []
    for p in msg.parts:
        if p.kind == "text" and p.text:
            parts.append(p.text)
        elif p.kind == "image":
            n = index.get(id(p))
            parts.append(f"[image {n}]" if n else "[image 已省略]")
    return "\n".join(parts)


def _fold_history(dialog: list[Any], call_names: dict[str, str], image_index: dict[int, int]) -> str:
    """把此前对话（含工具调用/结果）折成文本。

    实测 AgentService/Run 不把 UserMessageAction.conversation_history 喂给模型
    （纯多轮也答"不知道"），所以历史必须进 user 文本；结构化 history 照发不误。
    """
    lines = ["<conversation_history>"]
    for m in dialog:
        if m.role == "user":
            t = _user_text_with_images(m, image_index)
            if t.strip():
                lines += ["[user]", t]
        elif m.role == "assistant":
            t = _msg_text(m)
            if t.strip():
                lines += ["[assistant]", t]
            for p in m.parts:
                if p.kind == "tool_call":
                    lines.append(f"[tool_call {p.name} id={p.id}] {p.arguments or '{}'}")
        for p in m.parts:
            if p.kind != "tool_result":
                continue
            name = p.tool_name or call_names.get(p.tool_call_id, "")
            flag = " error" if p.is_error else ""
            lines += [f"[tool_result {name} id={p.tool_call_id}{flag}]", p.content or ""]
    lines.append("</conversation_history>")
    return "\n".join(lines)


def _has_user_content(msg: Any) -> bool:
    return any((p.kind == "text" and p.text.strip()) or p.kind == "image" for p in msg.parts)


def _history_from_msgs(msgs: list[Any]) -> tuple[str, bytes, list[tuple[bytes, str]]]:
    """内部 Msg 列表 → (本轮 user 文本, ConversationHistory 字节, 附图列表)。

    system 并进本轮 user 文本（服务端会把 custom_system_prompt 当 CLI 参数）。
    此前对话折成文本放在 user 文本前面；最后一条不是 user 消息时（以 tool 结果收尾），
    用 CONTINUE_TEXT 顶上。图片（当前的和历史里的）都作为本轮附图带上，文本里留编号。
    """
    systems: list[str] = []
    dialog: list[Any] = []
    for m in msgs:
        if m.role == "system":
            t = _msg_text(m)
            if t.strip():
                systems.append(t)
        else:
            dialog.append(m)
    call_names = {
        p.id: p.name for m in dialog for p in m.parts if p.kind == "tool_call" and p.id
    }

    current: Optional[Any] = None
    if dialog and dialog[-1].role == "user" and _has_user_content(dialog[-1]):
        current = dialog[-1]
        dialog = dialog[:-1]
        tail = [p for p in current.parts if p.kind == "tool_result"]
        if tail:
            dialog.append(SimpleNamespace(role="tool", parts=tail))

    images, image_index, _dropped = _select_images(dialog, current)

    if current is not None:
        current_user = _user_text_with_images(current, image_index)
        if not _msg_text(current).strip():
            current_user = current_user + "\n请看附图。"
    elif dialog:
        current_user = CONTINUE_TEXT
    else:
        current_user = systems.pop() if systems else "你好"

    hist = b""
    for m in dialog:
        role = m.role
        tool_results = [p for p in m.parts if p.kind == "tool_result"]
        if role == "user":
            items = b"".join(
                P.pb_bytes(1, _text_item(p.text)) for p in m.parts if p.kind == "text" and p.text
            )
            if items:
                hist += P.pb_bytes(1, P.pb_bytes(1, items))
        elif role == "assistant":
            items = b""
            for p in m.parts:
                if p.kind == "text" and p.text:
                    items += P.pb_bytes(1, _text_item(p.text))
                elif p.kind == "tool_call":
                    call = P.pb_str(1, _restore_call_id(p.id)) + P.pb_str(2, p.name or "")
                    call += P.pb_str(3, p.arguments or "{}")
                    # content(1) → AssistantContent.tool_call(4)
                    items += P.pb_bytes(1, P.pb_bytes(4, call))
            if items:
                hist += P.pb_bytes(1, P.pb_bytes(2, items))
        for p in tool_results:
            body = P.pb_str(1, _restore_call_id(p.tool_call_id))
            body += P.pb_str(2, p.tool_name or call_names.get(p.tool_call_id, ""))
            body += P.pb_bytes(3, _text_item(p.content or ""))
            if p.is_error:
                body += P.pb_bool(4, True)
            hist += P.pb_bytes(1, P.pb_bytes(3, body))

    if images:
        # 放在问题紧前面：模型对结尾更敏感，不然容易忽略附图去找"文件"
        current_user = IMAGE_NOTE + "\n\n" + current_user
    if dialog:
        current_user = (
            HISTORY_PREFACE + "\n" + _fold_history(dialog, call_names, image_index) + "\n\n" + current_user
        )
    if systems:
        current_user = "\n\n".join(systems) + "\n\n" + current_user
    return current_user, hist, images


def _export_call_id(raw: str) -> str:
    """服务端的 tool_call_id 形如 'call-…-0\\nfc_…'，换行对外换成 %0A（可逆）。"""
    return (raw or "").strip().replace("\n", "%0A")


def _restore_call_id(ext: str) -> str:
    return (ext or "").replace("%0A", "\n")


def _value_py(parsed: dict) -> Any:
    """google.protobuf.Value → Python。"""
    if 3 in parsed:
        return P.as_text(P.first(parsed, 3))
    if 2 in parsed:
        raw = P.first(parsed, 2)
        if isinstance(raw, (bytes, bytearray)) and len(raw) == 8:
            num = struct.unpack("<d", bytes(raw))[0]
            return int(num) if float(num).is_integer() and abs(num) < 2**53 else num
        return 0
    if 4 in parsed:
        return bool(P.first(parsed, 4))
    if 5 in parsed:
        return _struct_py(P.pb_parse(P.first(parsed, 5)))
    if 6 in parsed:
        lst = P.pb_parse(P.first(parsed, 6))
        return [_value_py(P.pb_parse(v)) for _, v in lst.get(1, [])]
    return None


def _struct_py(parsed: dict, entry_field: int = 1) -> dict[str, Any]:
    """Struct.fields / map<string, Value>：entry{key(1) value(2)}。"""
    out: dict[str, Any] = {}
    for _, entry in parsed.get(entry_field, []):
        e = P.pb_parse(entry)
        key = P.as_text(P.first(e, 1))
        out[key] = _value_py(P.pb_parse(P.first(e, 2)))
    return out


def _mcp_exec_event(args: dict, index: int) -> dict[str, Any]:
    """ExecServerMessage.mcp_args → 对外 tool_call 事件（单帧 complete）。"""
    name = P.as_text(P.first(args, 5)) or P.as_text(P.first(args, 1))
    call_id = _export_call_id(P.as_text(P.first(args, 3))) or ("call_" + uuid.uuid4().hex[:24])
    payload = _struct_py(args, entry_field=2)
    return {
        "type": "tool_call",
        "id": call_id,
        "name": name,
        "args": json.dumps(payload, ensure_ascii=False),
        "complete": True,
        "index": index,
    }


def _selected_context(images: list[tuple[bytes, str]]) -> bytes:
    """SelectedContext.selected_images(1)：SelectedImage{uuid(2) mime_type(7) data(8)}。
    服务端拿 data 自己算 blob id / 识别 MIME，不需要尺寸。"""
    body = b""
    for data, mime in images:
        img = P.pb_str(2, str(uuid.uuid4())) + P.pb_str(7, mime or "image/png") + P.pb_bytes(8, data)
        body += P.pb_bytes(1, img)
    return body


def _user_message(text: str, images: Optional[list[tuple[bytes, str]]] = None) -> bytes:
    body = P.pb_str(1, text) + P.pb_str(2, str(uuid.uuid4()))
    if images:
        body += P.pb_bytes(3, _selected_context(images))
    body += P.pb_enum(4, AGENT_MODE)
    return body


def _hist_text(text: str) -> bytes:
    return P.pb_bytes(1, P.pb_str(1, text))


def _conversation_history(msgs: list[tuple[str, str]]) -> bytes:
    out = b""
    for role, text in msgs:
        if not text:
            continue
        content = P.pb_bytes(1, _hist_text(text))
        msg = P.pb_bytes(2, content) if role == "assistant" else P.pb_bytes(1, content)
        out += P.pb_bytes(1, msg)
    return out


def _format_prior(prior: list[tuple[str, str]]) -> str:
    if not prior:
        return ""
    lines = []
    for role, text in prior:
        label = "用户" if role == "user" else "助手"
        lines.append(f"{label}：{text}")
    return "此前对话：\n" + "\n".join(lines) + "\n\n"


def decode_messages_pb(messages_pb: list[bytes]) -> list[tuple[str, str]]:
    roles = {1: "user", 2: "assistant", 3: "tool", 4: "system"}
    out: list[tuple[str, str]] = []
    for raw in messages_pb:
        parsed = P.pb_parse(raw)
        role = roles.get(_first_int(parsed, 1), "user")
        text = P.as_text(P.first(parsed, 2))
        if text:
            out.append((role, text))
    return out


def _split_turn(messages_pb: list[bytes]) -> tuple[str, list[tuple[str, str]]]:
    msgs = decode_messages_pb(messages_pb)
    systems = [t for r, t in msgs if r == "system"]
    dialog = [(r, t) for r, t in msgs if r in ("user", "assistant")]
    if dialog and dialog[-1][0] == "user":
        user_text = dialog[-1][1]
        prior = dialog[:-1]
    elif dialog:
        user_text = "请继续。"
        prior = dialog
    else:
        user_text = systems[-1] if systems else "你好"
        prior = []
        systems = systems[:-1] if systems else []
    if prior:
        user_text = _format_prior(prior) + user_text
    if systems:
        user_text = "\n\n".join(systems) + "\n\n" + user_text
    return user_text, prior


def build_run_request(
    model: str,
    messages_pb: list[bytes],
    msgs: Optional[list[Any]] = None,
    tools: Optional[list[dict]] = None,
) -> bytes:
    images: list[tuple[bytes, str]] = []
    if msgs:
        user_text, hist, images = _history_from_msgs(msgs)
    else:
        user_text, prior = _split_turn(messages_pb)
        hist = _conversation_history(prior) if prior else b""
    tool_defs = _mcp_tool_defs(tools)
    _tls.tool_defs = tool_defs
    try:
        import sand_server as SS  # noqa: PLC0415

        SS._log(
            f"agent run tools={len(tool_defs)} images={len(images)} "
            f"image_bytes={sum(len(d) for d, _ in images)} text_chars={len(user_text)}"
        )
    except Exception:  # noqa: BLE001
        pass
    uma = P.pb_bytes(1, _user_message(user_text, images))
    uma += P.pb_bytes(2, _request_context())
    if hist:
        uma += P.pb_bytes(7, hist)
    body = P.pb_bytes(1, _conv_state())
    body += P.pb_bytes(2, P.pb_bytes(1, uma))
    if tool_defs:
        body += P.pb_bytes(4, b"".join(P.pb_bytes(1, d) for d in tool_defs))
    body += P.pb_str(5, str(uuid.uuid4()))
    body += P.pb_bytes(9, _requested_model(model))
    # 注意：exclude_workspace_context(12) 普通账号会被拒
    # 「Workspace context exclusion is not allowed for this user, team, or selected model」
    body += P.pb_str(25, str(uuid.uuid4()))
    body += P.pb_str(26, str(uuid.uuid4()))
    return body


class _H2Bidi:
    def __init__(self, headers: dict[str, str], host: str):
        self.host = host or DEFAULT_AGENT_HOST
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.sock: Optional[ssl.SSLSocket] = None
        self.conn = h2.connection.H2Connection()
        self.stream_id: Optional[int] = None
        self.buf = bytearray()
        # 收到工具调用后设置：到点即停止读流（等并行调用收齐）
        self.soft_deadline: Optional[float] = None

    def connect(self) -> None:
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])
        raw = socket.create_connection((self.host, 443), timeout=20)
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        if self.sock.selected_alpn_protocol() != "h2":
            raise UpstreamError(502, "agent ALPN 不是 h2", "upstream_error")
        self.sock.settimeout(1.0)
        self.conn.initiate_connection()
        self._flush()
        self.stream_id = self.conn.get_next_available_stream_id()
        hdrs = [
            (":method", "POST"),
            (":authority", self.host),
            (":scheme", "https"),
            (":path", AGENT_PATH),
        ] + list(self.headers.items())
        self.conn.send_headers(self.stream_id, hdrs, end_stream=False)
        self._flush()

    def send_msg(self, payload: bytes) -> None:
        assert self.stream_id is not None
        data = P.frame(payload)
        while data:
            window = self.conn.local_flow_control_window(self.stream_id)
            chunk = min(len(data), window, self.conn.max_outbound_frame_size)
            if chunk <= 0:
                self._recv_once()
                continue
            self.conn.send_data(self.stream_id, data[:chunk], end_stream=False)
            data = data[chunk:]
            self._flush()

    def _flush(self) -> None:
        out = self.conn.data_to_send()
        if out and self.sock is not None:
            self.sock.sendall(out)

    def _recv_once(self) -> list[Any]:
        if self.sock is None:
            return []
        try:
            chunk = self.sock.recv(65535)
        except socket.timeout:
            return []
        if not chunk:
            return [h2.events.StreamEnded()]
        events = self.conn.receive_data(chunk)
        for ev in events:
            if isinstance(ev, h2.events.DataReceived):
                self.conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                self.buf.extend(ev.data)
            elif isinstance(ev, h2.events.WindowUpdated):
                pass
        self._flush()
        return events

    def iter_frames(self, stop: threading.Event, deadline: float) -> Iterator[tuple[int, bytes]]:
        while time.time() < deadline and not stop.is_set():
            if self.soft_deadline is not None and time.time() >= self.soft_deadline:
                return
            events = self._recv_once()
            for ev in events:
                if isinstance(ev, h2.events.ResponseReceived):
                    status = dict(ev.headers).get(b":status", b"?").decode()
                    if status != "200":
                        raise UpstreamError(
                            int(status) if status.isdigit() else 502,
                            f"agent HTTP {status}",
                            "upstream_error",
                        )
                elif isinstance(ev, h2.events.StreamEnded):
                    return
                elif isinstance(ev, h2.events.ConnectionTerminated):
                    raise UpstreamError(502, f"agent GOAWAY: {ev}", "upstream_error")
            while len(self.buf) >= 5:
                flag = self.buf[0]
                ln = int.from_bytes(self.buf[1:5], "big")
                if len(self.buf) < 5 + ln:
                    break
                payload = bytes(self.buf[5 : 5 + ln])
                del self.buf[: 5 + ln]
                yield flag, payload
        if time.time() >= deadline:
            raise UpstreamError(504, "agent stream timeout", "upstream_error")

    def close(self) -> None:
        try:
            self.conn.close_connection()
            self._flush()
        except Exception:
            pass
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


def _kv_reply(kid: int, get_blob: bool) -> bytes:
    if get_blob:
        err = P.pb_bytes(2, P.pb_str(1, "not found"))
        return P.pb_bytes(3, pb_u32(1, kid) + P.pb_bytes(2, err))
    return P.pb_bytes(3, pb_u32(1, kid) + P.pb_bytes(3, b""))


def _exec_ctx_ok(eid: int, exec_sid: str) -> bytes:
    success = P.pb_bytes(1, P.pb_bytes(1, _request_context()))
    body = pb_u32(1, eid)
    if exec_sid:
        body += P.pb_str(15, exec_sid)
    body += P.pb_bytes(10, P.pb_bytes(1, success))
    return P.pb_bytes(2, body)


def _exec_throw(eid: int, msg: str) -> bytes:
    return P.pb_bytes(5, P.pb_bytes(2, pb_u32(1, eid) + P.pb_str(2, msg)))


def iter_agent_events(
    access_token: str,
    machine_id: str,
    mac_machine_id: str,
    model: str,
    messages_pb: list[bytes],
    client_type: str = "ide",
    stop: Optional[threading.Event] = None,
    timeout: float = 180.0,
    agent_host: str = DEFAULT_AGENT_HOST,
    client_version: str = AGENT_CLIENT_VERSION,
    workspace: str = DEFAULT_WORKSPACE,
    msgs: Optional[list[Any]] = None,
    tools: Optional[list[dict]] = None,
) -> Iterator[dict[str, Any]]:
    stop = stop or threading.Event()
    _tls.workspace = workspace or DEFAULT_WORKSPACE
    _tls.tool_defs = []
    tool_calls = 0
    pc = P.Credentials(
        access_token=access_token,
        machine_id=machine_id,
        mac_machine_id=mac_machine_id,
        source="gateway-account",
    )
    headers = P.build_headers(pc, uuid.uuid4().hex, str(uuid.uuid4()), client_type, "")
    headers["x-cursor-client-version"] = client_version or AGENT_CLIENT_VERSION
    client = _H2Bidi(headers, agent_host or DEFAULT_AGENT_HOST)
    try:
        client.connect()
        client.send_msg(P.pb_bytes(1, build_run_request(model, messages_pb, msgs, tools)))
        last_hb = time.time()
        for flag, payload in client.iter_frames(stop, time.time() + timeout):
            if time.time() - last_hb > 5:
                client.send_msg(P.pb_bytes(7, b""))
                last_hb = time.time()
            if flag & P.FLAG_END_STREAM:
                trailer = payload.decode("utf-8", "replace").strip()
                if tool_calls and trailer:
                    # 工具调用已回给调用方，服务端随后的中止不算错误
                    return
                if trailer and trailer not in ("{}", ""):
                    info = _trailer_info(trailer)
                    msg = info.get("message") or trailer[:240]
                    raise UpstreamError(
                        401
                        if "unauthenticated" in msg.lower() or "not_logged" in msg.lower()
                        else 502,
                        msg,
                        info.get("code") or "upstream_error",
                    )
                return
            top = P.pb_parse(payload)
            if 1 in top:
                yield from _events_from_interaction(P.pb_parse(P.first(top, 1)))
            if 2 in top:
                em = P.pb_parse(P.first(top, 2))
                eid = _first_int(em, 1)
                exec_sid = P.as_text(P.first(em, 15))
                if 10 in em:
                    client.send_msg(_exec_ctx_ok(eid, exec_sid))
                elif 11 in em:
                    # 调用方工具：不在这里执行，转成 tool_call 回给调用方，收齐并行调用后断流
                    ev = _mcp_exec_event(P.pb_parse(P.first(em, 11)), tool_calls)
                    tool_calls += 1
                    client.soft_deadline = time.time() + TOOL_GRACE_S
                    yield ev
                else:
                    kinds = [k for k in em if k not in (1, 15, 19, 55, 57)]
                    client.send_msg(
                        _exec_throw(
                            eid,
                            "此环境没有本地文件系统和终端，只能使用对话中声明的工具"
                            f"（unsupported exec {kinds}）",
                        )
                    )
            if 4 in top:
                kv = P.pb_parse(P.first(top, 4))
                kid = _first_int(kv, 1)
                client.send_msg(_kv_reply(kid, 2 in kv))
    finally:
        client.close()


def _events_from_interaction(iu: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if 1 in iu:
        text = P.as_text(P.first(P.pb_parse(P.first(iu, 1)), 1))
        if text:
            out.append({"type": "text", "text": text})
    if 4 in iu:
        text = P.as_text(P.first(P.pb_parse(P.first(iu, 4)), 1))
        if text:
            out.append({"type": "thinking", "text": text})
    if 14 in iu:
        te = P.pb_parse(P.first(iu, 14))
        out.append(
            {
                "type": "usage",
                "input": _first_int(te, 1),
                "output": _first_int(te, 2),
                "cache_read": _first_int(te, 3),
                "cache_write": _first_int(te, 4),
            }
        )
    return out


def _trailer_info(trailer: str) -> dict[str, str]:
    try:
        import json

        obj = json.loads(trailer)
        err = obj.get("error") or {}
        details = err.get("details") or []
        debug = ""
        if details:
            debug = ((details[0] or {}).get("debug") or {}).get("error") or ""
        return {
            "message": err.get("message") or debug or "",
            "code": err.get("code") or debug or "",
        }
    except Exception:
        return {"message": trailer[:240], "code": ""}


async def stream_account_events(
    access_token: str,
    machine_id: str,
    mac_machine_id: str,
    model: str,
    messages_pb: list[bytes],
    client_type: str = "ide",
    agent_host: str = DEFAULT_AGENT_HOST,
    client_version: str = AGENT_CLIENT_VERSION,
    workspace: str = DEFAULT_WORKSPACE,
    msgs: Optional[list[Any]] = None,
    tools: Optional[list[dict]] = None,
) -> AsyncGenerator[dict[str, Any], None]:
    import asyncio

    q: queue.Queue[Any] = queue.Queue()
    stop = threading.Event()

    def _run() -> None:
        try:
            for ev in iter_agent_events(
                access_token,
                machine_id,
                mac_machine_id,
                model,
                messages_pb,
                client_type=client_type,
                stop=stop,
                agent_host=agent_host,
                client_version=client_version,
                workspace=workspace,
                msgs=msgs,
                tools=tools,
            ):
                q.put(ev)
        except Exception as exc:  # noqa: BLE001
            q.put(exc)
        finally:
            q.put(None)

    worker = threading.Thread(target=_run, name="agent-run", daemon=True)
    worker.start()
    try:
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()
        worker.join(timeout=2.0)
