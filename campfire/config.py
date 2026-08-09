"""Environment-backed Campfire configuration."""

import ipaddress
import os
from pathlib import Path


def _networks(raw):
    """Parse a comma-separated list of trusted proxy addresses or CIDR ranges."""
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if entry:
            networks.append(ipaddress.ip_network(entry, strict=False))
    return tuple(networks)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DB_PATH = Path(os.environ.get("CAMPFIRE_DB", PROJECT_ROOT / "data" / "campfire.db"))
UPLOAD_DIR = Path(os.environ.get("CAMPFIRE_UPLOAD_DIR", PROJECT_ROOT / "data" / "uploads"))
MAX_UPLOAD_BYTES = max(1, int(os.environ.get("CAMPFIRE_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))))
# Zero means no ceiling, which stays the default: imposing one on an instance
# that never asked for it would start refusing uploads that used to work.
MAX_STORAGE_BYTES = max(0, int(os.environ.get("CAMPFIRE_MAX_STORAGE_BYTES", "0")))
SECURE_COOKIES = os.environ.get("CAMPFIRE_SECURE_COOKIES", "0") == "1"
PUBLIC_ORIGIN = os.environ.get("CAMPFIRE_ORIGIN", "").rstrip("/")
ACCESS_LOGS = os.environ.get("CAMPFIRE_ACCESS_LOG", "0") == "1"
HOST = os.environ.get("CAMPFIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAMPFIRE_PORT", "8000"))
# Empty by default: forwarded client addresses are trusted only where an
# operator has named the proxy that sets them.
TRUSTED_PROXIES = _networks(os.environ.get("CAMPFIRE_TRUSTED_PROXIES", ""))
MAX_EVENT_STREAMS = max(1, int(os.environ.get("CAMPFIRE_MAX_EVENT_STREAMS", "200")))
# Retention is measured in days, so sweeping hourly is prompt enough while
# keeping the work off the request path entirely.
RETENTION_SWEEP_SECONDS = max(60, int(os.environ.get("CAMPFIRE_RETENTION_SWEEP_SECONDS", "3600")))
MAX_EVENT_STREAMS_PER_USER = max(1, int(os.environ.get("CAMPFIRE_MAX_EVENT_STREAMS_PER_USER", "8")))
