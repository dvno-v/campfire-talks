"""Environment-backed Campfire configuration."""

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    """A configuration that Campfire refuses to serve with."""


def _networks(raw):
    """Parse a comma-separated list of trusted proxy addresses or CIDR ranges."""
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if entry:
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError as failure:
                raise ConfigError(
                    f"CAMPFIRE_TRUSTED_PROXIES contains an invalid address or range: {entry}"
                ) from failure
            if network.prefixlen == 0:
                raise ConfigError("CAMPFIRE_TRUSTED_PROXIES cannot trust the entire internet")
            networks.append(network)
    return tuple(networks)


def _integer(name, default, minimum=None, maximum=None):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as failure:
        raise ConfigError(f"{name} must be a whole number") from failure
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return value


def _boolean(name, default=False):
    raw = os.environ.get(name, "1" if default else "0")
    if raw not in {"0", "1"}:
        raise ConfigError(f"{name} must be 0 or 1")
    return raw == "1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DB_PATH = Path(os.environ.get("CAMPFIRE_DB", PROJECT_ROOT / "data" / "campfire.db"))
UPLOAD_DIR = Path(os.environ.get("CAMPFIRE_UPLOAD_DIR", PROJECT_ROOT / "data" / "uploads"))
MAX_UPLOAD_BYTES = _integer("CAMPFIRE_MAX_UPLOAD_BYTES", 8 * 1024 * 1024, 1)
# Zero means no ceiling, which stays the default: imposing one on an instance
# that never asked for it would start refusing uploads that used to work.
MAX_STORAGE_BYTES = _integer("CAMPFIRE_MAX_STORAGE_BYTES", 0, 0)
# Warn before either Campfire's image ceiling or the filesystem itself is full.
# This is deliberately a percentage rather than a byte reserve: the same
# default remains useful on a small home server and a larger dedicated volume.
STORAGE_WARNING_PERCENT = _integer("CAMPFIRE_STORAGE_WARNING_PERCENT", 90, 1, 99)
SECURE_COOKIES = _boolean("CAMPFIRE_SECURE_COOKIES")
PUBLIC_ORIGIN = os.environ.get("CAMPFIRE_ORIGIN", "")
ACCESS_LOGS = _boolean("CAMPFIRE_ACCESS_LOG")
HOST = os.environ.get("CAMPFIRE_HOST", "127.0.0.1")
PORT = _integer("CAMPFIRE_PORT", 8000, 1, 65535)
# Empty by default: forwarded client addresses are trusted only where an
# operator has named the proxy that sets them.
TRUSTED_PROXIES = _networks(os.environ.get("CAMPFIRE_TRUSTED_PROXIES", ""))
MAX_EVENT_STREAMS = _integer("CAMPFIRE_MAX_EVENT_STREAMS", 32, 1)
# Retention is measured in days, so sweeping hourly is prompt enough while
# keeping the work off the request path entirely.
RETENTION_SWEEP_SECONDS = _integer("CAMPFIRE_RETENTION_SWEEP_SECONDS", 3600, 60)
MAX_EVENT_STREAMS_PER_USER = _integer("CAMPFIRE_MAX_EVENT_STREAMS_PER_USER", 4, 1)
MAX_CONCURRENT_REQUESTS = _integer("CAMPFIRE_MAX_CONCURRENT_REQUESTS", 64, 4, 1024)
REQUEST_WORKERS = _integer("CAMPFIRE_REQUEST_WORKERS", 64, 4, 1024)
KEEPALIVE_TIMEOUT_SECONDS = _integer("CAMPFIRE_KEEPALIVE_TIMEOUT_SECONDS", 5, 1, 60)


def _is_loopback(host):
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_configuration():
    """Fail closed when a network-reachable deployment lacks public safeguards."""
    errors = []
    resolved_database = DB_PATH.resolve()
    resolved_uploads = UPLOAD_DIR.resolve()
    if (resolved_database == resolved_uploads
            or resolved_database in resolved_uploads.parents
            or resolved_uploads in resolved_database.parents):
        errors.append("CAMPFIRE_DB and CAMPFIRE_UPLOAD_DIR must be separate, non-nested paths")
    origin_host = None
    if PUBLIC_ORIGIN:
        parsed = urlparse(PUBLIC_ORIGIN)
        origin_host = parsed.hostname
        try:
            parsed.port
        except ValueError:
            errors.append("CAMPFIRE_ORIGIN contains an invalid port")
        if (parsed.scheme not in {"http", "https"} or not origin_host
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.params or parsed.query or parsed.fragment
                or PUBLIC_ORIGIN.endswith("/")):
            errors.append("CAMPFIRE_ORIGIN must be an origin such as https://chat.example.net with no trailing slash")
        if parsed.scheme != "https" and not (origin_host and _is_loopback(origin_host)):
            errors.append("CAMPFIRE_ORIGIN must use HTTPS unless its host is loopback")
        if parsed.scheme == "https" and not SECURE_COOKIES:
            errors.append("CAMPFIRE_SECURE_COOKIES=1 is required for an HTTPS origin")
    elif not _is_loopback(HOST):
        errors.append("CAMPFIRE_ORIGIN is required when CAMPFIRE_HOST is network-reachable")

    if SECURE_COOKIES and (not PUBLIC_ORIGIN or not PUBLIC_ORIGIN.startswith("https://")):
        errors.append("secure cookies require an explicit HTTPS CAMPFIRE_ORIGIN")
    if TRUSTED_PROXIES and not PUBLIC_ORIGIN:
        errors.append("CAMPFIRE_TRUSTED_PROXIES requires an explicit CAMPFIRE_ORIGIN")
    if not _is_loopback(HOST):
        if not TRUSTED_PROXIES:
            errors.append("CAMPFIRE_TRUSTED_PROXIES is required for a network-reachable listener")
        if not MAX_STORAGE_BYTES:
            errors.append("CAMPFIRE_MAX_STORAGE_BYTES must be set for a network-reachable listener")
    if MAX_EVENT_STREAMS_PER_USER > MAX_EVENT_STREAMS:
        errors.append("CAMPFIRE_MAX_EVENT_STREAMS_PER_USER cannot exceed CAMPFIRE_MAX_EVENT_STREAMS")
    if MAX_EVENT_STREAMS >= REQUEST_WORKERS:
        errors.append("CAMPFIRE_MAX_EVENT_STREAMS must leave at least one CAMPFIRE_REQUEST_WORKERS slot")
    if REQUEST_WORKERS > MAX_CONCURRENT_REQUESTS:
        errors.append("CAMPFIRE_REQUEST_WORKERS cannot exceed CAMPFIRE_MAX_CONCURRENT_REQUESTS")
    if errors:
        raise ConfigError("Unsafe Campfire configuration:\n- " + "\n- ".join(errors))
    return True
