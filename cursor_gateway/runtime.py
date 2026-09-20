"""节点运行时：每账号一套凭据管理 + 独立监听端口。"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import uvicorn

from .credentials import BoxManager, CredentialManager, TenantCreds
from .models import NodeRecord, RuntimeStatus, modes_for_model
from .store import Store


def _log(msg: str) -> None:
    from .paths import ensure_sys_path

    ensure_sys_path()
    import sand_server as SS

    SS._log(f"node {msg}")


@dataclass
class Shared:
    store: Store
    client: httpx.AsyncClient
    client_h2: httpx.AsyncClient
    box_manager: BoxManager


class NodeRuntime:
    def __init__(self, record: NodeRecord, shared: Shared) -> None:
        self.record = record
        self.shared = shared
        self.inflight = 0
        self.last_picked_at = 0.0
        self.cm = CredentialManager(
            primary_api_key=record.api_key,
            backend=record.backend,
            identity_fn=self.identity,
            persist_fn=self._persist,
        )
        if not record.machine_id:
            self.identity()

    def apply(self, record: NodeRecord) -> None:
        self.record = record
        self.cm.configure(
            primary_api_key=record.api_key,
            backend=record.backend,
            identity_fn=self.identity,
            persist_fn=self._persist,
        )

    def identity(self) -> tuple[str, str]:
        rec = self.record
        if rec.machine_id:
            return rec.machine_id, rec.mac_machine_id
        mid = secrets.token_hex(32)
        mac = secrets.token_hex(32)
        self.shared.store.update_identity(rec.id, mid, mac)
        rec.machine_id = mid
        rec.mac_machine_id = mac
        return mid, mac

    def _persist(self, creds: TenantCreds) -> None:
        self.shared.store.update_tokens(
            self.record.id,
            access_token=creds.access_token,
            refresh_token=creds.refresh_token,
            email=creds.email,
            sub=creds.sub,
            fingerprint=creds.fingerprint,
        )
        self.record.access_token = creds.access_token
        self.record.refresh_token = creds.refresh_token
        if creds.email:
            self.record.email = creds.email
            self.record.name = creds.email
        if creds.sub:
            self.record.sub = creds.sub
        if creds.fingerprint:
            self.record.fingerprint = creds.fingerprint

    def provision_state(self) -> dict:
        fresh = self.shared.store.get_node(self.record.id)
        if fresh is not None:
            self.record.provision_state = fresh.provision_state
        return dict(self.record.provision_state or {})

    def set_provision_entry(self, fp: str, entry: dict) -> None:
        state = self.provision_state()
        state[fp] = entry
        self.record.provision_state = state
        self.shared.store.update_provision_state(self.record.id, state)


class SoftServer(uvicorn.Server):
    """节点旁路端口：绑失败只抛错，不 sys.exit 拖垮管理进程。"""

    def install_signal_handlers(self) -> None:
        return

    async def startup(self, sockets=None) -> None:
        try:
            await super().startup(sockets=sockets)
        except SystemExit as exc:
            raise OSError(f"无法绑定 {self.config.host}:{self.config.port}") from exc


class PortBinder:
    def __init__(self) -> None:
        self.server: Optional[uvicorn.Server] = None
        self.task: Optional[asyncio.Task] = None
        self.error = ""
        self.listening = False
        self.host = ""
        self.port = 0

    async def start(self, app, host: str, port: int) -> None:
        await self.stop()
        self.error = ""
        self.host = host
        self.port = int(port)
        config = uvicorn.Config(
            app,
            host=host,
            port=int(port),
            lifespan="off",
            log_level="warning",
            access_log=False,
        )
        self.server = SoftServer(config)
        self.task = asyncio.create_task(self._serve(), name=f"node-port-{port}")
        for _ in range(50):
            await asyncio.sleep(0.05)
            if self.server.started:
                self.listening = True
                return
            if self.task.done():
                self.error = self._task_error() or f"端口 {port} 未能启动"
                self.listening = False
                return
        if self.server.started:
            self.listening = True
        else:
            self.error = f"端口 {port} 启动超时"
            self.listening = False

    async def _serve(self) -> None:
        assert self.server is not None
        try:
            await self.server.serve()
        except (OSError, SystemExit) as exc:
            self.error = str(exc) or f"端口 {self.port} 被占用"
            self.listening = False

    def _task_error(self) -> str:
        if self.task is None or not self.task.done() or self.task.cancelled():
            return self.error
        try:
            exc = self.task.exception()
        except Exception as err:  # noqa: BLE001
            return str(err)
        return str(exc) if exc else self.error

    async def stop(self) -> None:
        self.listening = False
        if self.server is not None:
            self.server.should_exit = True
        task, self.task = self.task, None
        self.server = None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
                try:
                    await task
                except Exception:
                    pass


@dataclass
class Registry:
    shared: Shared
    runtimes: dict[str, NodeRuntime] = field(default_factory=dict)
    binders: dict[str, PortBinder] = field(default_factory=dict)
    _slug_index: dict[str, str] = field(default_factory=dict)

    def reload_records(self) -> None:
        for rec in self.shared.store.list_nodes():
            current = self.runtimes.get(rec.id)
            if current is None:
                self.runtimes[rec.id] = NodeRuntime(rec, self.shared)
            else:
                current.apply(rec)
        alive = {n.id for n in self.shared.store.list_nodes()}
        for nid in list(self.runtimes):
            if nid not in alive:
                self.runtimes.pop(nid, None)
        self._slug_index = {rt.record.slug: rt.record.id for rt in self.runtimes.values()}

    def get(self, ref: str) -> Optional[NodeRuntime]:
        if not ref:
            return None
        if ref in self.runtimes:
            return self.runtimes[ref]
        nid = self._slug_index.get(ref)
        if nid:
            return self.runtimes.get(nid)
        rec = self.shared.store.get_node(ref)
        if rec is None:
            return None
        self.reload_records()
        return self.runtimes.get(rec.id)

    def default(self) -> Optional[NodeRuntime]:
        rec = self.shared.store.default_node()
        if rec is None:
            return None
        return self.get(rec.id)

    def eligible(self, rt: NodeRuntime, mode: str = "", model: str = "") -> bool:
        rec = rt.record
        if not (rec.enabled and (rec.api_key or rec.access_token)):
            return False
        return bool(modes_for_model(rec.mode_config, model, mode))

    def pick(self, mode: str = "", model: str = "") -> Optional[NodeRuntime]:
        """在已启用、有凭据、且能跑该模型（及指定模式）的节点里选负载最低的一个。"""
        candidates = [rt for rt in self.runtimes.values() if self.eligible(rt, mode, model)]
        if not candidates:
            return None
        chosen = min(
            candidates,
            key=lambda rt: (rt.inflight, rt.last_picked_at, rt.record.created_at),
        )
        chosen.last_picked_at = time.monotonic()
        return chosen

    def resolve(
        self,
        ref: Optional[str],
        *,
        require_enabled: bool = True,
        mode: str = "",
        model: str = "",
    ) -> NodeRuntime:
        from .credentials import UpstreamError

        if ref:
            rt = self.get(ref)
            if rt is None:
                raise UpstreamError(404, "节点不存在或尚未配置", "node_not_found")
            if require_enabled and not rt.record.enabled:
                raise UpstreamError(503, f"节点「{rt.record.name}」已停用", "node_disabled")
            return rt
        rt = self.pick(mode, model)
        if rt is None:
            if model and mode:
                msg = f"没有可用节点能用 {mode} 模式跑 {model}。"
            elif model:
                msg = f"没有可用节点开放模型 {model}。"
            elif mode:
                msg = f"没有启用 {mode} 模式的可用节点。"
            else:
                msg = "没有可用节点：请先添加账号并填写 API Key，保持节点启用，且至少启用一种推理模式。"
            raise UpstreamError(503, msg, "no_available_node")
        return rt

    def resolve_default(self, *, require_enabled: bool = True) -> NodeRuntime:
        from .credentials import UpstreamError

        rt = self.default()
        if rt is None:
            raise UpstreamError(404, "节点不存在或尚未配置", "node_not_found")
        if require_enabled and not rt.record.enabled:
            raise UpstreamError(503, f"节点「{rt.record.name}」已停用", "node_disabled")
        return rt

    def status_of(self, node_id: str) -> RuntimeStatus:
        rt = self.runtimes.get(node_id)
        binder = self.binders.get(node_id)
        rec = rt.record if rt else self.shared.store.get_node(node_id)
        enabled = bool(rec.enabled) if rec else False
        listening = bool(binder and binder.listening)
        error = binder.error if binder else ""
        listen_url = ""
        if rec and rec.port:
            host = rec.host if rec.host not in ("0.0.0.0", "::") else "127.0.0.1"
            listen_url = f"http://{host}:{rec.port}"
        return RuntimeStatus(
            node_id=node_id,
            enabled=enabled,
            listening=listening,
            listen_error=error,
            listen_url=listen_url,
        )

    async def start_node(self, node_id: str, app_factory) -> RuntimeStatus:
        self.reload_records()
        rt = self.runtimes.get(node_id)
        if rt is None:
            raise KeyError(node_id)
        if not rt.record.enabled:
            self.shared.store.update_node(node_id, {"enabled": True})
            self.reload_records()
            rt = self.runtimes[node_id]
        binder = self.binders.get(node_id)
        if binder is None:
            binder = PortBinder()
            self.binders[node_id] = binder
        app = app_factory(rt)
        try:
            await binder.start(app, rt.record.host, rt.record.port)
        except Exception as exc:  # noqa: BLE001
            binder.error = str(exc)
            binder.listening = False
        if not binder.listening:
            try:
                alt = self.shared.store.next_port(exclude_id=node_id)
            except Exception:
                alt = 0
            if alt and alt != rt.record.port:
                _log(f"{rt.record.name} port {rt.record.port} busy, retry {alt}")
                self.shared.store.update_node(node_id, {"port": alt})
                self.reload_records()
                rt = self.runtimes[node_id]
                app = app_factory(rt)
                try:
                    await binder.start(app, rt.record.host, rt.record.port)
                except Exception as exc:  # noqa: BLE001
                    binder.error = str(exc)
                    binder.listening = False
        if binder.listening:
            _log(f"{rt.record.name} listening {rt.record.host}:{rt.record.port}")
        else:
            _log(f"{rt.record.name} listen failed: {binder.error}")
        return self.status_of(node_id)

    async def stop_node(self, node_id: str, *, disable: bool = True) -> RuntimeStatus:
        binder = self.binders.get(node_id)
        if binder:
            await binder.stop()
        if disable:
            self.shared.store.update_node(node_id, {"enabled": False})
        self.reload_records()
        return self.status_of(node_id)

    async def sync_listeners(self, app_factory) -> None:
        self.reload_records()
        wanted = {nid for nid, rt in self.runtimes.items() if rt.record.enabled}
        for nid in list(self.binders):
            if nid not in wanted:
                await self.binders[nid].stop()
        for nid in wanted:
            binder = self.binders.get(nid)
            if binder and binder.listening:
                rt = self.runtimes[nid]
                if binder.port == rt.record.port and binder.host == rt.record.host:
                    continue
            try:
                await self.start_node(nid, app_factory)
            except Exception as exc:  # noqa: BLE001
                _log(f"sync listen {nid} failed: {exc}")
