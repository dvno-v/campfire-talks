"""Environment-backed Campfire configuration."""

import ipaddress
import os
import re
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


def _secret(name):
    """Read a secret from a mounted file, falling back to a direct variable.

    Compose uses the file form so the API secret is absent from the Campfire
    container's inspectable environment. Native development may use the direct
    value for convenience.
    """
    filename = os.environ.get(f"{name}_FILE", "").strip()
    if not filename:
        return os.environ.get(name, "").strip()
    try:
        value = Path(filename).read_text(encoding="utf-8").strip()
    except OSError as failure:
        raise ConfigError(f"{name}_FILE could not be read") from failure
    if not value:
        raise ConfigError(f"{name}_FILE is empty")
    return value


def _livekit_secret(value, api_key, is_key_file):
    """Parse the exact one-entry key file shared with LiveKit.

    Keeping this deliberately narrower than general YAML prevents a typo,
    second mapping, comment, or quoted value from passing Campfire's startup
    checks while producing tokens LiveKit can never verify.
    """
    if not is_key_file:
        return value, False
    match = re.fullmatch(
        rf"{re.escape(api_key)}: ([0-9a-f]{{64}})" if api_key else r"(?!)", value)
    return (match.group(1), False) if match else ("", True)

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
MEDIA_URL = os.environ.get("CAMPFIRE_MEDIA_URL", "").strip()
LIVEKIT_API_KEY = os.environ.get("CAMPFIRE_LIVEKIT_API_KEY", "").strip()
_LIVEKIT_SECRET_FILE_VALUE = _secret("CAMPFIRE_LIVEKIT_API_SECRET")
_LIVEKIT_USES_KEY_FILE = bool(
    os.environ.get("CAMPFIRE_LIVEKIT_API_SECRET_FILE", "").strip())
# LiveKit's key_file is a tiny YAML mapping. The Compose deployment mounts the
# same one-line file into both services, so there is one secret to rotate and
# no secret in either container's inspectable environment. A raw value remains
# supported for native development.
LIVEKIT_API_SECRET, _LIVEKIT_KEY_FILE_INVALID = _livekit_secret(
    _LIVEKIT_SECRET_FILE_VALUE, LIVEKIT_API_KEY, _LIVEKIT_USES_KEY_FILE)
MAX_VOICE_PARTICIPANTS = _integer("CAMPFIRE_MAX_VOICE_PARTICIPANTS", 8, 2, 25)
# A lease is how long a crashed browser keeps holding a room's encryption key
# against everybody else, so it is deliberately close to the heartbeat interval
# rather than generous: three missed beats, not nine. Raising it only lengthens
# that lockout; it buys no reliability the retries do not already provide.
VOICE_LEASE_SECONDS = _integer("CAMPFIRE_VOICE_LEASE_SECONDS", 45, 30, 300)


def _is_loopback(host):
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _valid_origin_host(host):
    """Accept an IP literal or a conservative ASCII DNS hostname."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host.rstrip(".").split(".")
        return bool(labels) and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels)


def _same_host(first, second):
    return bool(first and second and first.rstrip(".").casefold() == second.rstrip(".").casefold())


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
    media_values = (MEDIA_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    if any(media_values) and not all(media_values):
        errors.append("CAMPFIRE_MEDIA_URL, CAMPFIRE_LIVEKIT_API_KEY, and its API secret must be set together")
    if _LIVEKIT_KEY_FILE_INVALID:
        errors.append("CAMPFIRE_LIVEKIT_API_SECRET_FILE must contain exactly 'API_KEY: 64-lowercase-hex-secret'")
    if MEDIA_URL:
        try:
            media = urlparse(MEDIA_URL)
            media_host = media.hostname
            media.port
            invalid_port = False
        except ValueError:
            media = urlparse("")
            media_host = None
            invalid_port = True
        if (any(character.isspace() for character in MEDIA_URL)
                or media.scheme not in {"ws", "wss"} or not _valid_origin_host(media_host)
                or invalid_port or media.username
                or media.password or media.path not in {"", "/"} or media.params
                or media.query or media.fragment or MEDIA_URL.endswith("/")):
            errors.append("CAMPFIRE_MEDIA_URL must be an origin such as wss://media.example.net with no trailing slash")
        elif media.scheme != "wss" and not _is_loopback(media_host):
            errors.append("CAMPFIRE_MEDIA_URL must use WSS unless its host is loopback")
        origin_media_host = urlparse(PUBLIC_ORIGIN).hostname if PUBLIC_ORIGIN else None
        if (_same_host(origin_media_host, media_host)
                and not (_is_loopback(origin_media_host) and _is_loopback(media_host))):
            errors.append("CAMPFIRE_MEDIA_URL must use a separate hostname from CAMPFIRE_ORIGIN outside loopback development")
    if LIVEKIT_API_KEY and not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", LIVEKIT_API_KEY):
        errors.append("CAMPFIRE_LIVEKIT_API_KEY must be 3–64 letters, numbers, underscores, or hyphens")
    if LIVEKIT_API_SECRET and len(LIVEKIT_API_SECRET) < 32:
        errors.append("CAMPFIRE_LIVEKIT_API_SECRET must contain at least 32 characters")
    if errors:
        raise ConfigError("Unsafe Campfire configuration:\n- " + "\n- ".join(errors))
    return True
