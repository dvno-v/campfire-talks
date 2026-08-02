"""Authentication, invitation, and in-memory abuse controls."""

import hashlib
import hmac
import re
import secrets
import threading
import time

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,24}$")
PASSWORD_ITERATIONS = 600_000


class RateLimiter:
    """Small limiter that intentionally stores no durable client history."""

    def __init__(self, attempts=8, window=300):
        self.attempts = attempts
        self.window = window
        self._entries = {}
        self._last_sweep = 0
        self._lock = threading.Lock()

    def allow(self, key, timestamp=None):
        timestamp = timestamp or time.time()
        with self._lock:
            if timestamp - self._last_sweep >= self.window:
                self._sweep(timestamp)
            recent = [seen for seen in self._entries.get(key, []) if seen > timestamp - self.window]
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
        for key in [key for key, seen in self._entries.items() if not seen or max(seen) <= cutoff]:
            del self._entries[key]
        self._last_sweep = timestamp

    def clear(self, key):
        with self._lock:
            self._entries.pop(key, None)


AUTH_LIMITER = RateLimiter()
UPLOAD_LIMITER = RateLimiter(attempts=20, window=60)


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


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
