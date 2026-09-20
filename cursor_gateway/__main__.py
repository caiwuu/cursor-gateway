"""python -m cursor_gateway 启动入口。"""

from __future__ import annotations

import os

from .paths import ensure_sys_path

ensure_sys_path()


def main() -> None:
    import uvicorn

    from .store import Store

    store = Store()
    settings = store.get_app_settings()
    store.close()
    host = os.environ.get("CURSOR_GATEWAY_HOST") or settings.host or "0.0.0.0"
    port = int(os.environ.get("CURSOR_GATEWAY_PORT") or settings.port or 8788)
    uvicorn.run("cursor_gateway.server:app", host=host, port=port)


if __name__ == "__main__":
    main()
