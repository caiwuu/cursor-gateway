"""account 模式：官方 3.19 AgentService/Run（HTTP/2 双向流）。"""

from __future__ import annotations

import queue
import socket
import ssl
import threading
import time
import uuid
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
AGENT_MODE_ASK = 2
DEFAULT_WORKSPACE = "/tmp/sand-account"

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
    return P.pb_enum(10, AGENT_MODE_ASK) + pb_u64(26, now) + P.pb_str(27, "Asia/Shanghai")


def _workspace() -> str:
    return getattr(_tls, "workspace", None) or DEFAULT_WORKSPACE


def _req_env() -> bytes:
    workspace = _workspace()
    return (
        P.pb_str(1, "darwin")
        + P.pb_str(2, workspace)
        + P.pb_str(3, "/bin/zsh")
        + P.pb_bool(5, False)
        + P.pb_str(10, "Asia/Shanghai")
        + P.pb_str(11, workspace)
        + P.pb_str(21, workspace)
    )


def _request_context() -> bytes:
    body = P.pb_bytes(4, _req_env())
    for f in (33, 36, 39, 40, 41, 42, 43, 44, 45):
        body += P.pb_bool(f, True)
    return body


def _user_message(text: str) -> bytes:
    return P.pb_str(1, text) + P.pb_str(2, str(uuid.uuid4())) + P.pb_enum(4, AGENT_MODE_ASK)


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


def build_run_request(model: str, messages_pb: list[bytes]) -> bytes:
    user_text, prior = _split_turn(messages_pb)
    uma = P.pb_bytes(1, _user_message(user_text))
    uma += P.pb_bytes(2, _request_context())
    if prior:
        uma += P.pb_bytes(7, _conversation_history(prior))
    body = P.pb_bytes(1, _conv_state())
    body += P.pb_bytes(2, P.pb_bytes(1, uma))
    body += P.pb_str(5, str(uuid.uuid4()))
    body += P.pb_bytes(9, _requested_model(model))
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
) -> Iterator[dict[str, Any]]:
    stop = stop or threading.Event()
    _tls.workspace = workspace or DEFAULT_WORKSPACE
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
        client.send_msg(P.pb_bytes(1, build_run_request(model, messages_pb)))
        last_hb = time.time()
        for flag, payload in client.iter_frames(stop, time.time() + timeout):
            if time.time() - last_hb > 5:
                client.send_msg(P.pb_bytes(7, b""))
                last_hb = time.time()
            if flag & P.FLAG_END_STREAM:
                trailer = payload.decode("utf-8", "replace").strip()
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
                else:
                    kinds = [k for k in em if k not in (1, 15, 19, 55, 57)]
                    client.send_msg(_exec_throw(eid, f"unsupported exec {kinds}"))
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
