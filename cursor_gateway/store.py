from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
import uuid
from datetime import date, timedelta
from typing import Any, Optional

import sqlite3

from .db import connect
from .accounts import (
    SESSION_TTL,
    hash_password,
    new_card_code,
    new_session_token,
    valid_username,
    verify_password,
    yuan_to_micros,
)
from .models import (
    DEFAULT_APP_PORT,
    DEFAULT_LISTEN_HOST,
    DEFAULT_NODE_PORT_START,
    ApiToken,
    AppSettings,
    LedgerEntry,
    NodeRecord,
    RedeemCard,
    RequestLog,
    UserRecord,
    default_mode_config,
    is_placeholder_secret,
    normalize_mode_config,
    normalize_model_list,
    normalize_model_prices,
    normalize_shop_url,
)
from .paths import db_path, legacy_settings_path


def _now() -> int:
    return int(time.time())


def _slugify(name: str) -> str:
    text = (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) < 2:
        return "node"
    return text[:40]


def _new_machine_pair() -> tuple[str, str]:
    return secrets.token_hex(32), secrets.token_hex(32)


def _port_free(port: int, host: str = "0.0.0.0") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_to_node(row) -> NodeRecord:
    state_raw = row["provision_state"] or "{}"
    try:
        state = json.loads(state_raw)
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    raw_cfg = "{}"
    try:
        raw_cfg = row["mode_config"] or "{}"
    except (KeyError, IndexError):
        raw_cfg = "{}"
    try:
        parsed_cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
    except Exception:
        parsed_cfg = {}
    return NodeRecord(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        enabled=bool(row["enabled"]),
        is_default=bool(row["is_default"]),
        default_mode=row["default_mode"] or "account",
        mode_config=normalize_mode_config(parsed_cfg),
        host=row["host"] or DEFAULT_LISTEN_HOST,
        port=int(row["port"]),
        api_key=row["api_key"] or "",
        access_token=row["access_token"] or "",
        refresh_token=row["refresh_token"] or "",
        backend=row["backend"] or "https://api2.cursor.sh",
        machine_id=row["machine_id"] or "",
        mac_machine_id=row["mac_machine_id"] or "",
        provision_on_missing=bool(row["provision_on_missing"]),
        provision_on_start=bool(row["provision_on_start"]),
        provision_wait=float(row["provision_wait"] or 90),
        provision_bg_wait=float(row["provision_bg_wait"] or 300),
        provision_prompt=row["provision_prompt"] or "",
        account_client_type=row["account_client_type"] or "ide",
        account_client_version=row["account_client_version"] or "3.19.13",
        account_workspace=row["account_workspace"] or "",
        agent_host=row["agent_host"] or "agentn.global.api5.cursor.sh",
        provision_state=state,
        notes=row["notes"] or "",
        email=row["email"] or "",
        sub=row["sub"] or "",
        fingerprint=row["fingerprint"] or "",
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        last_used_at=int(row["last_used_at"] or 0),
    )


def _int(row, key: str) -> int:
    try:
        return int(row[key] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _row_text(row, key: str, default: str = "") -> str:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else str(value)


def _card_enabled(row) -> bool:
    try:
        value = row["enabled"]
    except (KeyError, IndexError):
        return True
    if value is None:
        return True
    return bool(int(value))


def _card_from_row(row) -> RedeemCard:
    return RedeemCard(
        id=row["id"],
        code=row["code"] or "",
        amount=int(row["amount"] or 0),
        used_by=row["used_by"] or "",
        used_name=_row_text(row, "used_name"),
        used_at=int(row["used_at"] or 0),
        created_at=int(row["created_at"] or 0),
        note=row["note"] or "",
        enabled=_card_enabled(row),
    )


def _usage_nums(row) -> dict[str, int]:
    prompt = _int(row, "prompt_tokens")
    completion = _int(row, "completion_tokens")
    return {
        "requests": _int(row, "requests"),
        "success": _int(row, "success"),
        "errors": _int(row, "errors"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": _int(row, "cache_read_tokens"),
        "cache_write_tokens": _int(row, "cache_write_tokens"),
        "total_tokens": prompt + completion,
    }


def _row_to_log(row) -> RequestLog:
    prompt = _int(row, "prompt_tokens")
    completion = _int(row, "completion_tokens")
    return RequestLog(
        id=int(row["id"]),
        node_id=row["node_id"],
        node_name=row["node_name"] or "",
        mode=row["mode"] or "",
        model=row["model"] or "",
        protocol=row["protocol"] or "",
        stream=bool(row["stream"]),
        status=int(row["status"] or 0),
        latency_ms=int(row["latency_ms"] or 0),
        error=row["error"] or "",
        created_at=int(row["created_at"] or 0),
        token_id=row["token_id"] or "" if "token_id" in row.keys() else "",
        token_name=row["token_name"] or "" if "token_name" in row.keys() else "",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=_int(row, "cache_read_tokens"),
        cache_write_tokens=_int(row, "cache_write_tokens"),
    )


class Store:
    def __init__(self, path=None) -> None:
        self.path = path or db_path()
        self._lock = threading.RLock()
        self.conn = connect(self.path)
        self.migrate_legacy_if_needed()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def _set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO app_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _json_setting(self, key: str, default: Any) -> Any:
        raw = self._setting(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def get_app_settings(self) -> AppSettings:
        with self._lock:
            port_raw = self._setting("port", str(DEFAULT_APP_PORT))
            try:
                port = int(port_raw)
            except ValueError:
                port = DEFAULT_APP_PORT
            model_list = normalize_model_list(
                self._json_setting("model_list", None) or None
            )
            mode_defaults = normalize_mode_config(
                self._json_setting("mode_defaults", None),
                model_list=model_list,
                defaults=default_mode_config(model_list),
                allowed=set(model_list),
            )
            default_in = _as_float(self._setting("input_price_per_1m", "2"), 2.0)
            default_out = _as_float(self._setting("output_price_per_1m", "8"), 8.0)
            model_prices = normalize_model_prices(
                self._json_setting("model_prices", None),
                model_list=model_list,
                default_input=default_in,
                default_output=default_out,
            )
            return AppSettings(
                host=self._setting("host", DEFAULT_LISTEN_HOST) or DEFAULT_LISTEN_HOST,
                port=port,
                admin_token=self._setting("admin_token", ""),
                default_node_id=self._setting("default_node_id", ""),
                model_list=model_list,
                mode_defaults=mode_defaults,
                allow_register=_as_bool(self._setting("allow_register", "1"), True),
                shop_url=normalize_shop_url(self._setting("shop_url", "")),
                input_price_per_1m=default_in,
                output_price_per_1m=default_out,
                model_prices=model_prices,
            )

    def update_app_settings(self, updates: dict[str, Any]) -> AppSettings:
        allowed = {
            "host",
            "port",
            "admin_token",
            "default_node_id",
            "model_list",
            "mode_defaults",
            "allow_register",
            "shop_url",
            "input_price_per_1m",
            "output_price_per_1m",
            "model_prices",
        }
        with self._lock:
            current_list = normalize_model_list(
                self._json_setting("model_list", None) or None
            )
            if "model_list" in updates:
                current_list = normalize_model_list(updates.get("model_list"))
                self._set_setting("model_list", json.dumps(current_list, ensure_ascii=False))
            if "mode_defaults" in updates:
                cfg = normalize_mode_config(
                    updates.get("mode_defaults"),
                    model_list=current_list,
                    defaults=default_mode_config(current_list),
                    allowed=set(current_list),
                )
                self._set_setting("mode_defaults", json.dumps(cfg, ensure_ascii=False))
            default_in = _as_float(
                updates.get("input_price_per_1m", self._setting("input_price_per_1m", "2")),
                2.0,
            )
            default_out = _as_float(
                updates.get("output_price_per_1m", self._setting("output_price_per_1m", "8")),
                8.0,
            )
            if "model_prices" in updates or "model_list" in updates:
                merged = updates.get("model_prices")
                if merged is None:
                    merged = self._json_setting("model_prices", None)
                prices = normalize_model_prices(
                    merged,
                    model_list=current_list,
                    default_input=default_in,
                    default_output=default_out,
                )
                self._set_setting("model_prices", json.dumps(prices, ensure_ascii=False))
            for key, value in updates.items():
                if key not in allowed or key in {"model_list", "mode_defaults", "model_prices"}:
                    continue
                if key == "allow_register":
                    self._set_setting(key, "1" if _as_bool(value, True) else "0")
                    continue
                if key == "shop_url":
                    self._set_setting(key, normalize_shop_url(value))
                    continue
                if key in {"input_price_per_1m", "output_price_per_1m"}:
                    self._set_setting(key, str(_as_float(value, 0.0)))
                    continue
                self._set_setting(key, "" if value is None else str(value))
            if "default_node_id" in updates:
                node_id = str(updates.get("default_node_id") or "")
                self.conn.execute("UPDATE nodes SET is_default = 0")
                if node_id:
                    self.conn.execute(
                        "UPDATE nodes SET is_default = 1 WHERE id = ?", (node_id,)
                    )
            self.conn.commit()
        return self.get_app_settings()

    def list_nodes(self) -> list[NodeRecord]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM nodes ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def get_node(self, ref: str) -> Optional[NodeRecord]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM nodes WHERE id = ? OR slug = ?", (ref, ref)
            ).fetchone()
        return _row_to_node(row) if row else None

    def default_node(self) -> Optional[NodeRecord]:
        settings = self.get_app_settings()
        if settings.default_node_id:
            node = self.get_node(settings.default_node_id)
            if node:
                return node
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM nodes WHERE is_default = 1 LIMIT 1"
            ).fetchone()
            if row:
                return _row_to_node(row)
            row = self.conn.execute(
                "SELECT * FROM nodes WHERE enabled = 1 ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row:
                return _row_to_node(row)
            row = self.conn.execute(
                "SELECT * FROM nodes ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        return _row_to_node(row) if row else None

    def next_port(self, exclude_id: str = "") -> int:
        with self._lock:
            rows = self.conn.execute("SELECT id, port FROM nodes").fetchall()
        used = {int(r["port"]) for r in rows if r["port"] and r["id"] != exclude_id}
        port = DEFAULT_NODE_PORT_START
        while port in used or not _port_free(port):
            port += 1
            if port > 65535:
                raise RuntimeError("无可用端口")
        return port

    def _unique_slug(self, name: str, exclude_id: str = "") -> str:
        base = _slugify(name)
        slug = base
        n = 2
        while True:
            row = self.conn.execute(
                "SELECT id FROM nodes WHERE slug = ?", (slug,)
            ).fetchone()
            if not row or row["id"] == exclude_id:
                return slug
            slug = f"{base}-{n}"
            n += 1

    def create_node(self, payload: dict[str, Any]) -> NodeRecord:
        now = _now()
        node_id = str(payload.get("id") or uuid.uuid4())
        name = (
            payload.get("name") or payload.get("email") or "未命名节点"
        ).strip() or "未命名节点"
        with self._lock:
            slug = (payload.get("slug") or "").strip() or self._unique_slug(name)
            if self.conn.execute(
                "SELECT id FROM nodes WHERE slug = ?", (slug,)
            ).fetchone():
                slug = self._unique_slug(slug or name)
            port = payload.get("port")
            try:
                port_i = int(port) if port not in (None, "") else self.next_port()
            except (TypeError, ValueError, RuntimeError):
                port_i = self.next_port()
            machine_id = str(payload.get("machine_id") or "").strip()
            mac_machine_id = str(payload.get("mac_machine_id") or "").strip()
            if not machine_id or not mac_machine_id:
                auto_mid, auto_mac = _new_machine_pair()
                machine_id = machine_id or auto_mid
                mac_machine_id = mac_machine_id or auto_mac
            count = self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
            is_default = _as_bool(payload.get("is_default"), count == 0)
            if is_default:
                self.conn.execute("UPDATE nodes SET is_default = 0")
                self._set_setting("default_node_id", node_id)
            rec = NodeRecord(
                id=node_id,
                name=name,
                slug=slug,
                enabled=_as_bool(payload.get("enabled"), True),
                is_default=is_default,
                default_mode=str(payload.get("default_mode") or "account"),
                mode_config=normalize_mode_config(payload.get("mode_config")),
                host=str(payload.get("host") or DEFAULT_LISTEN_HOST),
                port=port_i,
                api_key=str(payload.get("api_key") or ""),
                access_token=str(payload.get("access_token") or ""),
                refresh_token=str(payload.get("refresh_token") or ""),
                backend=str(payload.get("backend") or "https://api2.cursor.sh"),
                machine_id=machine_id,
                mac_machine_id=mac_machine_id,
                provision_on_missing=_as_bool(
                    payload.get("provision_on_missing"), True
                ),
                provision_on_start=_as_bool(payload.get("provision_on_start"), True),
                provision_wait=float(payload.get("provision_wait") or 90),
                provision_bg_wait=float(payload.get("provision_bg_wait") or 300),
                provision_prompt=str(payload.get("provision_prompt") or ""),
                account_client_type=str(payload.get("account_client_type") or "ide"),
                account_client_version=str(
                    payload.get("account_client_version") or "3.19.13"
                ),
                account_workspace=str(
                    payload.get("account_workspace") or ""
                ),
                agent_host=str(
                    payload.get("agent_host") or "agentn.global.api5.cursor.sh"
                ),
                provision_state=payload.get("provision_state")
                if isinstance(payload.get("provision_state"), dict)
                else {},
                notes=str(payload.get("notes") or ""),
                email=str(payload.get("email") or ""),
                sub=str(payload.get("sub") or ""),
                fingerprint=str(payload.get("fingerprint") or ""),
                created_at=now,
                updated_at=now,
            )
            self._insert(rec)
            self.conn.commit()
        return rec

    def _insert(self, rec: NodeRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO nodes(
                id, name, slug, enabled, is_default, default_mode, mode_config, host, port,
                api_key, access_token, refresh_token, backend, machine_id,
                mac_machine_id, provision_on_missing, provision_on_start,
                provision_wait, provision_bg_wait, provision_prompt,
                account_client_type, account_client_version, account_workspace,
                agent_host, provision_state, notes, email, sub, fingerprint,
                created_at, updated_at, last_used_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                rec.id,
                rec.name,
                rec.slug,
                int(rec.enabled),
                int(rec.is_default),
                rec.default_mode,
                json.dumps(normalize_mode_config(rec.mode_config), ensure_ascii=False),
                rec.host,
                rec.port,
                rec.api_key,
                rec.access_token,
                rec.refresh_token,
                rec.backend,
                rec.machine_id,
                rec.mac_machine_id,
                int(rec.provision_on_missing),
                int(rec.provision_on_start),
                rec.provision_wait,
                rec.provision_bg_wait,
                rec.provision_prompt,
                rec.account_client_type,
                rec.account_client_version,
                rec.account_workspace,
                rec.agent_host,
                json.dumps(rec.provision_state, ensure_ascii=False),
                rec.notes,
                rec.email,
                rec.sub,
                rec.fingerprint,
                rec.created_at,
                rec.updated_at,
                rec.last_used_at,
            ),
        )

    def update_node(
        self, node_id: str, payload: dict[str, Any]
    ) -> Optional[NodeRecord]:
        current = self.get_node(node_id)
        if current is None:
            return None
        fields = dict(payload)
        for secret in ("api_key", "access_token", "refresh_token"):
            if secret in fields and is_placeholder_secret(fields[secret]):
                fields.pop(secret)
        bool_fields = (
            "enabled",
            "is_default",
            "provision_on_missing",
            "provision_on_start",
        )
        for key in bool_fields:
            if key in fields:
                fields[key] = _as_bool(fields[key])
        if "name" in fields:
            fields["name"] = (fields["name"] or "").strip() or current.name
        if "slug" in fields:
            raw = (fields["slug"] or "").strip()
            fields["slug"] = raw
        if "provision_state" in fields and not isinstance(
            fields["provision_state"], dict
        ):
            fields.pop("provision_state")
        allowed = {
            "name",
            "slug",
            "enabled",
            "is_default",
            "default_mode",
            "mode_config",
            "host",
            "port",
            "api_key",
            "access_token",
            "refresh_token",
            "backend",
            "machine_id",
            "mac_machine_id",
            "provision_on_missing",
            "provision_on_start",
            "provision_wait",
            "provision_bg_wait",
            "provision_prompt",
            "account_client_type",
            "account_client_version",
            "account_workspace",
            "agent_host",
            "provision_state",
            "notes",
            "email",
            "sub",
            "fingerprint",
            "last_used_at",
        }
        with self._lock:
            if "slug" in fields:
                fields["slug"] = self._unique_slug(
                    fields["slug"] or current.name, current.id
                )
            if fields.get("is_default"):
                self.conn.execute("UPDATE nodes SET is_default = 0")
                self._set_setting("default_node_id", current.id)
            assignments = []
            values: list[Any] = []
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key in ("provision_state", "mode_config"):
                    if key == "mode_config":
                        value = normalize_mode_config(value)
                    value = json.dumps(value, ensure_ascii=False)
                elif key in bool_fields:
                    value = int(bool(value))
                elif key == "port":
                    value = int(value)
                assignments.append(f"{key} = ?")
                values.append(value)
            assignments.append("updated_at = ?")
            values.append(_now())
            values.append(current.id)
            self.conn.execute(
                f"UPDATE nodes SET {', '.join(assignments)} WHERE id = ?", values
            )
            self.conn.commit()
        return self.get_node(current.id)

    def delete_node(self, node_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            settings = self.get_app_settings()
            if settings.default_node_id == node_id:
                self._set_setting("default_node_id", "")
            self.conn.commit()
            return cur.rowcount > 0

    def update_tokens(
        self,
        node_id: str,
        *,
        access_token: str = "",
        refresh_token: str = "",
        email: str = "",
        sub: str = "",
        fingerprint: str = "",
    ) -> None:
        fields: dict[str, Any] = {}
        if access_token:
            fields["access_token"] = access_token
        if refresh_token:
            fields["refresh_token"] = refresh_token
        if email:
            fields["email"] = email
            fields["name"] = email
        if sub:
            fields["sub"] = sub
        if fingerprint:
            fields["fingerprint"] = fingerprint
        if fields:
            self.update_node(node_id, fields)

    def update_identity(
        self, node_id: str, machine_id: str, mac_machine_id: str
    ) -> None:
        self.update_node(
            node_id, {"machine_id": machine_id, "mac_machine_id": mac_machine_id}
        )

    def update_provision_state(self, node_id: str, state: dict[str, Any]) -> None:
        self.update_node(node_id, {"provision_state": state})

    def touch_node(self, node_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE nodes SET last_used_at = ? WHERE id = ?", (_now(), node_id)
            )
            self.conn.commit()

    def log_request(
        self,
        node_id: str,
        *,
        mode: str,
        model: str,
        protocol: str,
        stream: bool,
        status: int,
        latency_ms: int,
        error: str = "",
        token_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        ok = 200 <= int(status or 0) < 400
        prompt = int(prompt_tokens or 0) if ok else 0
        completion = int(completion_tokens or 0) if ok else 0
        cache_read = int(cache_read_tokens or 0) if ok else 0
        cache_write = int(cache_write_tokens or 0) if ok else 0
        token_id = (token_id or "").strip()
        model = (model or "").strip()
        day = date.today().isoformat()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO request_logs(
                    node_id, mode, model, protocol, stream, status,
                    latency_ms, error, created_at, token_id,
                    prompt_tokens, completion_tokens,
                    cache_read_tokens, cache_write_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    mode,
                    model,
                    protocol,
                    int(stream),
                    status,
                    latency_ms,
                    (error or "")[:500],
                    _now(),
                    token_id,
                    prompt,
                    completion,
                    cache_read,
                    cache_write,
                ),
            )
            self.conn.execute(
                "DELETE FROM request_logs WHERE id NOT IN ("
                "SELECT id FROM request_logs ORDER BY id DESC LIMIT 2000)"
            )
            self.conn.execute(
                """
                INSERT INTO usage_daily(
                    day, token_id, node_id, model, requests, success, errors,
                    prompt_tokens, completion_tokens,
                    cache_read_tokens, cache_write_tokens
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, token_id, node_id, model) DO UPDATE SET
                    requests = requests + 1,
                    success = success + excluded.success,
                    errors = errors + excluded.errors,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                    cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens
                """,
                (
                    day,
                    token_id,
                    node_id,
                    model,
                    1 if ok else 0,
                    0 if ok else 1,
                    prompt,
                    completion,
                    cache_read,
                    cache_write,
                ),
            )
            self.conn.commit()

    def list_logs(
        self, node_id: str = "", limit: int = 80, offset: int = 0, user_id: str = ""
    ) -> tuple[list[RequestLog], int]:
        limit = max(1, min(int(limit or 80), 500))
        offset = max(0, int(offset or 0))
        where: list[str] = []
        args: list[Any] = []
        if node_id:
            where.append("l.node_id = ?")
            args.append(node_id)
        if user_id:
            where.append("l.token_id IN (SELECT id FROM api_tokens WHERE user_id = ?)")
            args.append(user_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = int(
                self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM request_logs l {clause}",
                    args,
                ).fetchone()["c"]
            )
            rows = self.conn.execute(
                f"""
                SELECT l.*, n.name AS node_name, t.name AS token_name
                FROM request_logs l
                LEFT JOIN nodes n ON n.id = l.node_id
                LEFT JOIN api_tokens t ON t.id = l.token_id
                {clause}
                ORDER BY l.id DESC LIMIT ? OFFSET ?
                """,
                (*args, limit, offset),
            ).fetchall()
        return [_row_to_log(r) for r in rows], total

    def usage_by_token(self) -> dict[str, dict[str, int]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT token_id,
                       COALESCE(SUM(requests), 0) AS requests,
                       COALESCE(SUM(success), 0) AS success,
                       COALESCE(SUM(errors), 0) AS errors,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM usage_daily
                GROUP BY token_id
                """
            ).fetchall()
        return {str(r["token_id"] or ""): _usage_nums(r) for r in rows}

    def usage_summary(
        self,
        *,
        days: int = 30,
        token_id: str = "",
        node_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        days = max(1, min(int(days or 30), 366))
        end = date.today()
        start = end - timedelta(days=days - 1)
        where = ["day >= ?", "day <= ?"]
        args: list[Any] = [start.isoformat(), end.isoformat()]
        if token_id:
            where.append("token_id = ?")
            args.append(token_id)
        if node_id:
            where.append("node_id = ?")
            args.append(node_id)
        if user_id:
            where.append(
                "token_id IN (SELECT id FROM api_tokens WHERE user_id = ?)"
            )
            args.append(user_id)
        clause = " AND ".join(where)

        with self._lock:
            totals_row = self.conn.execute(
                f"""
                SELECT COALESCE(SUM(requests), 0) AS requests,
                       COALESCE(SUM(success), 0) AS success,
                       COALESCE(SUM(errors), 0) AS errors,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM usage_daily
                WHERE {clause}
                """,
                args,
            ).fetchone()
            by_day = self.conn.execute(
                f"""
                SELECT day AS key,
                       COALESCE(SUM(requests), 0) AS requests,
                       COALESCE(SUM(success), 0) AS success,
                       COALESCE(SUM(errors), 0) AS errors,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM usage_daily
                WHERE {clause}
                GROUP BY day
                ORDER BY day ASC
                """,
                args,
            ).fetchall()
            by_token = self.conn.execute(
                f"""
                SELECT token_id AS key,
                       COALESCE(SUM(requests), 0) AS requests,
                       COALESCE(SUM(success), 0) AS success,
                       COALESCE(SUM(errors), 0) AS errors,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM usage_daily
                WHERE {clause}
                GROUP BY token_id
                ORDER BY prompt_tokens + completion_tokens DESC, requests DESC
                """,
                args,
            ).fetchall()
            by_node = self.conn.execute(
                f"""
                SELECT node_id AS key,
                       COALESCE(SUM(requests), 0) AS requests,
                       COALESCE(SUM(success), 0) AS success,
                       COALESCE(SUM(errors), 0) AS errors,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM usage_daily
                WHERE {clause}
                GROUP BY node_id
                ORDER BY prompt_tokens + completion_tokens DESC, requests DESC
                """,
                args,
            ).fetchall()
            by_model = self.conn.execute(
                f"""
                SELECT model AS key,
                       COALESCE(SUM(requests), 0) AS requests,
                       COALESCE(SUM(success), 0) AS success,
                       COALESCE(SUM(errors), 0) AS errors,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM usage_daily
                WHERE {clause}
                GROUP BY model
                ORDER BY prompt_tokens + completion_tokens DESC, requests DESC
                """,
                args,
            ).fetchall()
            raw_rows = self.conn.execute(
                f"""
                SELECT u.day, u.token_id, u.node_id, u.model,
                       u.requests, u.success, u.errors,
                       u.prompt_tokens, u.completion_tokens,
                       u.cache_read_tokens, u.cache_write_tokens
                FROM usage_daily u
                WHERE {clause}
                ORDER BY u.day DESC, u.prompt_tokens + u.completion_tokens DESC
                """,
                args,
            ).fetchall()

        token_names = {t.id: t.name for t in self.list_tokens()}
        node_names = {n.id: n.name for n in self.list_nodes()}

        def named(rows, kind: str) -> list[dict[str, Any]]:
            out = []
            for r in rows:
                key = str(r["key"] or "")
                item = _usage_nums(r)
                item["id"] = key
                if kind == "token":
                    item["name"] = token_names.get(key) or ("未绑定令牌" if not key else "已删除令牌")
                elif kind == "node":
                    item["name"] = node_names.get(key) or key or "未知节点"
                elif kind == "model":
                    item["name"] = key or "未知模型"
                else:
                    item["name"] = key
                out.append(item)
            return out

        rows = []
        for r in raw_rows:
            token_key = str(r["token_id"] or "")
            node_key = str(r["node_id"] or "")
            item = _usage_nums(r)
            item.update(
                {
                    "day": r["day"],
                    "token_id": token_key,
                    "token_name": token_names.get(token_key)
                    or ("未绑定令牌" if not token_key else "已删除令牌"),
                    "node_id": node_key,
                    "node_name": node_names.get(node_key) or node_key or "未知节点",
                    "model": r["model"] or "",
                }
            )
            rows.append(item)

        totals = _usage_nums(totals_row)
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "days": days,
            "totals": totals,
            "by_day": named(by_day, "day"),
            "by_token": named(by_token, "token"),
            "by_node": named(by_node, "node"),
            "by_model": named(by_model, "model"),
            "rows": rows,
        }

    def _row_to_token(self, row) -> ApiToken:
        return ApiToken(
            id=row["id"],
            name=row["name"] or "",
            token=row["token"] or "",
            enabled=bool(row["enabled"]),
            created_at=int(row["created_at"] or 0),
            last_used_at=int(row["last_used_at"] or 0),
            request_count=int(row["request_count"] or 0),
            user_id=_row_text(row, "user_id"),
            username=_row_text(row, "username"),
        )

    def has_api_tokens(self) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM api_tokens").fetchone()
        return bool(row and int(row["c"]) > 0)

    def list_tokens(self, user_id: str = "") -> list[ApiToken]:
        with self._lock:
            if user_id:
                rows = self.conn.execute(
                    """
                    SELECT t.*, u.username AS username
                    FROM api_tokens t
                    LEFT JOIN users u ON u.id = t.user_id
                    WHERE t.user_id = ?
                    ORDER BY t.created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT t.*, u.username AS username
                    FROM api_tokens t
                    LEFT JOIN users u ON u.id = t.user_id
                    ORDER BY t.created_at DESC
                    """
                ).fetchall()
        return [self._row_to_token(r) for r in rows]

    def get_token(self, ref: str) -> Optional[ApiToken]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT t.*, u.username AS username
                FROM api_tokens t
                LEFT JOIN users u ON u.id = t.user_id
                WHERE t.id = ? OR t.token = ?
                """,
                (ref, ref),
            ).fetchone()
        return self._row_to_token(row) if row else None

    def create_token(self, name: str, user_id: str = "") -> ApiToken:
        now = _now()
        rec = ApiToken(
            id=str(uuid.uuid4()),
            name=(name or "").strip() or "未命名令牌",
            token="sk-" + secrets.token_urlsafe(36),
            enabled=True,
            created_at=now,
            last_used_at=0,
            request_count=0,
            user_id=(user_id or "").strip(),
        )
        with self._lock:
            if rec.user_id:
                owner = self.conn.execute(
                    "SELECT username FROM users WHERE id = ?", (rec.user_id,)
                ).fetchone()
                rec.username = owner["username"] if owner else ""
            self.conn.execute(
                """
                INSERT INTO api_tokens(
                    id, name, token, enabled, created_at, last_used_at, request_count, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.id,
                    rec.name,
                    rec.token,
                    1,
                    rec.created_at,
                    rec.last_used_at,
                    rec.request_count,
                    rec.user_id,
                ),
            )
            self.conn.commit()
        return rec

    def update_token(self, token_id: str, payload: dict[str, Any]) -> Optional[ApiToken]:
        current = self.get_token(token_id)
        if current is None:
            return None
        name = payload.get("name")
        enabled = payload.get("enabled")
        with self._lock:
            if name is not None:
                self.conn.execute(
                    "UPDATE api_tokens SET name = ? WHERE id = ?",
                    ((str(name).strip() or current.name), current.id),
                )
            if enabled is not None:
                self.conn.execute(
                    "UPDATE api_tokens SET enabled = ? WHERE id = ?",
                    (1 if _as_bool(enabled) else 0, current.id),
                )
            self.conn.commit()
        return self.get_token(current.id)

    def delete_token(self, token_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def touch_token(self, token_id: str, *, count: bool = False) -> None:
        with self._lock:
            if count:
                self.conn.execute(
                    "UPDATE api_tokens SET last_used_at = ?, "
                    "request_count = request_count + 1 WHERE id = ?",
                    (_now(), token_id),
                )
            else:
                self.conn.execute(
                    "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                    (_now(), token_id),
                )
            self.conn.commit()

    def stats(self) -> dict[str, Any]:
        day_ago = _now() - 86400
        with self._lock:
            nodes = self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
            enabled = self.conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE enabled = 1"
            ).fetchone()["c"]
            today = self.conn.execute(
                "SELECT COUNT(*) AS c FROM request_logs WHERE created_at >= ?",
                (day_ago,),
            ).fetchone()["c"]
            errors = self.conn.execute(
                "SELECT COUNT(*) AS c FROM request_logs "
                "WHERE created_at >= ? AND status >= 400",
                (day_ago,),
            ).fetchone()["c"]
            token_count = self.conn.execute(
                "SELECT COUNT(*) AS c FROM api_tokens"
            ).fetchone()["c"]
            user_count = self.conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]
            usage_24h = self.conn.execute(
                """
                SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM request_logs WHERE created_at >= ?
                """,
                (day_ago,),
            ).fetchone()
        default = self.default_node()
        prompt_24h = int(usage_24h["prompt_tokens"] or 0)
        completion_24h = int(usage_24h["completion_tokens"] or 0)
        return {
            "nodes": int(nodes),
            "enabled": int(enabled),
            "requests_24h": int(today),
            "errors_24h": int(errors),
            "prompt_tokens_24h": prompt_24h,
            "completion_tokens_24h": completion_24h,
            "total_tokens_24h": prompt_24h + completion_24h,
            "tokens": int(token_count),
            "users": int(user_count),
            "default_node_id": default.id if default else "",
            "default_node_name": default.name if default else "",
        }

    def _row_to_user(self, row) -> UserRecord:
        role = _row_text(row, "role", "user")
        if role not in ("admin", "user"):
            role = "user"
        return UserRecord(
            id=row["id"],
            username=row["username"] or "",
            password_hash=row["password_hash"] or "",
            role=role,
            enabled=bool(row["enabled"]),
            balance=int(row["balance"] or 0),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
            token_count=_int(row, "token_count"),
        )

    def has_admin(self) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND enabled = 1"
            ).fetchone()
        return bool(row and int(row["c"]) > 0)

    def admin_count(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT u.*,
                       (SELECT COUNT(*) FROM api_tokens t WHERE t.user_id = u.id) AS token_count
                FROM users u
                ORDER BY u.created_at DESC
                """
            ).fetchall()
        return [self._row_to_user(r) for r in rows]

    def get_user(self, ref: str) -> Optional[UserRecord]:
        key = (ref or "").strip()
        if not key:
            return None
        with self._lock:
            row = self.conn.execute(
                """
                SELECT u.*,
                       (SELECT COUNT(*) FROM api_tokens t WHERE t.user_id = u.id) AS token_count
                FROM users u
                WHERE u.id = ? OR lower(u.username) = lower(?)
                """,
                (key, key),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def create_user(
        self, username: str, password: str, *, yuan: float = 0, role: str = "user"
    ) -> UserRecord:
        name = (username or "").strip()
        if not valid_username(name):
            raise ValueError("用户名需 2–32 位，仅字母数字下划线或中文")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        if self.get_user(name):
            raise ValueError("用户名已被占用")
        role = "admin" if role == "admin" or not self.has_admin() else "user"
        now = _now()
        rec = UserRecord(
            id=str(uuid.uuid4()),
            username=name,
            password_hash=hash_password(password),
            role=role,
            enabled=True,
            balance=yuan_to_micros(yuan),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO users(id, username, password_hash, role, enabled, balance, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        rec.id,
                        rec.username,
                        rec.password_hash,
                        rec.role,
                        rec.balance,
                        rec.created_at,
                        rec.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名已被占用") from exc
            if rec.balance:
                self.conn.execute(
                    """
                    INSERT INTO balance_ledger(user_id, kind, amount, balance, note, created_at)
                    VALUES (?, 'recharge', ?, ?, '开户赠送', ?)
                    """,
                    (rec.id, rec.balance, rec.balance, now),
                )
            self.conn.commit()
        return rec

    def update_user(self, user_id: str, payload: dict[str, Any]) -> Optional[UserRecord]:
        current = self.get_user(user_id)
        if current is None:
            return None
        with self._lock:
            if "enabled" in payload:
                enabled = 1 if _as_bool(payload.get("enabled")) else 0
                if current.role == "admin" and not enabled and self.admin_count() <= 1:
                    raise ValueError("不能停用唯一的管理员")
                self.conn.execute(
                    "UPDATE users SET enabled = ?, updated_at = ? WHERE id = ?",
                    (enabled, _now(), current.id),
                )
            if "role" in payload:
                role = str(payload.get("role") or "").strip()
                if role not in ("admin", "user"):
                    raise ValueError("角色只能是 admin 或 user")
                if current.role == "admin" and role != "admin" and self.admin_count() <= 1:
                    raise ValueError("不能取消唯一的管理员")
                self.conn.execute(
                    "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                    (role, _now(), current.id),
                )
            password = payload.get("password")
            if password:
                if len(str(password)) < 6:
                    raise ValueError("密码至少 6 位")
                self.conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (hash_password(str(password)), _now(), current.id),
                )
            self.conn.commit()
        return self.get_user(current.id)

    def delete_user(self, user_id: str) -> bool:
        current = self.get_user(user_id)
        if current is None:
            return False
        if current.role == "admin" and self.admin_count() <= 1:
            raise ValueError("不能删除唯一的管理员")
        with self._lock:
            self.conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (current.id,))
            self.conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (current.id,))
            self.conn.execute("DELETE FROM balance_ledger WHERE user_id = ?", (current.id,))
            cur = self.conn.execute("DELETE FROM users WHERE id = ?", (current.id,))
            self.conn.commit()
            return cur.rowcount > 0

    def adjust_balance(self, user_id: str, micros: int, *, kind: str, note: str = "") -> UserRecord:
        current = self.get_user(user_id)
        if current is None:
            raise ValueError("用户不存在")
        delta = int(micros)
        with self._lock:
            row = self.conn.execute(
                "SELECT balance FROM users WHERE id = ?", (current.id,)
            ).fetchone()
            before = int(row["balance"] or 0) if row else 0
            after = max(0, before + delta) if kind == "usage" else max(0, before + delta)
            self.conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE id = ?",
                (after, _now(), current.id),
            )
            self.conn.execute(
                """
                INSERT INTO balance_ledger(user_id, kind, amount, balance, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (current.id, kind, delta, after, (note or "")[:200], _now()),
            )
            self.conn.commit()
        rec = self.get_user(current.id)
        if rec is None:
            raise ValueError("用户不存在")
        return rec

    def charge_user(self, user_id: str, micros: int, *, note: str = "") -> Optional[UserRecord]:
        cost = int(micros or 0)
        if cost <= 0 or not user_id:
            return self.get_user(user_id)
        return self.adjust_balance(user_id, -cost, kind="usage", note=note)

    def create_session(self, user_id: str) -> str:
        token = new_session_token()
        now = _now()
        with self._lock:
            self.conn.execute(
                "DELETE FROM user_sessions WHERE user_id = ? OR expires_at < ?",
                (user_id, now),
            )
            self.conn.execute(
                "INSERT INTO user_sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now, now + SESSION_TTL),
            )
            self.conn.commit()
        return token

    def user_by_session(self, token: str) -> Optional[UserRecord]:
        raw = (token or "").strip()
        if not raw:
            return None
        now = _now()
        with self._lock:
            row = self.conn.execute(
                "SELECT user_id FROM user_sessions WHERE token = ? AND expires_at > ?",
                (raw, now),
            ).fetchone()
        if not row:
            return None
        return self.get_user(row["user_id"])

    def delete_session(self, token: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM user_sessions WHERE token = ?", ((token or "").strip(),))
            self.conn.commit()

    def authenticate(self, username: str, password: str) -> UserRecord:
        user = self.get_user(username)
        if user is None or not verify_password(password, user.password_hash):
            raise ValueError("用户名或密码错误")
        if not user.enabled:
            raise ValueError("账号已停用")
        return user

    def create_cards(self, count: int, yuan: float, *, note: str = "") -> list[RedeemCard]:
        n = max(1, min(int(count or 1), 100))
        amount = yuan_to_micros(yuan)
        if amount <= 0:
            raise ValueError("卡密面额必须大于 0")
        now = _now()
        cards: list[RedeemCard] = []
        with self._lock:
            for _ in range(n):
                rec = RedeemCard(
                    id=str(uuid.uuid4()),
                    code=new_card_code(),
                    amount=amount,
                    created_at=now,
                    note=(note or "").strip(),
                )
                self.conn.execute(
                    """
                    INSERT INTO redeem_cards(id, code, amount, used_by, used_at, created_at, note, enabled)
                    VALUES (?, ?, ?, '', 0, ?, ?, 1)
                    """,
                    (rec.id, rec.code, rec.amount, rec.created_at, rec.note),
                )
                cards.append(rec)
            self.conn.commit()
        return cards

    def list_cards(self) -> list[RedeemCard]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT c.*, u.username AS used_name
                FROM redeem_cards c
                LEFT JOIN users u ON u.id = c.used_by
                ORDER BY c.created_at DESC
                """
            ).fetchall()
        return [_card_from_row(row) for row in rows]

    def get_card(self, card_id: str) -> Optional[RedeemCard]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT c.*, u.username AS used_name
                FROM redeem_cards c
                LEFT JOIN users u ON u.id = c.used_by
                WHERE c.id = ?
                """,
                ((card_id or "").strip(),),
            ).fetchone()
        return _card_from_row(row) if row else None

    def update_card(self, card_id: str, *, enabled: Optional[bool] = None) -> RedeemCard:
        rec = self.get_card(card_id)
        if rec is None:
            raise ValueError("卡密不存在")
        if rec.used_by:
            raise ValueError("已兑换的卡密不能改状态")
        if enabled is None:
            return rec
        with self._lock:
            self.conn.execute(
                "UPDATE redeem_cards SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, rec.id),
            )
            self.conn.commit()
        updated = self.get_card(rec.id)
        if updated is None:
            raise ValueError("卡密不存在")
        return updated

    def delete_cards(self, ids: list[str]) -> int:
        clean = [str(x).strip() for x in ids if str(x).strip()]
        if not clean:
            return 0
        placeholders = ",".join("?" for _ in clean)
        with self._lock:
            cur = self.conn.execute(
                f"DELETE FROM redeem_cards WHERE id IN ({placeholders})",
                clean,
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def redeem_card(self, user_id: str, code: str) -> UserRecord:
        raw = re.sub(r"\s+", "", (code or "").strip().upper())
        if not raw:
            raise ValueError("请填写卡密")
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("用户不存在")
        now = _now()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM redeem_cards WHERE upper(code) = ?", (raw,)
            ).fetchone()
            if row is None:
                raise ValueError("卡密不存在")
            if row["used_by"]:
                raise ValueError("卡密已被使用")
            if not _card_enabled(row):
                raise ValueError("卡密已停用")
            amount = int(row["amount"] or 0)
            self.conn.execute(
                "UPDATE redeem_cards SET used_by = ?, used_at = ? WHERE id = ?",
                (user.id, now, row["id"]),
            )
            bal = self.conn.execute(
                "SELECT balance FROM users WHERE id = ?", (user.id,)
            ).fetchone()
            after = int(bal["balance"] or 0) + amount
            self.conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE id = ?",
                (after, now, user.id),
            )
            self.conn.execute(
                """
                INSERT INTO balance_ledger(user_id, kind, amount, balance, note, created_at)
                VALUES (?, 'redeem', ?, ?, ?, ?)
                """,
                (user.id, amount, after, f"兑换 {raw}", now),
            )
            self.conn.commit()
        rec = self.get_user(user.id)
        if rec is None:
            raise ValueError("用户不存在")
        return rec

    def list_ledger(self, user_id: str = "", limit: int = 50) -> list[LedgerEntry]:
        limit = max(1, min(int(limit or 50), 200))
        with self._lock:
            if user_id:
                rows = self.conn.execute(
                    """
                    SELECT * FROM balance_ledger WHERE user_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM balance_ledger ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            LedgerEntry(
                id=int(r["id"]),
                user_id=r["user_id"] or "",
                kind=r["kind"] or "",
                amount=int(r["amount"] or 0),
                balance=int(r["balance"] or 0),
                note=r["note"] or "",
                created_at=int(r["created_at"] or 0),
            )
            for r in rows
        ]

    def migrate_legacy_if_needed(self) -> Optional[NodeRecord]:
        with self._lock:
            count = self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
        if count:
            return None
        path = legacy_settings_path()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        payload = {
            "name": "从 settings.json 导入",
            "slug": "legacy",
            "enabled": True,
            "is_default": True,
            "default_mode": data.get("default_mode") or "account",
            "host": data.get("host") or DEFAULT_LISTEN_HOST,
            "port": data.get("port") or DEFAULT_NODE_PORT_START,
            "api_key": data.get("api_key")
            or data.get("bot_api_key")
            or data.get("cursor_api_key")
            or "",
            "access_token": data.get("access_token")
            or data.get("bot_access_token")
            or data.get("cursor_access_token")
            or "",
            "refresh_token": data.get("refresh_token") or "",
            "backend": data.get("backend") or "https://api2.cursor.sh",
            "machine_id": data.get("machine_id") or "",
            "mac_machine_id": data.get("mac_machine_id") or "",
            "provision_on_missing": data.get("provision_on_missing", True),
            "provision_on_start": data.get("provision_on_start", True),
            "provision_wait": data.get("provision_wait") or 90,
            "provision_bg_wait": data.get("provision_bg_wait") or 300,
            "provision_prompt": data.get("provision_prompt") or "",
            "provision_state": data.get("_provision_state")
            if isinstance(data.get("_provision_state"), dict)
            else {},
            "notes": "由 sand_gateway/settings.json 首次启动自动导入",
        }
        node = self.create_node(payload)
        with self._lock:
            if data.get("host"):
                self._set_setting("host", str(data.get("host")))
            self.conn.commit()
        return node
