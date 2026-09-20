#!/usr/bin/env python3
"""本地调用 AiService.RunGenerateImage，把图存到本机。

和聊天不是同一条链路：
  聊天  box-relay + grokBotToken + InferenceService.Stream
  出图  本机直连 api2 + Cursor session + sand 头 + AiService.RunGenerateImage

用法：
  .venv-probe/bin/python generate_image.py "白底正中一个红色小方块"
  .venv-probe/bin/python generate_image.py "一只猫" -o cat.jpg
  .venv-probe/bin/python generate_image.py "古风女子" --aspect 9:16 -o portrait.jpg
  .venv-probe/bin/python generate_image.py "换成夜景" --ref generated/cat.jpg --aspect 9:16
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

import httpx

import probe_runinference as P

RPC = "/aiserver.v1.AiService/RunGenerateImage"
DEFAULT_MODEL = "grok-4.5"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
ERROR_RE = re.compile(r"ERROR_[A-Z0-9_]+")
SIZE_RE = re.compile(r"^(\d+)\s*[x×]\s*(\d+)$", re.I)
RATIO_RE = re.compile(r"^(\d+)\s*[:/]\s*(\d+)$")
SUPPORTED_ASPECTS = {
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "16:9": (16, 9),
    "9:16": (9, 16),
}
OPENAI_SIZES = {
    "256x256": "1:1",
    "512x512": "1:1",
    "1024x1024": "1:1",
    "1024x1536": "9:16",
    "1536x1024": "16:9",
    "1024x1792": "9:16",
    "1792x1024": "16:9",
}
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
QUALITY_MAX_MODE = {
    "high": True,
    "hd": True,
    "max": True,
    "xhigh": True,
    "low": False,
    "medium": False,
    "standard": False,
    "auto": True,
}
REF_MAX_BYTES = 20 * 1024 * 1024
ASPECT_ALIASES = {
    "竖版": "9:16",
    "竖图": "9:16",
    "portrait": "9:16",
    "横版": "16:9",
    "横图": "16:9",
    "landscape": "16:9",
    "方形": "1:1",
    "正方形": "1:1",
    "square": "1:1",
}


class GenerateImageError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _scrub(text: str) -> str:
    text = JWT_RE.sub("<jwt>", text or "")
    return re.sub(r"Bearer\s+\S+", "Bearer <redacted>", text, flags=re.I)


def _snap_aspect(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise GenerateImageError(400, f"invalid aspect {width}:{height}", "invalid_aspect")
    target = width / height
    return min(
        SUPPORTED_ASPECTS.items(),
        key=lambda item: abs(item[1][0] / item[1][1] - target),
    )[0]


def normalize_aspect_ratio(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    alias = ASPECT_ALIASES.get(raw.lower()) or ASPECT_ALIASES.get(raw)
    if alias:
        return alias
    compact = raw.replace(" ", "").replace("×", "x")
    if compact in SUPPORTED_ASPECTS:
        return compact
    if compact.lower() in OPENAI_SIZES:
        return OPENAI_SIZES[compact.lower()]
    size = SIZE_RE.fullmatch(compact)
    if size:
        return _snap_aspect(int(size.group(1)), int(size.group(2)))
    ratio = RATIO_RE.fullmatch(compact)
    if ratio:
        return _snap_aspect(int(ratio.group(1)), int(ratio.group(2)))
    raise GenerateImageError(
        400,
        "aspect must be 1:1, 4:3, 3:4, 16:9, 9:16, 1024x1536, or 竖版; "
        f"got {value}",
        "invalid_aspect",
    )


def quality_to_max_mode(quality: Optional[str], default: bool = True) -> bool:
    if quality is None:
        return default
    key = str(quality).strip().lower()
    if not key:
        return default
    if key not in QUALITY_MAX_MODE:
        raise GenerateImageError(
            400,
            "quality must be low, medium, high, hd, max, or auto; "
            f"got {quality}",
            "invalid_quality",
        )
    return QUALITY_MAX_MODE[key]


def _mime_from_bytes(data: bytes, fallback: str = "image/png") -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def _decode_image_data(raw: str) -> bytes:
    text = raw.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except Exception as exc:
        raise GenerateImageError(400, "reference image is not valid base64", "invalid_image") from exc


def load_reference_image(item: Union[str, Path, bytes, dict[str, Any]]) -> dict[str, str]:
    if isinstance(item, dict):
        mime = str(item.get("mime_type") or item.get("mimeType") or "").strip()
        if item.get("path"):
            loaded = load_reference_image(item["path"])
            if mime:
                loaded["mime_type"] = mime
            return loaded
        raw = item.get("data") or item.get("b64_json") or item.get("image") or ""
        data = _decode_image_data(str(raw))
        if not data:
            raise GenerateImageError(400, "reference image is empty", "invalid_image")
        if len(data) > REF_MAX_BYTES:
            raise GenerateImageError(400, "reference image is too large", "invalid_image")
        return {"data": base64.b64encode(data).decode("ascii"), "mime_type": mime or _mime_from_bytes(data)}
    if isinstance(item, (bytes, bytearray)):
        data = bytes(item)
        if not data:
            raise GenerateImageError(400, "reference image is empty", "invalid_image")
        if len(data) > REF_MAX_BYTES:
            raise GenerateImageError(400, "reference image is too large", "invalid_image")
        return {"data": base64.b64encode(data).decode("ascii"), "mime_type": _mime_from_bytes(data)}
    text = str(item).strip()
    if not text:
        raise GenerateImageError(400, "reference image is empty", "invalid_image")
    path = Path(text).expanduser()
    if path.is_file():
        data = path.read_bytes()
        if not data:
            raise GenerateImageError(400, f"reference image is empty: {path}", "invalid_image")
        if len(data) > REF_MAX_BYTES:
            raise GenerateImageError(400, f"reference image is too large: {path}", "invalid_image")
        mime = MIME_BY_EXT.get(path.suffix.lower()) or _mime_from_bytes(data)
        return {"data": base64.b64encode(data).decode("ascii"), "mime_type": mime}
    if text.startswith("data:") or re.fullmatch(r"[A-Za-z0-9+/=\s]+", text) is not None:
        data = _decode_image_data(text)
        if not data:
            raise GenerateImageError(400, "reference image is empty", "invalid_image")
        if len(data) > REF_MAX_BYTES:
            raise GenerateImageError(400, "reference image is too large", "invalid_image")
        return {"data": base64.b64encode(data).decode("ascii"), "mime_type": _mime_from_bytes(data)}
    raise GenerateImageError(400, f"reference image not found: {path}", "invalid_image")


def load_reference_images(items: Optional[Iterable[Any]]) -> list[dict[str, str]]:
    if items is None:
        return []
    if isinstance(items, (str, bytes, Path, dict)):
        items = [items]
    loaded = []
    for item in items:
        if item is None or item == "":
            continue
        loaded.append(load_reference_image(item))
    return loaded


def _vscdb_session_token() -> str:
    store = P._read_vscdb(("cursorAuth/accessToken",))
    token = store.get("cursorAuth/accessToken") or ""
    if not token:
        raise GenerateImageError(401, "Cursor state.vscdb 里没有 accessToken", "not_logged_in")
    return token


def _build_request(
    prompt: str,
    model: str,
    max_mode: bool,
    aspect_ratio: Optional[str] = None,
    reference_images: Optional[Sequence[dict[str, str]]] = None,
) -> bytes:
    body = P.pb_str(1, prompt)
    for image in reference_images or ():
        inner = P.pb_str(1, image["data"])
        if image.get("mime_type"):
            inner += P.pb_str(2, image["mime_type"])
        body += P.pb_bytes(2, inner)
    if model:
        body += P.pb_str(3, model)
    if max_mode:
        body += P.pb_bool(4, True)
    if aspect_ratio:
        body += P.pb_str(5, aspect_ratio)
    return body


def _headers(token: str, machine_id: str, mac_machine_id: str) -> dict[str, str]:
    return {
        "content-type": "application/proto",
        "connect-protocol-version": "1",
        "authorization": f"Bearer {token}",
        "x-cursor-checksum": P.cursor_checksum(machine_id, mac_machine_id),
        "x-cursor-client-type": "sand",
        "x-cursor-client-source": "sand-desktop",
        "x-cursor-client-version": "0.46.0",
        "x-sand-box-namespace": "prod",
        "x-cursor-client-device-type": "desktop",
        "x-ghost-mode": "true",
        "x-request-id": str(uuid.uuid4()),
        "user-agent": "connect-es/1.6.1",
    }


def _parse_success_or_error(raw: bytes) -> dict[str, Any]:
    pos = 0
    while pos < len(raw):
        key, pos = P._read_varint(raw, pos)
        field_no, wire = key >> 3, key & 7
        if wire != 2:
            if wire == 0:
                _, pos = P._read_varint(raw, pos)
                continue
            break
        length, pos = P._read_varint(raw, pos)
        inner = raw[pos : pos + length]
        pos += length
        if field_no == 1:
            mime, data = "", b""
            ip = 0
            while ip < len(inner):
                k2, ip = P._read_varint(inner, ip)
                f2, w2 = k2 >> 3, k2 & 7
                if w2 == 2:
                    n2, ip = P._read_varint(inner, ip)
                    value = inner[ip : ip + n2]
                    ip += n2
                    if f2 == 1:
                        data = value
                    elif f2 == 2:
                        mime = value.decode("utf-8", "replace")
                elif w2 == 0:
                    _, ip = P._read_varint(inner, ip)
                else:
                    break
            decoded = data
            if data[:1] not in (b"\xff", b"\x89") and b"/" in data[:16]:
                try:
                    decoded = base64.b64decode(data)
                except Exception:
                    decoded = data
            kind = "bin"
            if decoded.startswith(b"\xff\xd8"):
                kind = "jpeg"
            elif decoded.startswith(b"\x89PNG"):
                kind = "png"
            return {
                "case": "success",
                "mime": mime,
                "kind": kind,
                "bytes": decoded,
            }
        if field_no == 2:
            message, restricted = "", False
            ip = 0
            while ip < len(inner):
                k2, ip = P._read_varint(inner, ip)
                f2, w2 = k2 >> 3, k2 & 7
                if w2 == 2:
                    n2, ip = P._read_varint(inner, ip)
                    value = inner[ip : ip + n2]
                    ip += n2
                    if f2 == 1:
                        message = _scrub(value.decode("utf-8", "replace"))
                elif w2 == 0:
                    val, ip = P._read_varint(inner, ip)
                    if f2 == 2:
                        restricted = bool(val)
                else:
                    break
            return {
                "case": "error",
                "message": message or "generate image failed",
                "model_restricted": restricted,
            }
    return {"case": "empty"}


def _raise_from_http(status: int, raw: bytes, content_type: str) -> None:
    text = raw.decode("utf-8", "replace")
    codes = ERROR_RE.findall(text)
    message = _scrub(text[:240])
    try:
        obj = json.loads(text)
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            message = _scrub(str(err.get("message") or err.get("code") or message))
            details = err.get("details")
            if isinstance(details, list) and details:
                debug = details[0].get("debug") if isinstance(details[0], dict) else {}
                if isinstance(debug, dict) and debug.get("error"):
                    codes.append(str(debug["error"]))
                nested = debug.get("details") if isinstance(debug, dict) else None
                if isinstance(nested, dict) and nested.get("detail"):
                    message = _scrub(str(nested["detail"]))
    except json.JSONDecodeError:
        pass
    code = codes[0] if codes else ""
    raise GenerateImageError(status, message or f"HTTP {status} {content_type}", code)


def generate_image(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    max_mode: bool = True,
    quality: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    reference_images: Optional[Iterable[Any]] = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    ratio = normalize_aspect_ratio(aspect_ratio)
    use_max_mode = quality_to_max_mode(quality, max_mode)
    refs = load_reference_images(reference_images)
    token = _vscdb_session_token()
    machine_id, mac_machine_id = P._inherit_cursor_identity()
    url = P.BACKEND.rstrip("/") + RPC
    body = _build_request(prompt, model, use_max_mode, ratio, refs)
    headers = _headers(token, machine_id, mac_machine_id)
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0), http2=False) as client:
        response = client.post(url, content=body, headers=headers)
    content_type = (response.headers.get("content-type") or "").split(";")[0]
    if response.status_code != 200 or content_type.startswith("application/json"):
        _raise_from_http(response.status_code, response.content, content_type)
    parsed = _parse_success_or_error(response.content)
    if parsed.get("case") != "success" or not parsed.get("bytes"):
        if parsed.get("case") == "error":
            raise GenerateImageError(
                400 if parsed.get("model_restricted") else 502,
                str(parsed.get("message") or "generate image failed"),
                "model_restricted" if parsed.get("model_restricted") else "upstream_error",
            )
        raise GenerateImageError(502, "empty generate-image response", "empty_response")
    parsed["width"], parsed["height"] = _image_size(parsed["bytes"])
    return parsed


def _image_size(data: bytes) -> tuple[Optional[int], Optional[int]]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not data.startswith(b"\xff\xd8"):
        return None, None
    pos = 2
    while pos + 9 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")
            return width, height
        if marker == 0xD8 or marker == 0xD9:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        pos += 2 + length
    return None, None


def _default_output(kind: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = ".jpg" if kind == "jpeg" else ".png" if kind == "png" else ".bin"
    folder = Path.cwd() / "generated"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"image-{stamp}{suffix}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="本地调用 RunGenerateImage，图存到本机")
    parser.add_argument("prompt", help="出图描述")
    parser.add_argument("-o", "--output", help="输出路径，默认 generated/image-<时间>.jpg")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="透传 model_id，出图引擎通常会忽略")
    parser.add_argument(
        "-a",
        "--aspect",
        help="画幅比例：1:1、4:3、3:4、16:9、9:16，或 1024x1536 / 竖版",
    )
    parser.add_argument(
        "--max-mode",
        dest="max_mode",
        action="store_true",
        default=True,
        help="高质量（官方 max_mode，默认开）",
    )
    parser.add_argument(
        "--no-max-mode",
        dest="max_mode",
        action="store_false",
        help="关闭 max_mode",
    )
    parser.add_argument(
        "--quality",
        help="OpenAI 风格质量：low/medium/high/hd/max/auto（映射到 max_mode）",
    )
    parser.add_argument(
        "--ref",
        action="append",
        default=[],
        help="参考图路径，可重复。对应官方 reference_images",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="HTTP 超时秒数")
    args = parser.parse_args(argv)
    try:
        result = generate_image(
            args.prompt,
            model=args.model,
            max_mode=args.max_mode,
            quality=args.quality,
            aspect_ratio=args.aspect,
            reference_images=args.ref or None,
            timeout=args.timeout,
        )
    except GenerateImageError as exc:
        print(f"失败 HTTP {exc.status} {exc.code}: {exc}", file=sys.stderr)
        return 1
    path = Path(args.output) if args.output else _default_output(str(result["kind"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result["bytes"])
    width, height = result.get("width"), result.get("height")
    extra = f"  {width}x{height}" if width and height else ""
    ratio = normalize_aspect_ratio(args.aspect)
    if ratio:
        extra += f"  aspect={ratio}"
        if width and height:
            actual = _snap_aspect(int(width), int(height))
            if actual != ratio:
                extra += f"  (upstream still {actual}; field sent but ignored)"
    extra += f"  max_mode={str(quality_to_max_mode(args.quality, args.max_mode)).lower()}"
    if args.ref:
        extra += f"  refs={len(args.ref)}"
    print(f"{path}  {result['kind']}  {len(result['bytes'])} bytes  mime={result['mime']}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
