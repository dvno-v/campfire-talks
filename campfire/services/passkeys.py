"""WebAuthn passkey registration, authentication, and credential inventory."""

from __future__ import annotations

import json
import secrets
import time

from webauthn import base64url_to_bytes, generate_authentication_options
from webauthn import generate_registration_options, options_to_json
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers.structs import AuthenticatorSelectionCriteria
from webauthn.helpers.structs import PublicKeyCredentialDescriptor
from webauthn.helpers.structs import ResidentKeyRequirement, UserVerificationRequirement

from ..database import utc_now
from ..security import password_matches, session_hash

CHALLENGE_SECONDS = 300
MAX_ACTIVE_CHALLENGES = 4096
FAKE_CREDENTIAL_BYTES = 32


class PasskeyError(ValueError):
    """A passkey ceremony was missing, expired, or failed verification."""


def list_passkeys(database, user_id):
    return [dict(row) for row in database.execute("""
      SELECT id,name,created_at,last_used_at FROM passkeys
      WHERE user_id=? ORDER BY created_at,id
    """, (user_id,)).fetchall()]


def _credential_descriptors(rows):
    return [PublicKeyCredentialDescriptor(id=bytes(row["credential_id"])) for row in rows]


def _store_challenge(database, user_id, purpose, challenge, timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp)
    database.execute("DELETE FROM webauthn_challenges WHERE expires_at<=?", (timestamp,))
    if database.execute("SELECT COUNT(*) FROM webauthn_challenges").fetchone()[0] >= MAX_ACTIVE_CHALLENGES:
        raise PasskeyError("Too many authentication ceremonies are active")
    ceremony = secrets.token_urlsafe(32)
    database.execute("""INSERT INTO webauthn_challenges
      (token_hash,user_id,purpose,challenge,expires_at,created_at) VALUES(?,?,?,?,?,?)""",
      (session_hash(ceremony), user_id, purpose, challenge,
       timestamp + CHALLENGE_SECONDS, utc_now()))
    return ceremony


def _consume_challenge(database, ceremony, purpose, timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp)
    row = database.execute("""SELECT user_id,challenge FROM webauthn_challenges
      WHERE token_hash=? AND purpose=? AND expires_at>?""",
      (session_hash(ceremony), purpose, timestamp)).fetchone()
    database.execute("DELETE FROM webauthn_challenges WHERE token_hash=?",
                     (session_hash(ceremony),))
    if not row:
        raise PasskeyError("Passkey ceremony is missing or expired")
    return row


def registration_options(database, user_id, current_password, rp_id, rp_name="Campfire"):
    user = database.execute(
        "SELECT username,password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or not password_matches(current_password, user["password_hash"]):
        raise PasskeyError("Current password is incorrect")
    credentials = database.execute(
        "SELECT credential_id FROM passkeys WHERE user_id=?", (user_id,)).fetchall()
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=str(user_id).encode(),
        user_name=user["username"],
        user_display_name=user["username"],
        challenge=challenge,
        exclude_credentials=_credential_descriptors(credentials),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    ceremony = _store_challenge(database, user_id, "register", challenge)
    return ceremony, json.loads(options_to_json(options))


def register_passkey(database, user_id, ceremony, credential, name, rp_id, origin):
    name = str(name).strip()
    if not 1 <= len(name) <= 64 or any(ord(character) < 32 for character in name):
        raise PasskeyError("Passkey name must be 1–64 printable characters")
    challenge = _consume_challenge(database, ceremony, "register")
    if challenge["user_id"] != user_id:
        raise PasskeyError("Passkey ceremony belongs to another account")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=bytes(challenge["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as failure:
        raise PasskeyError("Passkey verification failed") from failure
    created_at = utc_now()
    cursor = database.execute("""INSERT INTO passkeys
      (user_id,credential_id,public_key,sign_count,name,created_at)
      VALUES(?,?,?,?,?,?)""",
      (user_id, verified.credential_id, verified.credential_public_key,
       verified.sign_count, name, created_at))
    return {"id": cursor.lastrowid, "name": name, "created_at": created_at,
            "last_used_at": None}


def authentication_options(database, username, rp_id):
    user = database.execute(
        "SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    credentials = []
    if user:
        credentials = database.execute(
            "SELECT credential_id FROM passkeys WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
    # A nonexistent account and an account without a passkey receive the same
    # shaped ceremony. The fake descriptor can never verify or identify which
    # condition applied.
    descriptors = (_credential_descriptors(credentials)
                   if credentials else [PublicKeyCredentialDescriptor(
                       id=secrets.token_bytes(FAKE_CREDENTIAL_BYTES))])
    challenge = secrets.token_bytes(32)
    options = generate_authentication_options(
        rp_id=rp_id,
        challenge=challenge,
        allow_credentials=descriptors,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    ceremony = _store_challenge(
        database, user["id"] if user and credentials else None, "authenticate", challenge)
    return ceremony, json.loads(options_to_json(options))


def authenticate_passkey(database, ceremony, credential, rp_id, origin):
    challenge = _consume_challenge(database, ceremony, "authenticate")
    if challenge["user_id"] is None:
        raise PasskeyError("Passkey authentication failed")
    try:
        credential_id = base64url_to_bytes(str(credential.get("id", "")))
    except Exception as failure:
        raise PasskeyError("Passkey authentication failed") from failure
    row = database.execute("""SELECT id,user_id,public_key,sign_count FROM passkeys
      WHERE user_id=? AND credential_id=?""",
      (challenge["user_id"], credential_id)).fetchone()
    if not row:
        raise PasskeyError("Passkey authentication failed")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=bytes(challenge["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
    except Exception as failure:
        raise PasskeyError("Passkey authentication failed") from failure
    database.execute("UPDATE passkeys SET sign_count=?,last_used_at=? WHERE id=?",
                     (verified.new_sign_count, utc_now(), row["id"]))
    user = database.execute("SELECT id,username FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if not user:
        raise PasskeyError("Passkey authentication failed")
    return dict(user)


def remove_passkey(database, user_id, passkey_id, current_password):
    user = database.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or not password_matches(current_password, user["password_hash"]):
        return None
    deleted = database.execute(
        "DELETE FROM passkeys WHERE id=? AND user_id=?", (passkey_id, user_id)).rowcount
    return bool(deleted)
