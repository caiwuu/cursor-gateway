"""Box relay 自动挂载（provision）—— 按节点保存 provision-state。"""

from __future__ import annotations

import json
import time
import uuid
import asyncio
from typing import Any, Callable, Optional

import httpx

from .credentials import BoxGateway, BoxManager, RELAY_PATH, TenantCreds, UpstreamError
from .paths import ensure_sys_path

ensure_sys_path()

import sand_server as SS  # noqa: E402

BOX_RELAY_PROVISION_PROMPT = r"""
请在当前 Box 内部署 Cursor Sand Stream relay：用预构建仓库里的 host 覆盖安装，不要自行改写任何代码。严格按顺序执行：

1) 检查目录 /workspace/grok-box 是否存在；若不存在，先克隆仓库：
   git clone https://github.com/caiwuu/grok-box.git /workspace/grok-box

2) 依次执行以下命令（备份现有 host → 用仓库版本覆盖 host 与 version → 结束当前 host 进程）：
   cp -a /home/box/sand-host/host-main.cjs /home/box/sand-host/host-main.cjs.bak.manual
   cp -a /workspace/grok-box/box/sand-host/host-main.cjs /home/box/sand-host/host-main.cjs
   cp -a /workspace/grok-box/box/sand-host/version /home/box/sand-host/version
   kill -TERM "$(pgrep -f '/home/box/sand-host/host-main.cjs' | head -1)"

3) 最后按当前 Box 的标准方式重启 sand host（bot），确认新的 host 进程已拉起。

执行完请确认新 host 进程在运行；不要打印或暴露任何 token/credential。
""".strip()

_RESEND_IDLE_MS = 15 * 60 * 1000
_plocks: dict[str, asyncio.Lock] = {}
_plocks_guard: asyncio.Lock | None = None


def _log(msg: str) -> None:
    SS._log(f"provision {msg}")


async def _plock(fp: str) -> asyncio.Lock:
    global _plocks_guard
    if _plocks_guard is None:
        _plocks_guard = asyncio.Lock()
    async with _plocks_guard:
        lock = _plocks.get(fp)
        if lock is None:
            lock = asyncio.Lock()
            _plocks[fp] = lock
        return lock


def _gw_url(box: BoxGateway, path: str) -> str:
    return SS._join_box_url(box.gateway_url, path)


