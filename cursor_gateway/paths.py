"""路径与运行环境：仓库根、数据目录、前端产物。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
REPO_ROOT = PROJECT_DIR.parent


def ensure_sys_path() -> None:
    extra = os.environ.get("CURSOR_GATEWAY_REPO_ROOT")
    roots = [
        str(PROJECT_DIR),
        str(PROJECT_DIR / "vendor" / "sand"),
        str(REPO_ROOT),
    ]
    if extra:
        roots.insert(0, str(Path(extra).expanduser().resolve()))
    for item in roots:
        if item and item not in sys.path:
            sys.path.insert(0, item)


def data_dir() -> Path:
    override = os.environ.get("CURSOR_GATEWAY_HOME")
    base = Path(override).expanduser() if override else PROJECT_DIR / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    override = os.environ.get("CURSOR_GATEWAY_DB")
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return data_dir() / "gateway.db"


def web_dist() -> Path:
    return PROJECT_DIR / "web" / "dist"


def legacy_settings_path() -> Path:
    override = os.environ.get("SAND_GATEWAY_CONFIG")
    if override:
        return Path(override).expanduser()
    return REPO_ROOT / "sand_gateway" / "settings.json"
