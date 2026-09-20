"""用户账号：密码、卡密、按 Token 计价。余额单位是微元（1 元 = 1_000_000）。"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

YUAN_MICROS = 1_000_000
SESSION_TTL = 30 * 86400
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{2,32}$")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 210_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = (stored or "").split("$", 2)
    if len(parts) != 3 or parts[0] != "pbkdf2":
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), bytes.fromhex(parts[1]), 210_000
    )
    return secrets.compare_digest(dk.hex(), parts[2])


def new_session_token() -> str:
    return "us-" + secrets.token_urlsafe(32)


def new_card_code() -> str:
    raw = secrets.token_hex(8).upper()
    return "CG-" + "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def usage_cost_micros(prompt: int, completion: int, input_price: float, output_price: float) -> int:
    return max(
        0,
        int(round(int(prompt or 0) * float(input_price) + int(completion or 0) * float(output_price))),
    )


def yuan_to_micros(yuan: Any) -> int:
    try:
        value = float(yuan)
    except (TypeError, ValueError):
        return 0
    return max(0, int(round(value * YUAN_MICROS)))


def micros_to_yuan(micros: int) -> float:
    return round(int(micros or 0) / YUAN_MICROS, 6)


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.fullmatch((name or "").strip()))