def _gw_headers(box: BoxGateway, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers: dict[str, str] = {"authorization": f"Bearer {box.gateway_token}"}
    if box.network_token:
        headers["x-anyrun-network-token"] = box.network_token
    if extra:
        headers.update(extra)
    return headers


def _connect_stream_has_end_frame(body: bytes) -> bool:
    offset = 0
    saw_end = False
    while offset + 5 <= len(body):
        flags = body[offset]
        length = int.from_bytes(body[offset + 1 : offset + 5], "big")
        offset += 5
        if offset + length > len(body):
            return False
        if flags & 0x02:
            saw_end = True
        offset += length
    return saw_end and offset == len(body)


async def probe_relay(client: httpx.AsyncClient, box: BoxGateway) -> tuple[int, str, bool]:
    url = _gw_url(box, RELAY_PATH)
    headers = _gw_headers(
        box,
        {
            "content-type": "application/connect+proto",
            "connect-protocol-version": "1",
            "x-request-id": str(uuid.uuid4()),
        },
    )
    try:
        resp = await client.post(
            url,
            content=b"\x00\x00\x00\x00\x00",
            headers=headers,
            timeout=httpx.Timeout(25.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(504, f"探测 relay 失败：{exc}", "box_unreachable") from exc
    body = await resp.aread()
    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
    ok = (
        resp.status_code == 200
        and content_type.casefold().startswith("application/connect+proto")
        and _connect_stream_has_end_frame(body)
    )
    return resp.status_code, content_type, ok


def _find_agent_id_in_record(value: Any) -> str:
    stack = [value]
    fallback = ""
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("agentId", "id"):
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate:
                    if key == "agentId":
                        return candidate
                    if not fallback:
                        fallback = candidate
            for nested in current.values():
                if isinstance(nested, (dict, list)):
                    stack.append(nested)
        elif isinstance(current, list):
            stack.extend(current)
    return fallback


async def create_box_agent(client: httpx.AsyncClient, box: BoxGateway) -> str:
    body = {
        "name": "Cursor Sand Relay",
        "description": "Hosts the Cursor Sand Stream relay route.",
        "creationRoute": {"kind": "box"},
        "harness": "box",
        "isIntroductionSuppressed": True,
        "isKickstartRequested": False,
        "clientNonce": str(uuid.uuid4()),
        "supportsTemporalHarness": True,
    }
    try:
        resp = await client.post(
            _gw_url(box, "/api/createAgent"),
            json=body,
            headers=_gw_headers(box, {"x-sand-slim-avatars": "1"}),
            timeout=httpx.Timeout(30.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(504, f"createAgent 连接失败：{exc}", "box_unreachable") from exc
    detail = (await resp.aread()).decode("utf-8", "replace")
    if resp.status_code < 200 or resp.status_code >= 300:
        raise UpstreamError(
            502,
            f"在 Box 内创建 relay agent 失败：HTTP {resp.status_code} {detail[:300]}",
            "create_agent_failed",
        )
    try:
        record = json.loads(detail)
    except Exception as exc:
        raise UpstreamError(502, "Box 网关 createAgent 返回格式无效", "create_agent_failed") from exc
    agent_id = _find_agent_id_in_record(record)
    if not agent_id:
        raise UpstreamError(502, f"createAgent 未返回 agent id：{detail[:200]}", "create_agent_failed")
    return agent_id


async def send_provision_prompt(
    client: httpx.AsyncClient, box: BoxGateway, agent_id: str, prompt: str
) -> None:
    body = {
        "prompt": prompt,
        "agentId": agent_id,
        "clientNonce": str(uuid.uuid4()),
        "source": "desktop",
        "sessionId": "",
    }

    async def _post() -> None:
        try:
            resp = await client.post(
                _gw_url(box, "/api/sendPrompt"),
                json=body,
                headers=_gw_headers(box, {"x-sand-slim-avatars": "1"}),
                timeout=httpx.Timeout(25.0, connect=15.0),
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(504, f"sendPrompt 连接失败：{exc}", "box_unreachable") from exc
        detail = (await resp.aread()).decode("utf-8", "replace")
        if resp.status_code < 200 or resp.status_code >= 300:
            raise UpstreamError(
                502,
                f"下发初始化指令失败：HTTP {resp.status_code} {detail[:300]}",
                "send_prompt_failed",
            )
        try:
            data = json.loads(detail)
        except Exception as exc:
            raise UpstreamError(502, "Box agent sendPrompt 返回格式无效", "send_prompt_failed") from exc
        if not isinstance(data, dict) or data.get("accepted") is not True:
            raise UpstreamError(502, "Box agent 未接受初始化指令", "send_prompt_failed")

    ev_cm = None
    try:
        ev_cm = client.stream(
            "GET",
            _gw_url(box, "/events"),
            headers=_gw_headers(box, {"Accept": "text/event-stream"}),
            timeout=httpx.Timeout(25.0, connect=15.0),
        )
        try:
            await ev_cm.__aenter__()
        except Exception:
            ev_cm = None
        await _post()
    finally:
        if ev_cm is not None:
            try:
                await ev_cm.__aexit__(None, None, None)
            except Exception:
                pass


StateGet = Callable[[], dict]
StateSet = Callable[[str, dict], None]


async def provision_box(
    client: httpx.AsyncClient,
    bm: BoxManager,
    creds: TenantCreds,
    *,
    wait_seconds: float,
    force: bool = False,
    get_state: Optional[StateGet] = None,
    set_state: Optional[StateSet] = None,
    prompt: str = "",
) -> dict[str, Any]:
    fp = creds.fingerprint or "default"
    lock = await _plock(fp)
    prompt_text = (prompt or "").strip() or BOX_RELAY_PROVISION_PROMPT
    async with lock:
        _log(f"start fp={fp} wait={wait_seconds}s force={force}")
        box = await bm.get(creds, refresh=True)
        status, content_type, ok = await probe_relay(client, box)
        _log(f"probe fp={fp} HTTP {status} ct={content_type or '-'} ok={ok}")
        if ok:
            return {
                "status": "already-installed",
                "fingerprint": fp,
                "run_state": box.run_state,
                "relay_probe": status,
                "relay_url": _gw_url(box, RELAY_PATH),
            }
        if status in (401, 403):
            raise UpstreamError(status, "Box 网关鉴权失败（token 过期或账号无资格）", "box_unauthorized")

        state = get_state() if get_state else {}
        if not isinstance(state, dict):
            state = {}
        entry = state.get(fp) if isinstance(state.get(fp), dict) else {}
        entry = entry or {}
        raw_agent = entry.get("relayAgentId")
        agent_id = raw_agent if isinstance(raw_agent, str) else ""
        raw_last = entry.get("lastSentMs")
        last_sent = int(raw_last) if isinstance(raw_last, (int, float)) else 0
        now_ms = int(time.time() * 1000)

        need_send = False
        if not agent_id:
            _log(f"create box agent fp={fp}")
            agent_id = await create_box_agent(client, box)
            need_send = True
        elif force or (now_ms - last_sent > _RESEND_IDLE_MS):
            need_send = True
            _log(f"reuse agent fp={fp} agent={agent_id}（重新下发）")
        else:
            _log(f"reuse agent fp={fp} agent={agent_id}（仍在处理，直接轮询）")

        if need_send:
            try:
                await send_provision_prompt(client, box, agent_id, prompt_text)
            except UpstreamError as exc:
                if "does not exist" in (exc.message or "").lower():
                    _log(f"agent 已不在 Box 内，重建 fp={fp}")
                    agent_id = await create_box_agent(client, box)
                    await send_provision_prompt(client, box, agent_id, prompt_text)
                else:
                    raise
            last_sent = int(time.time() * 1000)
        if set_state:
            set_state(fp, {"relayAgentId": agent_id, "lastSentMs": last_sent})

        deadline = time.monotonic() + max(0.0, wait_seconds)
        started = time.monotonic()
        last_status = status
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            try:
                box = await bm.get(creds, refresh=True)
                last_status, content_type, ok = await probe_relay(client, box)
            except UpstreamError:
                continue
            _log(f"waiting fp={fp} {int(time.monotonic() - started)}s relay HTTP {last_status}")
            if ok:
                return {
                    "status": "installed",
                    "fingerprint": fp,
                    "relay_agent_id": agent_id,
                    "relay_probe": last_status,
                    "relay_url": _gw_url(box, RELAY_PATH),
                }
            if last_status in (401, 403):
                raise UpstreamError(last_status, "provision 期间网关鉴权失败", "box_unauthorized")

        return {
            "status": "provisioning",
            "fingerprint": fp,
            "relay_agent_id": agent_id,
            "relay_probe": last_status,
            "detail": "Box 仍在后台改写 host-main.cjs 并重启；稍后重试 provision（会复用同一 agent 继续等，不重复扣费）。",
        }
