from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    default_mode TEXT NOT NULL DEFAULT 'account',
    mode_config TEXT NOT NULL DEFAULT '{}',
    host TEXT NOT NULL DEFAULT '0.0.0.0',
    port INTEGER NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT 'https://api2.cursor.sh',
    machine_id TEXT NOT NULL DEFAULT '',
    mac_machine_id TEXT NOT NULL DEFAULT '',
    provision_on_missing INTEGER NOT NULL DEFAULT 1,
    provision_on_start INTEGER NOT NULL DEFAULT 1,
    provision_wait REAL NOT NULL DEFAULT 90,
    provision_bg_wait REAL NOT NULL DEFAULT 300,
    provision_prompt TEXT NOT NULL DEFAULT '',
    account_client_type TEXT NOT NULL DEFAULT 'ide',
    account_client_version TEXT NOT NULL DEFAULT '3.19.13',
    account_workspace TEXT NOT NULL DEFAULT '/tmp/sand-account',
    agent_host TEXT NOT NULL DEFAULT 'agentn.global.api5.cursor.sh',
    provision_state TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    sub TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    protocol TEXT NOT NULL DEFAULT '',
    stream INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    token_id TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT NOT NULL,
    token_id TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, token_id, node_id, model)
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    user_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    enabled INTEGER NOT NULL DEFAULT 1,
    balance INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS redeem_cards (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL,
    used_by TEXT NOT NULL DEFAULT '',
    used_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS balance_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    balance INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_slug ON nodes(slug);
CREATE INDEX IF NOT EXISTS idx_nodes_port ON nodes(port);
CREATE INDEX IF NOT EXISTS idx_logs_created ON request_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_node ON request_logs(node_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_tokens_token ON api_tokens(token);
CREATE INDEX IF NOT EXISTS idx_usage_daily_day ON usage_daily(day);
CREATE INDEX IF NOT EXISTS idx_usage_daily_token ON usage_daily(token_id, day);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_cards_code ON redeem_cards(code);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON balance_ledger(user_id, created_at DESC);
"""

_NODE_COLUMNS = (
    ("mode_config", "TEXT NOT NULL DEFAULT '{}'"),
)

_REQUEST_LOG_COLUMNS = (
    ("token_id", "TEXT NOT NULL DEFAULT ''"),
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
)

_API_TOKEN_COLUMNS = (
    ("user_id", "TEXT NOT NULL DEFAULT ''"),
)

_USER_COLUMNS = (
    ("role", "TEXT NOT NULL DEFAULT 'user'"),
)

_REDEEM_CARD_COLUMNS = (
    ("enabled", "INTEGER NOT NULL DEFAULT 1"),
)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def migrate(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "nodes" in tables:
        have = _column_names(conn, "nodes")
        for name, decl in _NODE_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {decl}")
    if "request_logs" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        have = _column_names(conn, "request_logs")
        for name, decl in _REQUEST_LOG_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE request_logs ADD COLUMN {name} {decl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_token ON request_logs(token_id, created_at DESC)"
        )
    if "api_tokens" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        have = _column_names(conn, "api_tokens")
        for name, decl in _API_TOKEN_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE api_tokens ADD COLUMN {name} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id)")
    if "users" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        have = _column_names(conn, "users")
        for name, decl in _USER_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
    if "redeem_cards" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        have = _column_names(conn, "redeem_cards")
        for name, decl in _REDEEM_CARD_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE redeem_cards ADD COLUMN {name} {decl}")
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
    )
    conn.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn
