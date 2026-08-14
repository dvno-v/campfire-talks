"""Small, actor-authorized LiveKit join grants and expiring room leases.

Campfire never receives the media encryption key. It receives only its SHA-256
fingerprint, which lets an occupied room reject an unrelated key rather than
creating two groups that can signal each other but cannot decrypt each other.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from ..database import utc_now


KEY_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
LEASE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
JOIN_TOKEN_SECONDS = 120
# A place nobody has heartbeated yet is held for this long rather than for a
# whole lease, so a browser that dies between the grant and its first heartbeat
# stops holding the room's key against everybody else within seconds. It stays
# below the smallest configurable lease, which also makes the remaining time on
# a place a usable signal: only a recent heartbeat can push it above this.
UNCONFIRMED_LEASE_SECONDS = 20


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(content):
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def issue_join_token(api_key, api_secret, room, user_id, username,
                     key_fingerprint, timestamp=None):
    """Issue the narrow LiveKit JWT understood by the self-hosted SFU."""
    now = int(timestamp if timestamp is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": api_key,
        "sub": f"user-{int(user_id)}",
        "name": username,
        "nbf": now - 5,
        "exp": now + JOIN_TOKEN_SECONDS,
        "jti": secrets.token_urlsafe(16),
        "metadata": json.dumps({"keyFingerprint": key_fingerprint},
                               separators=(",", ":")),
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": False,
            "canPublishSources": ["microphone", "screen_share", "screen_share_audio"],
        },
    }
    encoded = ".".join((_b64url(json.dumps(header, separators=(",", ":")).encode()),
                        _b64url(json.dumps(claims, separators=(",", ":")).encode())))
    signature = hmac.new(api_secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64url(signature)}"


def claim_lease(database, channel_id, user_id, key_fingerprint,
                participant_limit, lease_seconds, timestamp=None):
    """Atomically authorize and reserve one participant place.

    Returns ``(status, payload)`` where status is deliberately coarse enough
    that a non-member cannot distinguish a missing channel from a private one.
    """
    if not KEY_FINGERPRINT_RE.fullmatch(key_fingerprint):
        return "invalid_key", None
    now = int(timestamp if timestamp is not None else time.time())
    database.commit()
    database.execute("BEGIN IMMEDIATE")
    database.execute("DELETE FROM voice_leases WHERE expires_at<=?", (now,))
    channel = database.execute("""
      SELECT ch.id,ch.community_id,ch.kind
      FROM channels ch JOIN memberships m ON m.community_id=ch.community_id
      WHERE ch.id=? AND m.user_id=?
    """, (channel_id, user_id)).fetchone()
    if not channel:
        database.rollback()
        return "forbidden", None
    if channel["kind"] != "voice":
        database.rollback()
        return "not_voice", None

    # Everyone in a room shares one key by construction, so the first place
    # answers the fingerprint question for all of them.
    others = database.execute("""
      SELECT key_fingerprint,expires_at FROM voice_leases
      WHERE channel_id=? AND user_id<>?
    """, (channel_id, user_id)).fetchall()
    unconfirmed_seconds = min(UNCONFIRMED_LEASE_SECONDS, lease_seconds)
    if others and not hmac.compare_digest(others[0]["key_fingerprint"], key_fingerprint):
        database.rollback()
        # Refusing without saying why leaves the caller with one useless button
        # to press again. A place still counts as live only while its remaining
        # time reflects a recent heartbeat, so an abandoned room reports itself
        # as settling instead of claiming to be occupied. Neither number tells
        # the caller anything it could not learn by retrying.
        return "key_mismatch", {
            "participants": len(others),
            "live": sum(1 for row in others
                        if row["expires_at"] > now + unconfirmed_seconds),
            "retry_after": max(row["expires_at"] for row in others) - now,
        }
    if len(others) >= participant_limit:
        database.rollback()
        return "full", None

    token = secrets.token_urlsafe(32)
    # Short until the client proves it is still there; `renew_lease` promotes
    # the place to the full window on the first heartbeat.
    expires_at = now + unconfirmed_seconds
    database.execute("""
      INSERT INTO voice_leases
        (channel_id,user_id,token_hash,key_fingerprint,expires_at,created_at)
      VALUES(?,?,?,?,?,?)
      ON CONFLICT(channel_id,user_id) DO UPDATE SET
        token_hash=excluded.token_hash,key_fingerprint=excluded.key_fingerprint,
        expires_at=excluded.expires_at,created_at=excluded.created_at
    """, (channel_id, user_id, _digest(token), key_fingerprint,
          expires_at, utc_now()))
    database.commit()
    return "ok", {"lease": token, "lease_expires_at": expires_at,
                  "participants": len(others) + 1}


def renew_lease(database, channel_id, user_id, token, lease_seconds, timestamp=None):
    if not LEASE_TOKEN_RE.fullmatch(token):
        return False
    now = int(timestamp if timestamp is not None else time.time())
    database.execute("DELETE FROM voice_leases WHERE expires_at<=?", (now,))
    updated = database.execute("""
      UPDATE voice_leases SET expires_at=?
      WHERE channel_id=? AND user_id=? AND token_hash=?
        AND EXISTS (
          SELECT 1 FROM channels ch JOIN memberships m ON m.community_id=ch.community_id
          WHERE ch.id=voice_leases.channel_id AND ch.kind='voice' AND m.user_id=voice_leases.user_id)
    """, (now + lease_seconds, channel_id, user_id, _digest(token)))
    return updated.rowcount == 1


def release_lease(database, channel_id, user_id, token):
    if not LEASE_TOKEN_RE.fullmatch(token):
        return False
    deleted = database.execute("""
      DELETE FROM voice_leases WHERE channel_id=? AND user_id=? AND token_hash=?
    """, (channel_id, user_id, _digest(token)))
    return deleted.rowcount == 1
