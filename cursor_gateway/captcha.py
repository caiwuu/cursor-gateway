"""短时图形验证码：登录 / 注册防刷。答案只存内存，用一次即作废。"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from html import escape

_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_TTL = 300
_MAX = 2000
_lock = threading.Lock()
_items: dict[str, tuple[str, float]] = {}


def _purge(now: float) -> None:
    stale = [key for key, (_, exp) in _items.items() if exp <= now]
    for key in stale:
        _items.pop(key, None)
    while len(_items) > _MAX:
        _items.pop(next(iter(_items)), None)


def _svg(code: str) -> str:
    width, height = 148, 46
    rng = secrets.SystemRandom()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" rx="8" fill="#f3f7f6"/>',
    ]
    for _ in range(5):
        x1, y1 = rng.randint(4, width - 4), rng.randint(4, height - 4)
        x2, y2 = rng.randint(4, width - 4), rng.randint(4, height - 4)
        color = rng.choice(("#9ad8cf", "#7bb8b0", "#c5d5d1", "#8aa39e"))
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{rng.uniform(0.6, 1.4):.1f}" opacity="0.7"/>'
        )
    for i, ch in enumerate(code):
        x = 16 + i * 26 + rng.randint(-3, 3)
        y = rng.randint(28, 36)
        rot = rng.randint(-22, 22)
        size = rng.randint(20, 24)
        fill = rng.choice(("#0b7068", "#14524c", "#0f8f83", "#1a3d39"))
        parts.append(
            f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="700" '
            f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
            f'transform="rotate({rot} {x} {y})">{escape(ch)}</text>'
        )
    for _ in range(18):
        cx, cy = rng.randint(6, width - 6), rng.randint(6, height - 6)
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="1" fill="#8aa39e" opacity="0.45"/>')
    parts.append("</svg>")
    return "".join(parts)


def issue(length: int = 4) -> dict[str, object]:
    code = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    captcha_id = secrets.token_urlsafe(18)
    svg = _svg(code)
    image = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    now = time.time()
    with _lock:
        _purge(now)
        _items[captcha_id] = (code, now + _TTL)
    return {"id": captcha_id, "image": image, "ttl": _TTL}


def consume(captcha_id: str, answer: str) -> bool:
    key = (captcha_id or "").strip()
    guess = "".join((answer or "").split()).upper()
    now = time.time()
    with _lock:
        _purge(now)
        item = _items.pop(key, None)
    if item is None:
        return False
    code, exp = item
    if exp <= now:
        return False
    return secrets.compare_digest(code, guess)
