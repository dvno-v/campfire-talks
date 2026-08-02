"""Environment-backed Campfire configuration."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DB_PATH = Path(os.environ.get("CAMPFIRE_DB", PROJECT_ROOT / "data" / "campfire.db"))
UPLOAD_DIR = Path(os.environ.get("CAMPFIRE_UPLOAD_DIR", PROJECT_ROOT / "data" / "uploads"))
MAX_UPLOAD_BYTES = max(1, int(os.environ.get("CAMPFIRE_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))))
SECURE_COOKIES = os.environ.get("CAMPFIRE_SECURE_COOKIES", "0") == "1"
PUBLIC_ORIGIN = os.environ.get("CAMPFIRE_ORIGIN", "").rstrip("/")
ACCESS_LOGS = os.environ.get("CAMPFIRE_ACCESS_LOG", "0") == "1"
HOST = os.environ.get("CAMPFIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAMPFIRE_PORT", "8000"))
