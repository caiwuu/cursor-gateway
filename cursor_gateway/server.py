"""cursor-gateway 主进程：管理台 + 多节点网关。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import admin as AD
from . import gateway as GW
from . import user as UR
from .credentials import BoxManager, UpstreamError
from .paths import data_dir, db_path, ensure_sys_path, web_dist
from .runtime import Registry, Shared
from .store import Store

ensure_sys_path()

import sand_server as SS  # noqa: E402


def _log(msg: str) -> None:
    SS._log(f"app {msg}")


def _mk_client(http2: bool = False, read_timeout: float = 300.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=http2,
        timeout=httpx.Timeout(read_timeout, connect=15.0, read=read_timeout, write=30.0, pool=10.0),
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=64),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = Store()
    client = _mk_client(False)
    client_h2 = _mk_client(True)
    shared = Shared(
        store=store,
        client=client,
        client_h2=client_h2,
        box_manager=BoxManager(client),
    )
    SS._http = client
    registry = Registry(shared)
    registry.reload_records()
    app.state.store = store
    app.state.shared = shared
    app.state.registry = registry
    settings = store.get_app_settings()
    _log(
        f"ready nodes={len(registry.runtimes)} db={db_path()} "
        f"ui={settings.host}:{settings.port} data={data_dir()}"
    )
    try:
        try:
            await registry.sync_listeners(GW.create_node_app)
        except Exception as exc:  # noqa: BLE001
            _log(f"sync listeners failed: {exc}")
        for rt in list(registry.runtimes.values()):
            if rt.record.enabled:
                await GW.warmup_node(rt)
        yield
    finally:
        for nid in list(registry.binders):
            await registry.binders[nid].stop()
        await client.aclose()
        await client_h2.aclose()
        SS._http = None
        store.close()


app = FastAPI(title="cursor-gateway", lifespan=_lifespan, docs_url="/api/docs", redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


_PUBLIC_API = {
    "/api/user/shop",
    "/api/user/login",
    "/api/user/register",
    "/api/user/setup",
    "/api/user/captcha",
    "/api/docs",
    "/api/openapi.json",
}


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-user-session") or "").strip()


@app.middleware("http")
async def _admin_auth(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in _PUBLIC_API or path.startswith("/api/user/"):
        return await call_next(request)
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:
        return await call_next(request)
    got = _bearer(request)
    user = store.user_by_session(got) if got else None
    if user is not None and user.enabled and user.role == "admin":
        request.state.admin_user = user
        return await call_next(request)
    token = store.get_app_settings().admin_token
    env_token = os.environ.get("CURSOR_GATEWAY_ADMIN_TOKEN", "")
    needed = env_token or token
    if needed and got == needed:
        return await call_next(request)
    return JSONResponse({"error": "unauthorized", "message": "请先用管理员账号登录"}, status_code=401)


@app.exception_handler(UpstreamError)
async def _upstream_error(_request: Request, exc: UpstreamError):
    return SS._openai_error_response(exc)


def _default_rt(node_ref=None):
    registry: Registry = app.state.registry
    if node_ref:
        return registry.resolve(node_ref)
    return registry.resolve_default()


app.include_router(AD.router)
app.include_router(UR.router)
app.include_router(GW.build_gateway_router(_default_rt, with_node_prefix=True, include_root=True))
app.include_router(GW.build_gateway_router(_default_rt, with_node_prefix=False, include_root=False))


_DIST = web_dist()
_ASSETS = _DIST / "assets"
if _ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=_ASSETS), name="assets")


@app.get("/")
async def spa_root():
    index = _DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return {
        "service": "cursor-gateway",
        "ui": "frontend not built",
        "hint": "cd cursor-gateway/web && npm install && npm run build",
        "api": "/api/meta",
        "docs": "/api/docs",
    }


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    reserved = (
        "api/",
        "n/",
        "v1/",
        "bot/",
        "account/",
        "sand-direct/",
        "healthz",
        "assets/",
    )
    if full_path.startswith(reserved) or full_path in {
        "healthz",
        "favicon.ico",
    }:
        return JSONResponse({"error": "not found", "path": full_path}, status_code=404)
    index = _DIST / "index.html"
    if index.is_file():
        asset = _DIST / full_path
        if asset.is_file():
            return FileResponse(asset)
        return FileResponse(index)
    return JSONResponse({"error": "not found", "path": full_path}, status_code=404)
