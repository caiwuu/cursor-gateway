#!/usr/bin/env python3
"""GrokBotService 云 agent 客户端（异步）。

Cursor 已关停本地直连推理端点（InferenceService/Stream、AiService/StreamChat 等）。
Grok Bot（0.44.0）唯一能用的路径是 GrokBotService 的云 agent 编排：

  ListGrokBotAgents（Unary）        → 拿已有 agent 的 id
  WatchGrokBotTranscripts（Stream） → 建 transcript 流
  SendGrokBotUserMessage（Unary）   → 发一条用户消息，云端 agent 跑完把结果写进流

协议关键点（已实测确认）：
- GrokBotService 走 HTTP/1.1（不是 HTTP/2）。
- Unary 方法：content-type `application/proto`，请求/响应是**裸 protobuf 不分帧**。
- ServerStreaming：content-type `application/connect+proto` + Connect 5 字节分帧。
- transcript 条目 body 是 UTF-8 JSON；AI 回复在 `send-message` 条目里，
  `message.type == "text"` 时 `message.content` 是完整文本。

对外核心接口：`send_and_collect(agent_id, text, token)` 异步生成器，流式产出
AI 回复文本。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import time
import uuid
from typing import AsyncIterator, Optional

import httpx

from probe_runinference import (
    _read_varint,
    _varint,
    frame,
    pb_bytes,
    pb_str,
)

BACKEND = "https://api2.cursor.sh"
SVC = "/aiserver.v1.GrokBotService"
GB_UUID = os.environ.get("GROKBOT_MACHINE_UUID", "24640a09-51f7-4036-b8ca-ddc108324bc7")
QUIET_TIMEOUT = float(os.environ.get("GROKBOT_QUIET_TIMEOUT", "8") or 8)
HARD_TIMEOUT = float(os.environ.get("GROKBOT_HARD_TIMEOUT", "180") or 180)

# 缓存：避免每次请求都 ListGrokBotAgents
_agent_cache: dict[str, str] = {}


def pb_int(field_no: int, value: int) -> bytes:
    """int64/uint32 正值按 varint 编码。"""
    return _varint(field_no << 3) + _varint(value)


def machine_id() -> str:
    return hashlib.sha256(GB_UUID.encode()).hexdigest()


def gb_checksum(mid: str) -> str:
    """复刻 Grok Bot 的 x-cursor-checksum：时间戳 6 字节大端 → 滚动异或 → base64url + machineId。"""
    stamp = int(time.time() * 1000) // 1_000_000
    raw = bytearray((stamp >> s) & 0xFF for s in (40, 32, 24, 16, 8, 0))
    prev = 165
    for i in range(len(raw)):
        raw[i] = ((raw[i] ^ prev) + i % 256) & 0xFF
        prev = raw[i]
    prefix = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    return f"{prefix}{mid}"


def build_headers(token: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {token}",
        "x-cursor-checksum": gb_checksum(machine_id()),
        "x-cursor-client-type": "sand",
        "x-cursor-client-source": "sand-desktop",
        "x-cursor-client-version": "0.44.0",
        "x-sand-box-namespace": "prod",
        "x-ghost-mode": "false",
    }


# ---------------------------------------------------------------------------
# protobuf 解析
# ---------------------------------------------------------------------------


def _parse_agent(buf: bytes) -> dict[str, str]:
    out = {"id": "", "name": "", "viewer_session_id": ""}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 2:
            ln, pos = _read_varint(buf, pos)
            v = buf[pos : pos + ln]
            pos += ln
            if fn == 1:
                out["id"] = v.decode("utf-8", "replace")
            elif fn == 3:
                out["name"] = v.decode("utf-8", "replace")
            elif fn == 18:
                out["viewer_session_id"] = v.decode("utf-8", "replace")
        elif wt == 0:
            _, pos = _read_varint(buf, pos)
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return out


def _parse_agents(payload: bytes) -> list[dict[str, str]]:
    buf = payload
    pos = 0
    agents: list[dict[str, str]] = []
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 2:
            ln, pos = _read_varint(buf, pos)
            v = buf[pos : pos + ln]
            pos += ln
            if fn == 1:
                agents.append(_parse_agent(v))
        elif wt == 0:
            _, pos = _read_varint(buf, pos)
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return agents


def _parse_entry(buf: bytes) -> tuple[str, bytes]:
    """返回 (entry_kind, body)。"""
    kind = ""
    body = b""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            _, pos = _read_varint(buf, pos)
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            v = buf[pos : pos + ln]
            pos += ln
            if fn == 2:
                kind = v.decode("utf-8", "replace")
            elif fn == 3:
                body = v
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return kind, body


def _extract_text_from_frame(payload: bytes) -> list[str]:
    """顶层帧 field 2 = rows；rows field 3 = entries；取 send-message 的 text。"""
    out: list[str] = []
    pos = 0
    while pos < len(payload):
        key, pos = _read_varint(payload, pos)
        fn, wt = key >> 3, key & 7
        if wt == 2:
            ln, pos = _read_varint(payload, pos)
            v = payload[pos : pos + ln]
            pos += ln
            if fn == 2:  # rows
                p2 = 0
                while p2 < len(v):
                    k2, p2 = _read_varint(v, p2)
                    f2, w2 = k2 >> 3, k2 & 7
                    if w2 == 2:
                        l2, p2 = _read_varint(v, p2)
                        e = v[p2 : p2 + l2]
                        p2 += l2
                        if f2 == 3:  # entries
                            kind, body = _parse_entry(e)
                            if kind == "send-message":
                                t = _send_message_text(body)
                                if t:
                                    out.append(t)
                    elif w2 == 0:
                        _, p2 = _read_varint(v, p2)
                    elif w2 == 5:
                        p2 += 4
                    elif w2 == 1:
                        p2 += 8
                    else:
                        break
        elif wt == 0:
            _, pos = _read_varint(payload, pos)
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return out


def _send_message_text(body: bytes) -> str:
    """send-message 条目 body 是 JSON：{"kind":"send-message","message":{"type":"text","content":"..."}}。"""
    try:
        j = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    msg = j.get("message") if isinstance(j, dict) else None
    if not isinstance(msg, dict):
        return ""
    if msg.get("type") != "text":
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# RPC 调用
# ---------------------------------------------------------------------------


async def list_agents(client: httpx.AsyncClient, token: str) -> list[dict[str, str]]:
    """列出账号下所有 sand agent（不缓存，供交互式选择/刷新用）。"""
    headers = build_headers(token)
    headers["content-type"] = "application/proto"
    r = await client.post(BACKEND + SVC + "/ListGrokBotAgents", content=b"", headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"ListGrokBotAgents HTTP {r.status_code}: {r.content[:200]}")
    return _parse_agents(r.content)


async def list_agent_id(client: httpx.AsyncClient, token: str) -> str:
    """拿第一个 agent 的 id（带缓存）。"""
    if "id" in _agent_cache:
        return _agent_cache["id"]
    agents = await list_agents(client, token)
    if not agents:
        raise RuntimeError("ListGrokBotAgents 返回空列表（账号没有 sand agent？）")
    _agent_cache["id"] = agents[0]["id"]
    return agents[0]["id"]


async def send_and_collect(
    agent_id: str,
    text: str,
    token: str,
    quiet_s: float = QUIET_TIMEOUT,
    hard_s: float = HARD_TIMEOUT,
    quiet_after_text_s: Optional[float] = None,
) -> AsyncIterator[str]:
    """发一条用户消息，流式产出云端 agent 的 AI 回复文本。

    静默 quiet_s 秒无新文本即认为回复结束（agent 回复是一次性完整文本，之后
    只有心跳帧）。整体硬超时 hard_s 秒兜底。

    quiet_after_text_s 非空时，收到首段文本后改用这个更短的静默阈值：等 agent
    开口要给足时间，开口之后不必再等满 quiet_s。
    """
    headers = build_headers(token)
    hs = dict(headers)
    hs["content-type"] = "application/connect+proto"
    hs["connect-protocol-version"] = "1"
    hu = dict(headers)
    hu["content-type"] = "application/proto"

    cursor = pb_bytes(1, pb_str(1, agent_id) + pb_int(2, 0))
    wbody = frame(pb_bytes(1, cursor))

    timeout = httpx.Timeout(hard_s + 30.0, connect=20.0, read=hard_s + 30.0)
    async with httpx.AsyncClient(http2=False, timeout=timeout) as client:
        async with client.stream(
            "POST", BACKEND + SVC + "/WatchGrokBotTranscripts", content=wbody, headers=hs
        ) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"WatchGrokBotTranscripts HTTP {resp.status_code}: {detail}")

            # 发消息
            req = (
                pb_str(1, agent_id)
                + pb_str(2, str(uuid.uuid4()))
                + pb_str(3, text)
                + pb_int(4, int(time.time() * 1000))
            )
            sr = await client.post(
                BACKEND + SVC + "/SendGrokBotUserMessage", content=req, headers=hu
            )
            if sr.status_code != 200:
                raise RuntimeError(
                    f"SendGrokBotUserMessage HTTP {sr.status_code}: {sr.content[:200]}"
                )

            # 迭代流：分帧 + 静默超时
            buf = bytearray()
            it = resp.aiter_bytes().__aiter__()
            started = time.monotonic()
            seen_text = False
            while True:
                remaining = hard_s - (time.monotonic() - started)
                if remaining <= 0:
                    break
                quiet = quiet_after_text_s if (seen_text and quiet_after_text_s) else quiet_s
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=min(quiet, remaining))
                except asyncio.TimeoutError:
                    break  # 静默超时，回复结束
                except StopAsyncIteration:
                    break
                buf.extend(chunk)
                while len(buf) >= 5:
                    flag = buf[0]
                    (ln,) = struct.unpack(">I", bytes(buf[1:5]))
                    if len(buf) < 5 + ln:
                        break
                    payload = bytes(buf[5 : 5 + ln])
                    del buf[: 5 + ln]
                    if flag & 0x02:  # end-of-stream
                        return
                    for t in _extract_text_from_frame(payload):
                        seen_text = True
                        yield t


# 供导入方使用的同步 asyncio 助手
def get_agent_id_sync(token: str) -> str:
    import asyncio

    async def _inner() -> str:
        async with httpx.AsyncClient(http2=False, timeout=httpx.Timeout(30.0, connect=15.0)) as c:
            return await list_agent_id(c, token)

    return asyncio.run(_inner())
