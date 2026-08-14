"""Authentication, invitation, and in-memory abuse controls."""

import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import deque

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,24}$")
PASSWORD_ITERATIONS = 600_000


class RateLimiter:
    """Small, memory-bounded limiter that stores no durable client history."""

    def __init__(self, attempts=8, window=300, max_entries=4096, max_key_bytes=256):
        self.attempts = attempts
        self.window = window
        self.max_entries = max_entries
        self.max_key_bytes = max_key_bytes
        self._entries = {}
        self._last_sweep = 0
        self._lock = threading.Lock()

    def allow(self, key, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        if not isinstance(key, str) or len(key.encode("utf-8")) > self.max_key_bytes:
            return False
        with self._lock:
            if timestamp - self._last_sweep >= self.window:
                self._sweep(timestamp)
            if key not in self._entries and len(self._entries) >= self.max_entries:
                # A flood within one live window cannot allocate past the fixed
                # ceiling. Expired entries get one eager chance to make room.
                self._sweep(timestamp)
                if len(self._entries) >= self.max_entries:
                    return False
            recent = self._entries.get(key, deque())
            cutoff = timestamp - self.window
            while recent and recent[0] <= cutoff:
                recent.popleft()
            if len(recent) >= self.attempts:
                self._entries[key] = recent
                return False
            recent.append(timestamp)
            self._entries[key] = recent
            return True

    def _sweep(self, timestamp):
        """Drop fully expired keys.

        Keys embed caller-supplied values such as attempted usernames, so
        without this an attacker could grow the table without bound simply by
        varying the username on failed sign-in attempts.
        """
        cutoff = timestamp - self.window
        for key in [key for key, seen in self._entries.items() if not seen or seen[-1] <= cutoff]:
            del self._entries[key]
        self._last_sweep = timestamp

    def clear(self, key):
        with self._lock:
            self._entries.pop(key, None)


AUTH_LIMITER = RateLimiter()
# Every authentication request first spends from a key containing only the
# operation and a parsed network address. Account keys are a second layer, not
# a substitute: an attacker must never allocate memory by inventing usernames.
AUTH_IP_LIMITER = RateLimiter(attempts=30, window=300)
UPLOAD_LIMITER = RateLimiter(attempts=20, window=60)
# Token minting is cheap but writes a lease each time. A compromised member
# must not be able to turn that narrow endpoint into an unbounded SQLite writer.
MEDIA_TOKEN_LIMITER = RateLimiter(attempts=12, window=60)


def parse_address(raw):
    """Return an IP address from a header hop, or None when it is not one."""
    raw = (raw or "").strip().strip("[]")
    for candidate in (raw, raw.rsplit(":", 1)[0]):
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            continue
    return None


def is_trusted_proxy(raw, trusted_proxies):
    address = parse_address(raw)
    return address is not None and any(address in network for network in trusted_proxies)


def client_address(peer, forwarded_for, trusted_proxies):
    """Resolve the address to rate-limit by.

    Behind a reverse proxy every request arrives from the proxy, so limiting by
    peer address would put every user in one bucket and let one attacker lock
    out the instance. `X-Forwarded-For` fixes that but is attacker-controlled,
    so it is consulted only when the peer is a proxy the operator named.

    The header is walked from the right, discarding hops that are themselves
    trusted proxies: a client can prepend anything it likes, but it cannot
    forge the entry our own proxy appends.
    """
    if not peer:
        return "unknown"
    if not is_trusted_proxy(peer, trusted_proxies):
        return peer
    for hop in reversed((forwarded_for or "").split(",")):
        if parse_address(hop) is None:
            continue
        if not is_trusted_proxy(hop, trusted_proxies):
            return hop.strip()
    return peer


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


# A missing account still performs one real password comparison. This keeps the
# response path close to an existing account without storing a fake account or
# deriving a new hash for every hostile request.
DUMMY_PASSWORD_HASH = password_hash("not an account password")


def password_matches(password, encoded):
    try:
        if encoded.startswith("pbkdf2_sha256$"):
            _, iterations, salt_hex, expected = encoded.split("$", 3)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
            actual = digest.hex()
        else:
            salt_hex, expected = encoded.split(":", 1)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 210_000).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def invite_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def valid_invite(database, token):
    if not token:
        return None
    return database.execute("""
      SELECT i.id,i.community_id,c.name community_name FROM invitations i
      JOIN communities c ON c.id=i.community_id
      WHERE i.token_hash=? AND i.expires_at>? AND i.uses<i.max_uses
    """, (invite_hash(token), int(time.time()))).fetchone()
