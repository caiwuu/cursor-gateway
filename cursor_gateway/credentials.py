"""凭据与 Box 网关铸造层（按节点）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from .models import DEFAULT_BACKEND
from .paths import ensure_sys_path

ensure_sys_path()

import probe_runinference as P  # noqa: E402
import sand_server as SS  # noqa: E402

UpstreamError = SS.UpstreamError

ENSURE_SANDBOX_PATH = "/aiserver.v1.GrokBotService/EnsureSandBox"
RELAY_PATH = SS.BOX_RELAY_PATH
SAND_BOX_RUN_STATE_RUNNING = 3
_TOKEN_LEEWAY_S = 120
_BOX_REFRESH_AFTER_MS = int(os.environ.get("SAND_GATEWAY_BOX_REFRESH_MS", "3000000") or 3000000)
_BOX_WAKE_TRIES = int(os.environ.get("SAND_GATEWAY_BOX_WAKE_TRIES", "8") or 8)
_BOX_WAKE_INTERVAL_S = float(os.environ.get("SAND_GATEWAY_BOX_WAKE_INTERVAL", "5") or 5)


def _log(msg: str) -> None:
    SS._log(f"creds {msg}")


def jwt_claims(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def token_expiry(token: str) -> int:
    exp = jwt_claims(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def email_of(claims: dict) -> str:
    for key in ("email", "user_email", "https://cursor.com/email"):
        val = claims.get(key)
        if isinstance(val, str) and _looks_like_email(val):
            return val.strip()
    for key, val in claims.items():
        if "email" in key.lower() and isinstance(val, str) and _looks_like_email(val):
            return val.strip()
    sub = claims.get("sub")
    if isinstance(sub, str) and _looks_like_email(sub):
        return sub.strip()
    return ""


def _email_from_profile(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("email", "user_email", "userEmail", "customer_email"):
        val = data.get(key)
        if isinstance(val, str) and _looks_like_email(val):
            return val.strip()
    customer = data.get("customer")
    if isinstance(customer, dict):
        val = customer.get("email")
        if isinstance(val, str) and _looks_like_email(val):
            return val.strip()
    user = data.get("user")
    if isinstance(user, dict):
        val = user.get("email")
        if isinstance(val, str) and _looks_like_email(val):
            return val.strip()
    return ""


async def fetch_account_email(
    *,
    access_token: str = "",
    api_key: str = "",
    backend: str = DEFAULT_BACKEND,
) -> str:
    """JWT 经常没有 email，再向账号资料接口补一次。"""
    email = email_of(jwt_claims(access_token))
    if email:
        return email
    backend = (backend or DEFAULT_BACKEND).rstrip("/")
    tries: list[tuple[str, dict[str, str]]] = []
    if access_token:
        tries.append((f"{backend}/auth/full_stripe_profile", {"Authorization": f"Bearer {access_token}"}))
    if api_key:
        tries.append(("https://api.cursor.com/v1/me", {"Authorization": f"Bearer {api_key}"}))
    timeout = httpx.Timeout(8.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for url, headers in tries:
            try:
                resp = await client.get(url, headers=headers)
                if not resp.is_success:
                    continue
                found = _email_from_profile(resp.json())
                if found:
                    return found
            except Exception:
                continue
    return ""


def account_fingerprint(sub: str, access_token: str) -> str:
    principal = sub or access_token
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()[:16]


async def exchange_profile(api_key: str, backend: str = DEFAULT_BACKEND) -> dict:
    """用 API key 换票，返回写入节点所需的账号资料。"""
    key = (api_key or "").strip()
    if not key:
        raise UpstreamError(400, "请填写 API Key", "no_credentials")
    data = await asyncio.to_thread(P.exchange_api_key, key, (backend or DEFAULT_BACKEND).rstrip("/"))
    if not data or "accessToken" not in data:
        raise UpstreamError(401, "API key 无效或被后端拒绝", "invalid_api_key")
    access_token = data["accessToken"]
    claims = jwt_claims(access_token)
    sub = str(claims.get("sub") or "")
    email = await fetch_account_email(
        access_token=access_token, api_key=key, backend=backend
    )
    return {
        "access_token": access_token,
        "refresh_token": data.get("refreshToken") or "",
        "email": email,
        "sub": sub,
        "fingerprint": account_fingerprint(sub, access_token),
        "name": email,
    }


@dataclass
class TenantCreds:
    access_token: str
    refresh_token: str = ""
    api_key: str = ""
    sub: str = ""
    email: str = ""
    expires_at: int = 0
    fingerprint: str = ""
    machine_id: str = ""
    mac_machine_id: str = ""
    backend: str = DEFAULT_BACKEND


@dataclass
class BoxGateway:
    gateway_url: str
    gateway_token: str
    network_token: str = ""
    run_state: int = 0
    minted_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class _KeyedLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


PersistFn = Callable[[TenantCreds], None]
IdentityFn = Callable[[], tuple[str, str]]


class CredentialManager:
    def __init__(
        self,
        *,
        primary_api_key: str = "",
        backend: str = DEFAULT_BACKEND,
        identity_fn: Optional[IdentityFn] = None,
        persist_fn: Optional[PersistFn] = None,
    ) -> None:
        self._cache: dict[str, TenantCreds] = {}
        self._locks = _KeyedLocks()
        self._primary_api_key = primary_api_key
        self.backend = (backend or DEFAULT_BACKEND).rstrip("/")
        self._identity_fn = identity_fn
        self._persist_fn = persist_fn

    def configure(
        self,
        *,
        primary_api_key: str,
        backend: str,
        identity_fn: Optional[IdentityFn] = None,
        persist_fn: Optional[PersistFn] = None,
    ) -> None:
        self._primary_api_key = primary_api_key
        self.backend = (backend or DEFAULT_BACKEND).rstrip("/")
        if identity_fn is not None:
            self._identity_fn = identity_fn
        if persist_fn is not None:
            self._persist_fn = persist_fn

    @staticmethod
    def _cache_key(api_key: str, access_token: str) -> str:
        if api_key:
            return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:24]
        return "at:" + hashlib.sha256(access_token.encode()).hexdigest()[:24]

    def _identity(self) -> tuple[str, str]:
        if self._identity_fn:
            return self._identity_fn()
        return "", ""

    async def get(
        self, api_key: str = "", access_token: str = "", force: bool = False
    ) -> TenantCreds:
        if not api_key and not access_token:
            raise UpstreamError(
                401,
                "缺少凭据：请通过 Authorization: Bearer <crsr_ API key> 或 x-api-key 提供",
                "no_credentials",
            )
        key = self._cache_key(api_key, access_token)
        lock = await self._locks.get(key)
        async with lock:
            cached = self._cache.get(key)
            now = time.time()
            if (
                not force
                and cached is not None
                and (cached.expires_at == 0 or cached.expires_at - now > _TOKEN_LEEWAY_S)
            ):
                return cached
            creds = await self._mint(api_key, access_token, cached)
            self._cache[key] = creds
            return creds

    async def refresh(self, creds: TenantCreds) -> TenantCreds:
        return await self.get(
            api_key=creds.api_key, access_token=creds.access_token, force=True
        )

    async def _mint(
        self, api_key: str, access_token: str, cached: Optional[TenantCreds]
    ) -> TenantCreds:
        refresh_token = cached.refresh_token if cached else ""
        if api_key:
            data = await asyncio.to_thread(P.exchange_api_key, api_key, self.backend)
            if not data or "accessToken" not in data:
                raise UpstreamError(401, "API key 无效或被后端拒绝", "invalid_api_key")
            access_token = data["accessToken"]
            refresh_token = data.get("refreshToken", refresh_token)
            _log(f"exchanged api key -> at ({_short(access_token)})")
        else:
            if not jwt_claims(access_token):
                raise UpstreamError(
                    401,
                    "提供的凭据无法识别：需要真实的 Cursor API key（crsr_ 开头）或 access "
                    "token（JWT，eyJ 开头）。",
                    "invalid_api_key",
                )
            if token_expiry(access_token) and token_expiry(access_token) <= time.time():
                raise UpstreamError(
                    401, "access token 已过期，且未提供可续期的 API key", "token_expired"
                )
        claims = jwt_claims(access_token)
        sub = str(claims.get("sub") or "")
        mid, mac = self._identity()
        email = await fetch_account_email(
            access_token=access_token,
            api_key=api_key,
            backend=self.backend,
        )
        creds = TenantCreds(
            access_token=access_token,
            refresh_token=refresh_token,
            api_key=api_key,
            sub=sub,
            email=email,
            expires_at=token_expiry(access_token),
            fingerprint=account_fingerprint(sub, access_token),
            machine_id=mid,
            mac_machine_id=mac,
            backend=self.backend,
        )
        if self._persist_fn and self._primary_api_key and api_key == self._primary_api_key:
            try:
                self._persist_fn(creds)
                _log("回写 access_token/refresh_token 到节点")
            except Exception:  # noqa: BLE001
                pass
        return creds


class BoxManager:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[str, BoxGateway] = {}
        self._locks = _KeyedLocks()

    async def get(self, creds: TenantCreds, refresh: bool = False) -> BoxGateway:
        fp = creds.fingerprint or "default"
        lock = await self._locks.get(fp)
        async with lock:
            cached = self._cache.get(fp)
            now_ms = time.time() * 1000
            if (
                not refresh
                and cached is not None
                and cached.run_state == SAND_BOX_RUN_STATE_RUNNING
                and now_ms - cached.minted_at_ms < _BOX_REFRESH_AFTER_MS
            ):
                return cached
            box = await self._ensure(creds)
            self._cache[fp] = box
            return box

    async def _ensure(self, creds: TenantCreds) -> BoxGateway:
        box: Optional[BoxGateway] = None
        for attempt in range(_BOX_WAKE_TRIES):
            box = await self._ensure_once(creds)
            if box.run_state == SAND_BOX_RUN_STATE_RUNNING and box.gateway_url and box.gateway_token:
                return box
            if attempt < _BOX_WAKE_TRIES - 1:
                _log(
                    f"box run_state={box.run_state} (fp={creds.fingerprint})，"
                    f"等待启动 {attempt + 1}/{_BOX_WAKE_TRIES}"
                )
                await asyncio.sleep(_BOX_WAKE_INTERVAL_S)
        state = box.run_state if box else 0
        if box and box.gateway_url and box.gateway_token:
            return box
        raise UpstreamError(
            503,
            "EnsureSandBox 未返回可用的 Box 网关（Box 可能尚未就绪或该账号无 Grok Bot 资格）；"
            f"runState={state}",
            "box_not_ready",
        )

    async def _ensure_once(self, creds: TenantCreds, retries: int = 4) -> BoxGateway:
        mid, mac = creds.machine_id, creds.mac_machine_id
        backend = (creds.backend or DEFAULT_BACKEND).rstrip("/")
        url = backend + ENSURE_SANDBOX_PATH
        headers = {
            "authorization": f"Bearer {creds.access_token}",
            "connect-protocol-version": "1",
            "content-type": "application/proto",
            "x-cursor-checksum": P.cursor_checksum(mid, mac),
            "x-cursor-client-type": "sand",
            "x-cursor-client-source": "sand-desktop",
            "x-cursor-client-version": SS.BOX_CLIENT_VERSION,
            "x-sand-box-namespace": "prod",
            "x-ghost-mode": "true",
            "x-request-id": str(uuid.uuid4()),
            "user-agent": "connect-es/1.6.1",
        }
        body = P.pb_bool(2, True)
        last_detail = ""
        for attempt in range(retries + 1):
            try:
                resp = await self._client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                raise UpstreamError(504, f"EnsureSandBox 无法连接：{exc}", "box_unreachable") from exc
            if resp.status_code == 200:
                return _parse_ensure_sandbox(resp.content)
            last_detail = resp.text[:200]
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in (401, 403):
                raise UpstreamError(
                    resp.status_code,
                    f"EnsureSandBox 鉴权失败（token 可能过期或账号无资格）：{last_detail}",
                    "box_unauthorized",
                )
            raise UpstreamError(
                502, f"EnsureSandBox HTTP {resp.status_code}: {last_detail}", "box_error"
            )
        raise UpstreamError(502, f"EnsureSandBox 失败：{last_detail}", "box_error")


def _parse_ensure_sandbox(raw: bytes) -> BoxGateway:
    parsed = P.pb_parse(raw)
    gateway_url = P.as_text(P.first(parsed, 10))
    gateway_token = P.as_text(P.first(parsed, 11))
    network_token = P.as_text(P.first(parsed, 4))
    run_state_raw = P.first(parsed, 13)
    run_state = int(run_state_raw) if isinstance(run_state_raw, int) else 0
    return BoxGateway(
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        network_token=network_token,
        run_state=run_state,
    )


def _short(token: str) -> str:
    return f"{token[:8]}…{token[-6:]}" if len(token) > 20 else "<short>"
