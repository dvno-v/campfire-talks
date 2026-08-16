#!/usr/bin/env python3
"""Campfire HTTP application and request handlers."""

from __future__ import annotations

import hmac
import http.cookies
import json
import os
import queue
import re
import secrets
import select
import socket
import sqlite3
import threading
import time
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import ACCESS_LOGS, DB_PATH, HOST, MAX_EVENT_STREAMS, MAX_EVENT_STREAMS_PER_USER
from .config import LIVEKIT_API_KEY, LIVEKIT_API_SECRET, MAX_STORAGE_BYTES
from .config import MAX_UPLOAD_BYTES, PORT, PUBLIC_ORIGIN, RETENTION_SWEEP_SECONDS
from .config import MAX_VOICE_PARTICIPANTS, MEDIA_URL, SECURE_COOKIES, STATIC_DIR
from .config import STORAGE_WARNING_PERCENT, VOICE_LEASE_SECONDS
from .config import TRUSTED_PROXIES, UPLOAD_DIR
from .config import validate_configuration
from .database import connect, initialize_database, message_from_row, schema_version, utc_now
from .instance_lock import operation_lock, server_lock
from .migrations import LATEST_SCHEMA_VERSION
from .realtime import BROKER
from .security import AUTH_IP_LIMITER, AUTH_LIMITER, DUMMY_PASSWORD_HASH
from .security import MEDIA_TOKEN_LIMITER, MESSAGE_LIMITER, PASSWORD_ITERATIONS
from .security import UPLOAD_LIMITER, USERNAME_RE
from .security import client_address, invite_hash, password_hash, password_matches
from .security import session_hash, valid_invite
from .services.accounts import change_password as change_account_password
from .services.accounts import delete_account, deletion_plan, export_account
from .services.accounts import list_sessions, revoke_session as revoke_account_session
from .services.channels import MAX_SLOW_MODE_SECONDS, POSTING_ROLES, channel_context
from .services.channels import may_post, settings_payload
from .services.channels import update_settings as update_channel_settings
from .services.communities import ASSIGNABLE_ROLES, community_role, has_role, is_banned, is_member
from .services.communities import list_active_invites, list_community_bans, list_community_members
from .services.communities import remove_member as remove_community_member
from .services.communities import set_member_role, shares_community
from .services.communities import unban_member as unban_community_member
from .services.communities import revoke_invite as revoke_community_invite
from .services.messages import apply_edit, may_delete, may_edit, remove_message, visible_message
from .services.media import claim_lease, issue_join_token, release_lease, renew_lease
from .services.notifications import NOTIFICATION_MODES, account_mode, channel_states
from .services.notifications import mark_community_read, mark_read, set_account_mode, set_channel_mode
from .services.passkeys import PasskeyError, authenticate_passkey, authentication_options
from .services.passkeys import list_passkeys, register_passkey, registration_options, remove_passkey
from .services.retention import MAX_RETENTION_DAYS, purge_expired
from .services.retention import set_retention as set_community_retention
from .services.storage import begin_reserved_upload_write, capacity_warnings, directory_bytes
from .services.storage import filesystems_have_space
from .services.storage import release_upload, reserve_upload
from .services.storage import usage as storage_usage, writable_location
from .uploads import detect_image_type, safe_original_name, strip_metadata

KEEPALIVE_SECONDS = 20
DISCONNECT_POLL_SECONDS = 2
# One page of history. A channel holds more than this, so the response also says
# whether older messages remain: a client that could not tell would have no
# honest way to show the start of a channel apart from the middle of one.
MESSAGE_PAGE_SIZE = 100

# Static responses are loaded only from this explicit trusted manifest. Request
# paths select an in-memory response; they are never joined to a filesystem path
# or used to derive a response header.
_STATIC_MANIFEST = (
    ("/account.css", "account.css"),
    ("/app.js", "app.js"),
    ("/attachments.css", "attachments.css"),
    ("/feedback.css", "feedback.css"),
    ("/index.html", "index.html"),
    ("/invites.css", "invites.css"),
    ("/layout.css", "layout.css"),
    ("/members.css", "members.css"),
    ("/menu.css", "menu.css"),
    ("/messages.css", "messages.css"),
    ("/notifications.css", "notifications.css"),
    ("/shell.css", "shell.css"),
    ("/styles.css", "styles.css"),
    ("/voice.css", "voice.css"),
    ("/voice.js", "voice.js"),
    ("/livekit-e2ee-worker.js", "livekit-e2ee-worker.js"),
)


def _load_static_responses():
    """Load the reviewed frontend manifest without involving request data."""
    return {
        request_path: (STATIC_DIR / filename).read_bytes()
        for request_path, filename in _STATIC_MANIFEST
    }


_STATIC_RESPONSES = _load_static_responses()
_INDEX_RESPONSE = _STATIC_RESPONSES["/index.html"]
_STYLESHEET_PATHS = frozenset(
    request_path for request_path, filename in _STATIC_MANIFEST
    if filename.endswith(".css")
)
_SCRIPT_PATHS = frozenset(
    request_path for request_path, filename in _STATIC_MANIFEST
    if filename.endswith(".js")
)


def response_security_headers():
    connect_sources = "'self'" + (f" {MEDIA_URL}" if MEDIA_URL else "")
    headers = [
        ("Content-Security-Policy", f"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src {connect_sources}; font-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"),
        ("Permissions-Policy", "camera=(), microphone=(self), display-capture=(self), geolocation=()"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("X-Robots-Tag", "noindex, nofollow, noarchive"),
    ]
    if SECURE_COOKIES:
        headers.append(("Strict-Transport-Security", "max-age=31536000"))
    return headers


class InvalidBody(Exception):
    """Raised once a malformed request body has already been answered.

    Handlers must not continue after a rejected body: writing a second response
    to the same request desynchronizes a keep-alive connection.
    """


class App(BaseHTTPRequestHandler):
    server_version = "Campfire"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def add_security_headers(self):
        for name, value in response_security_headers():
            self.send_header(name, value)

    def end_headers(self):
        self.add_security_headers()
        super().end_headers()

    def log_message(self, fmt, *args):
        if ACCESS_LOGS:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    def json_body(self):
        """Return the request body as a JSON object, or raise InvalidBody."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= 70_000:
            # The declared length is unusable, so the stream cannot be resynchronized.
            self.close_connection = True
            return self.reject_body("Invalid JSON body")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.reject_body("Invalid JSON body")
        if not isinstance(body, dict):
            return self.reject_body("Request body must be a JSON object")
        return body

    def reject_body(self, message):
        self.error(HTTPStatus.BAD_REQUEST, message)
        raise InvalidBody(message)

    def send_json(self, value, status=HTTPStatus.OK, headers=None):
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, item in (headers or {}).items():
            self.send_header(name, item)
        self.end_headers()
        self.wfile.write(payload)

    def error(self, status, message):
        self.send_json({"error": message}, status)
        return None

    def current_user(self):
        cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("campfire_session")
        if not token:
            self.authenticated_session_token = None
            return None
        token_digest = session_hash(token.value)
        with connect() as db:
            row = db.execute("""
              SELECT users.id, users.username FROM sessions
              JOIN users ON users.id = sessions.user_id
              WHERE token = ? AND expires_at > ?
            """, (token_digest, int(time.time()))).fetchone()
        self.authenticated_session_token = token_digest if row else None
        return {"id": row["id"], "username": row["username"]} if row else None

    def require_user(self):
        user = self.current_user()
        if user:
            return user
        if self.command != "GET":
            # The unread request body would otherwise desynchronize the connection.
            self.close_connection = True
        return self.error(HTTPStatus.UNAUTHORIZED, "Sign in required")

    def valid_request_source(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        expected = PUBLIC_ORIGIN or f"{'https' if SECURE_COOKIES else 'http'}://{self.headers.get('Host', '')}"
        return hmac.compare_digest(origin.rstrip("/"), expected)

    def client_ip(self):
        peer = self.client_address[0] if self.client_address else ""
        return client_address(peer, self.headers.get("X-Forwarded-For"), TRUSTED_PROXIES)

    def auth_limit_key(self, operation, subject=None):
        key = f"{self.client_ip()}:{operation.casefold()}"
        if subject is not None:
            key += f":{str(subject).casefold()}"
        return key

    def rate_limit_auth(self, operation, subject=None):
        # The first bucket has a fixed-size key controlled by the server. A
        # syntactically valid account identifier may additionally spend from a
        # global account bucket, preventing a distributed password spray.
        if not AUTH_IP_LIMITER.allow(self.auth_limit_key(operation)):
            self.error(HTTPStatus.TOO_MANY_REQUESTS,
                       "Too many attempts. Wait a few minutes and try again")
            return False
        if subject is None or AUTH_LIMITER.allow(f"{operation.casefold()}:{str(subject).casefold()}"):
            return True
        self.error(HTTPStatus.TOO_MANY_REQUESTS, "Too many attempts. Wait a few minutes and try again")
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json({"status": "ok"})
        if path == "/readyz":
            return self.readiness()
        if path == "/api/me":
            user = self.current_user()
            return self.send_json({"user": user})
        if path == "/api/bootstrap":
            return self.bootstrap()
        if path == "/api/unread":
            return self.unread_state()
        if path == "/api/sessions":
            return self.active_sessions()
        if path == "/api/passkeys":
            return self.active_passkeys()
        if path == "/api/storage":
            return self.storage_usage()
        if path == "/api/account/export":
            return self.export_account_data()
        if path == "/api/account/deletion":
            return self.account_deletion_plan()
        if path == "/api/events":
            return self.events()
        if path.startswith("/api/communities/") and path.endswith("/members"):
            return self.community_members(path)
        if path.startswith("/api/communities/") and path.endswith("/invites"):
            return self.community_invites(path)
        if path.startswith("/api/communities/") and path.endswith("/bans"):
            return self.community_bans(path)
        if path.startswith("/api/attachments/"):
            return self.serve_attachment(path)
        if path.startswith("/api/channels/") and path.endswith("/messages"):
            return self.list_messages(path)
        if path.startswith("/api/"):
            return self.error(HTTPStatus.NOT_FOUND, "Not found")
        return self.static_file(path)

    def do_POST(self):
        if not self.valid_request_source():
            self.close_connection = True
            return self.error(HTTPStatus.FORBIDDEN, "Cross-origin request blocked")
        path = urlparse(self.path).path
        routes = {
            "/api/register": self.register,
            "/api/login": self.login,
            "/api/passkeys/login/options": self.passkey_login_options,
            "/api/passkeys/login/verify": self.passkey_login_verify,
            "/api/passkeys/register/options": self.passkey_registration_options,
            "/api/passkeys/register/verify": self.passkey_registration_verify,
            "/api/logout": self.logout,
            "/api/communities": self.create_community,
            "/api/channels": self.create_channel,
            "/api/invites": self.create_invite,
            "/api/invites/join": self.join_invite,
        }
        try:
            if path in routes:
                return routes[path]()
            if path.startswith("/api/channels/") and path.endswith("/messages"):
                return self.create_message(path)
            if path.startswith("/api/channels/") and path.endswith("/voice/token"):
                return self.voice_token(path)
            if path.startswith("/api/channels/") and path.endswith("/voice/heartbeat"):
                return self.voice_heartbeat(path)
            if path.startswith("/api/channels/") and path.endswith("/uploads"):
                return self.upload_attachment(path)
            if path.startswith("/api/channels/") and path.endswith("/read"):
                return self.mark_channel_read(path)
            if path.startswith("/api/communities/") and path.endswith("/ban"):
                return self.ban_member(path)
        except InvalidBody:
            return
        return self.error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self):
        if not self.valid_request_source():
            self.close_connection = True
            return self.error(HTTPStatus.FORBIDDEN, "Cross-origin request blocked")
        path = urlparse(self.path).path
        try:
            if path == "/api/account":
                return self.delete_own_account()
            if path.startswith("/api/invites/"):
                return self.revoke_invite(path)
            if path.startswith("/api/messages/"):
                return self.delete_message(path)
            if path.startswith("/api/communities/") and "/bans/" in path:
                return self.unban_member(path)
            if path.startswith("/api/communities/") and "/members/" in path:
                return self.kick_member(path)
            if path.startswith("/api/sessions/"):
                return self.revoke_session(path)
            if path.startswith("/api/passkeys/"):
                return self.delete_passkey(path)
            if path.startswith("/api/channels/") and path.endswith("/voice/lease"):
                return self.voice_leave(path)
        except InvalidBody:
            return
        return self.error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PATCH(self):
        if not self.valid_request_source():
            self.close_connection = True
            return self.error(HTTPStatus.FORBIDDEN, "Cross-origin request blocked")
        path = urlparse(self.path).path
        try:
            if path == "/api/preferences/notifications":
                return self.set_notification_default()
            if path == "/api/account/password":
                return self.change_password()
            # Exactly /api/channels/{id}, so a longer channel path never lands
            # here by having no suffix this route recognises.
            if path.startswith("/api/channels/") and path.count("/") == 3:
                return self.update_channel(path)
            if path.startswith("/api/messages/"):
                return self.edit_message(path)
            if path.startswith("/api/communities/") and path.endswith("/retention"):
                return self.update_retention(path)
            if path.startswith("/api/communities/") and "/members/" in path:
                return self.update_member_role(path)
            if path.startswith("/api/channels/") and path.endswith("/notifications"):
                return self.set_channel_notifications(path)
        except InvalidBody:
            return
        return self.error(HTTPStatus.NOT_FOUND, "Not found")

    def register(self):
        body = self.json_body()
        if not self.rate_limit_auth("register"):
            return
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        invite_token = str(body.get("invite", "")).strip()
        if not USERNAME_RE.fullmatch(username):
            return self.error(HTTPStatus.BAD_REQUEST, "Username must be 2–24 letters, numbers, or underscores")
        if not 12 <= len(password) <= 1024:
            return self.error(HTTPStatus.BAD_REQUEST, "Password must be 12–1024 characters")
        try:
            with connect() as db:
                # Serialize the empty-instance decision with user creation. A
                # second registration waits here, then observes the first user
                # and must present an invitation.
                db.execute("BEGIN IMMEDIATE")
                user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                invitation = valid_invite(db, invite_token)
                if user_count and not invitation:
                    return self.error(HTTPStatus.FORBIDDEN, "A valid invite is required")
                cursor = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                    (username, password_hash(password), utc_now()))
                user_id = cursor.lastrowid
                if invitation:
                    updated = db.execute("UPDATE invitations SET uses=uses+1 WHERE id=? AND uses<max_uses",
                                         (invitation["id"],))
                    if updated.rowcount != 1:
                        raise sqlite3.IntegrityError("invite exhausted")
                    db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                               (invitation["community_id"], user_id))
                    mark_community_read(db, invitation["community_id"], user_id)
                else:
                    community = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                           (f"{username}'s place", user_id, utc_now())).lastrowid
                    db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                               (community, user_id))
                    db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                               (community, "general", utc_now()))
        except sqlite3.IntegrityError:
            return self.error(HTTPStatus.CONFLICT, "That username is already taken")
        if invitation:
            self.announce_member(invitation["community_id"], user_id, username)
        return self.start_session(user_id, username, HTTPStatus.CREATED)

    def announce_member(self, community_id, user_id, username):
        """Tell the community about an arrival so member lists update in place."""
        BROKER.publish({"type": "member.joined", "community_id": community_id,
                        "member": {"id": user_id, "username": username,
                                   "role": "member", "online": False}})

    def login(self):
        body = self.json_body()
        attempted_username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        valid_username = USERNAME_RE.fullmatch(attempted_username) is not None
        if not self.rate_limit_auth("login", attempted_username if valid_username else None):
            return
        if not valid_username or len(password) > 1024:
            if len(password) <= 1024:
                password_matches(password, DUMMY_PASSWORD_HASH)
            return self.error(HTTPStatus.UNAUTHORIZED, "Incorrect username or password")
        with connect() as db:
            row = db.execute("SELECT id,username,password_hash FROM users WHERE username = ? COLLATE NOCASE",
                             (attempted_username,)).fetchone()
        matched = password_matches(password, row["password_hash"] if row else DUMMY_PASSWORD_HASH)
        if not row or not matched:
            return self.error(HTTPStatus.UNAUTHORIZED, "Incorrect username or password")
        if not row["password_hash"].startswith(f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"):
            with connect() as db:
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (password_hash(password), row["id"]))
        AUTH_LIMITER.clear(f"login:{attempted_username.casefold()}")
        return self.start_session(row["id"], row["username"])

    def start_session(self, user_id, username, status=HTTPStatus.OK):
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + 60 * 60 * 24 * 30
        with connect() as db:
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (session_hash(token), user_id, expires, utc_now()))
        return self.send_json({"user": {"id": user_id, "username": username}}, status,
                              {"Set-Cookie": self.session_cookie(token)})

    def webauthn_context(self):
        origin = PUBLIC_ORIGIN or f"{'https' if SECURE_COOKIES else 'http'}://{self.headers.get('Host', '')}"
        parsed = urlparse(origin)
        if not parsed.hostname:
            raise PasskeyError("Passkeys require a valid application origin")
        return parsed.hostname, origin

    def passkey_login_options(self):
        body = self.json_body()
        username = str(body.get("username", "")).strip()
        valid_username = USERNAME_RE.fullmatch(username) is not None
        if not self.rate_limit_auth("passkey-login", username if valid_username else None):
            return
        try:
            rp_id, _ = self.webauthn_context()
            with connect() as db:
                ceremony, options = authentication_options(
                    db, username if valid_username else "", rp_id)
        except PasskeyError:
            return self.error(HTTPStatus.SERVICE_UNAVAILABLE,
                              "Passkey authentication is temporarily unavailable")
        self.send_json({"ceremony": ceremony, "options": options})

    def passkey_login_verify(self):
        body = self.json_body()
        if not self.rate_limit_auth("passkey-verify"):
            return
        ceremony = str(body.get("ceremony", ""))
        credential = body.get("credential")
        if not 20 <= len(ceremony) <= 128 or not isinstance(credential, dict):
            return self.error(HTTPStatus.UNAUTHORIZED, "Passkey authentication failed")
        try:
            rp_id, origin = self.webauthn_context()
            with connect() as db:
                user = authenticate_passkey(db, ceremony, credential, rp_id, origin)
        except PasskeyError:
            return self.error(HTTPStatus.UNAUTHORIZED, "Passkey authentication failed")
        AUTH_LIMITER.clear(f"passkey-login:{user['username'].casefold()}")
        return self.start_session(user["id"], user["username"])

    def active_passkeys(self):
        user = self.require_user()
        if not user:
            return
        with connect() as db:
            passkeys = list_passkeys(db, user["id"])
        self.send_json({"passkeys": passkeys})

    def passkey_registration_options(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        current_password = str(body.get("current_password", ""))
        if len(current_password) > 1024:
            return self.error(HTTPStatus.FORBIDDEN, "Current password is incorrect")
        if not self.rate_limit_auth("passkey-register", user["id"]):
            return
        try:
            rp_id, _ = self.webauthn_context()
            with connect() as db:
                ceremony, options = registration_options(
                    db, user["id"], current_password, rp_id)
        except PasskeyError as failure:
            if "password" in str(failure).casefold():
                return self.error(HTTPStatus.FORBIDDEN, "Current password is incorrect")
            return self.error(HTTPStatus.SERVICE_UNAVAILABLE,
                              "Passkey registration is temporarily unavailable")
        self.send_json({"ceremony": ceremony, "options": options})

    def passkey_registration_verify(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        ceremony = str(body.get("ceremony", ""))
        name = str(body.get("name", ""))
        credential = body.get("credential")
        if not 20 <= len(ceremony) <= 128 or not isinstance(credential, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Passkey verification failed")
        try:
            rp_id, origin = self.webauthn_context()
            with connect() as db:
                stored = register_passkey(
                    db, user["id"], ceremony, credential, name, rp_id, origin)
        except (PasskeyError, sqlite3.IntegrityError):
            return self.error(HTTPStatus.BAD_REQUEST, "Passkey verification failed")
        self.send_json(stored, HTTPStatus.CREATED)

    def delete_passkey(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            passkey_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid passkey")
        password = str(self.json_body().get("current_password", ""))
        if len(password) > 1024:
            return self.error(HTTPStatus.FORBIDDEN, "Current password is incorrect")
        if not self.rate_limit_auth("passkey-delete", user["id"]):
            return
        with connect() as db:
            removed = remove_passkey(db, user["id"], passkey_id, password)
        if removed is None:
            return self.error(HTTPStatus.FORBIDDEN, "Current password is incorrect")
        if not removed:
            return self.error(HTTPStatus.NOT_FOUND, "Passkey not found")
        AUTH_LIMITER.clear(f"passkey-delete:{user['id']}")
        self.send_json({"ok": True})

    def logout(self):
        cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("campfire_session")
        if token:
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token = ?", (session_hash(token.value),))
        return self.send_json({"ok": True}, headers={"Set-Cookie": self.expired_session_cookie()})

    def active_sessions(self):
        user = self.require_user()
        if not user:
            return
        with connect() as database:
            sessions = list_sessions(database, user["id"], self.authenticated_session_token)
        self.send_json({"sessions": sessions})

    def revoke_session(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            parts = path.split("/")
            if len(parts) != 4:
                raise ValueError
            session_id = int(parts[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid session")
        with connect() as database:
            revoked_current = revoke_account_session(
                database, user["id"], session_id, self.authenticated_session_token)
        if revoked_current is None:
            return self.error(HTTPStatus.NOT_FOUND, "Session not found")
        headers = {"Set-Cookie": self.expired_session_cookie()} if revoked_current else None
        self.send_json({"ok": True, "current": revoked_current}, headers=headers)

    def change_password(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        current_password = str(body.get("current_password", ""))
        new_password = str(body.get("new_password", ""))
        if not 12 <= len(new_password) <= 1024:
            return self.error(HTTPStatus.BAD_REQUEST, "New password must be 12–1024 characters")
        if len(current_password) > 1024:
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid current password")
        if current_password == new_password:
            return self.error(HTTPStatus.CONFLICT, "New password must be different")
        # Charged here rather than on entry: the checks above are the client's
        # own mistakes to make, and spending the allowance on them would lock
        # someone out for five minutes without a password ever being compared.
        if not self.rate_limit_auth("password", user["id"]):
            return
        replacement = secrets.token_urlsafe(32)
        with connect() as database:
            revoked = change_account_password(database, user["id"], current_password, new_password,
                                              self.authenticated_session_token,
                                              session_hash(replacement))
        if revoked is None:
            return self.error(HTTPStatus.FORBIDDEN, "Current password is incorrect")
        AUTH_LIMITER.clear(f"password:{user['id']}")
        self.send_json({"ok": True, "revoked_sessions": revoked},
                       headers={"Set-Cookie": self.session_cookie(replacement)})

    def session_cookie(self, token):
        secure = "; Secure" if SECURE_COOKIES else ""
        return f"campfire_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}"

    def expired_session_cookie(self):
        secure = "; Secure" if SECURE_COOKIES else ""
        return f"campfire_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"

    def storage_usage(self):
        """What the instance is storing, for the people who look after it."""
        user = self.require_user()
        if not user:
            return
        with closing(connect()) as db:
            report = storage_usage(db, user["id"], MAX_STORAGE_BYTES,
                                   directory_bytes(UPLOAD_DIR))
            warnings = (capacity_warnings(db, MAX_STORAGE_BYTES, UPLOAD_DIR,
                                          DB_PATH, STORAGE_WARNING_PERCENT)
                        if report is not None else [])
        if report is None:
            return self.error(HTTPStatus.FORBIDDEN,
                              "Only community administrators can see storage usage")
        report["warnings"] = warnings
        self.send_json(report)

    def readiness(self):
        """Report whether this process can serve Campfire without exposing internals.

        Readiness only, deliberately. This endpoint is unauthenticated and
        reachable by anyone who can resolve the public name, so it answers the
        one question a probe is entitled to ask. How full the instance is
        describes the people using it, not whether it can serve, and it is
        already reported to administrators through `/api/storage`.
        """
        checks = {"database": "failed", "storage": "failed"}
        try:
            with closing(connect()) as database:
                # Naming a required table catches an empty or wrong SQLite file;
                # SELECT 1 alone would declare either one ready.
                database.execute("SELECT 1 FROM users LIMIT 1").fetchone()
                if schema_version(database) == LATEST_SCHEMA_VERSION:
                    checks["database"] = "ok"
        except (OSError, sqlite3.Error):
            pass
        database_parent_writable = writable_location(DB_PATH.parent, directory=True)
        if (database_parent_writable and writable_location(DB_PATH)
                and writable_location(UPLOAD_DIR, directory=True)
                and filesystems_have_space(DB_PATH, UPLOAD_DIR)):
            checks["storage"] = "ok"
        ready = all(value == "ok" for value in checks.values())
        payload = {"status": "ready" if ready else "not_ready", "checks": checks}
        self.send_json(payload, HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                       None if ready else {"Retry-After": "5"})

    def export_account_data(self):
        user = self.require_user()
        if not user:
            return
        with connect() as database:
            export = export_account(database, user["id"])
        if export is None:
            return self.error(HTTPStatus.NOT_FOUND, "Account not found")
        # Usernames are already restricted to letters, digits and underscores,
        # so the filename cannot carry quotes or path separators into the header.
        filename = f"campfire-{user['username']}-{export['exported_at'][:10]}.json"
        self.send_json(export, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    def account_deletion_plan(self):
        user = self.require_user()
        if not user:
            return
        with connect() as database:
            plan = deletion_plan(database, user["id"])
        self.send_json(plan)

    def delete_own_account(self):
        user = self.require_user()
        if not user:
            return
        password = str(self.json_body().get("current_password", ""))
        if len(password) > 1024:
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid current password")
        if not self.rate_limit_auth("delete", user["id"]):
            return
        with connect() as database:
            # Read the memberships before they are gone: the clients that must
            # be told cannot be identified from a deleted account.
            communities = [row["community_id"] for row in database.execute(
                "SELECT community_id FROM memberships WHERE user_id=?", (user["id"],)).fetchall()]
            status, plan = delete_account(database, user["id"], password)
        if status == "invalid_password":
            return self.error(HTTPStatus.FORBIDDEN, "Current password is incorrect")
        if status != "ok":
            return self.error(HTTPStatus.NOT_FOUND, "Account not found")
        for orphaned_file in plan["orphaned_files"]:
            (UPLOAD_DIR / orphaned_file).unlink(missing_ok=True)
        AUTH_LIMITER.clear(f"delete:{user['id']}")
        for community_id in communities:
            # `deleted_account` tells the remaining members that history changed
            # underneath them, not merely the member list.
            BROKER.publish({"type": "member.removed", "community_id": community_id,
                            "user_id": user["id"], "banned": False, "deleted_account": True})
        self.send_json({"ok": True, "messages": plan["messages"],
                        "attachments": plan["attachments"],
                        "communities_dissolved": plan["communities_dissolved"],
                        "communities_transferred": plan["communities_transferred"]},
                       headers={"Set-Cookie": self.expired_session_cookie()})

    def bootstrap(self):
        user = self.require_user()
        if not user:
            return
        with connect() as db:
            rows = db.execute("""
              SELECT c.id community_id,c.name community_name,
                c.message_retention_days,c.attachment_retention_days,
                ch.id channel_id,ch.name channel_name,
                ch.kind,ch.post_min_role,ch.slow_mode_seconds,ch.uploads_allowed,
                CASE WHEN c.owner_id=m.user_id THEN 'owner' ELSE m.role END community_role
              FROM communities c JOIN memberships m ON m.community_id=c.id
              JOIN channels ch ON ch.community_id=c.id WHERE m.user_id=?
              ORDER BY c.id,ch.id
            """, (user["id"],)).fetchall()
            states = channel_states(db, user["id"])
            default_mode = account_mode(db, user["id"])
        communities = {}
        for row in rows:
            community = communities.setdefault(row["community_id"], {
                "id": row["community_id"], "name": row["community_name"],
                "role": row["community_role"],
                "retention": {"message_days": row["message_retention_days"],
                              "attachment_days": row["attachment_retention_days"]},
                "channels": []})
            community["channels"].append({"id": row["channel_id"], "name": row["channel_name"]}
                                         | settings_payload(row)
                                         | self.channel_state_payload(states.get(row["channel_id"])))
        self.send_json({"user": user, "communities": list(communities.values()),
                        "notifications": {"default_mode": default_mode},
                        # The client refuses an oversized image before spending
                        # anyone's upload on it, so it has to be told the same
                        # ceiling the server enforces rather than assume one.
                        "limits": {"max_upload_bytes": MAX_UPLOAD_BYTES},
                        "media": {"enabled": bool(MEDIA_URL),
                                  "max_participants": MAX_VOICE_PARTICIPANTS}})

    def channel_state_payload(self, state):
        """The unread/notification fields a client needs to render one channel.

        A channel with nothing recorded reads the same as one nobody has opened
        yet, which is also what a client sees for a channel created moments ago.
        """
        state = state or {}
        return {"unread": state.get("unread", 0),
                "last_read_message_id": state.get("last_read_message_id", 0),
                "notify": state.get("notify")}

    def unread_state(self):
        """Every channel's unread total and notification mode in one read.

        Clients re-read this after a reconnect or a `stream.reset`, because
        anything published during a gap never reached them and the badges would
        otherwise stay wrong without saying so.
        """
        user = self.require_user()
        if not user:
            return
        with connect() as db:
            states = channel_states(db, user["id"])
            default_mode = account_mode(db, user["id"])
        channels = [{"channel_id": channel_id} | self.channel_state_payload(state)
                    for channel_id, state in states.items()]
        self.send_json({"channels": channels, "default_mode": default_mode})

    def channel_id_from(self, path):
        try:
            return int(path.split("/")[3])
        except (ValueError, IndexError):
            # The body is never read on this path, so the connection cannot be
            # resynchronized for a following request.
            self.close_connection = True
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid channel")

    def mark_channel_read(self, path):
        user = self.require_user()
        if not user:
            return
        channel_id = self.channel_id_from(path)
        if channel_id is None:
            return
        try:
            message_id = int(self.json_body().get("message_id", 0))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid message")
        with connect() as db:
            state = mark_read(db, channel_id, user["id"], message_id)
        if state is None:
            return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
        self.send_json({"channel_id": channel_id} | self.channel_state_payload(state))

    def set_notification_default(self):
        user = self.require_user()
        if not user:
            return
        mode = str(self.json_body().get("default_mode", ""))
        if mode not in NOTIFICATION_MODES:
            return self.error(HTTPStatus.BAD_REQUEST, "Notification mode must be 'all' or 'none'")
        with connect() as db:
            set_account_mode(db, user["id"], mode)
        self.send_json({"default_mode": mode})

    def set_channel_notifications(self, path):
        user = self.require_user()
        if not user:
            return
        channel_id = self.channel_id_from(path)
        if channel_id is None:
            return
        # "default" clears the override, which an absent field could not express
        # unambiguously.
        mode = str(self.json_body().get("mode", ""))
        if mode not in NOTIFICATION_MODES | {"default"}:
            return self.error(HTTPStatus.BAD_REQUEST,
                              "Notification mode must be 'all', 'none', or 'default'")
        with connect() as db:
            allowed = set_channel_mode(db, channel_id, user["id"], None if mode == "default" else mode)
            state = channel_states(db, user["id"], channel_id).get(channel_id) if allowed else None
        if not allowed:
            return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
        self.send_json({"channel_id": channel_id} | self.channel_state_payload(state))

    def create_community(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        name = str(body.get("name", "")).strip()
        if not 2 <= len(name) <= 40:
            return self.error(HTTPStatus.BAD_REQUEST, "Community name must be 2–40 characters")
        with connect() as db:
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      (name, user["id"], utc_now())).lastrowid
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                       (community_id, user["id"]))
            channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                    (community_id, "general", utc_now())).lastrowid
        self.send_json({"id": community_id, "name": name, "role": "owner",
                        "channels": [{"id": channel_id, "name": "general", "kind": "text",
                                      "post_min_role": "member", "slow_mode_seconds": 0,
                                      "uploads_allowed": True}]}, HTTPStatus.CREATED)

    def community_members(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            community_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        with connect() as database:
            members = list_community_members(database, community_id, user["id"], BROKER.online_user_ids())
        if members is None:
            return self.error(HTTPStatus.NOT_FOUND, "Community not found")
        self.send_json({"members": members})

    def community_invites(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            community_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        with connect() as database:
            invitations = list_active_invites(database, community_id, user["id"])
        if invitations is None:
            return self.error(HTTPStatus.FORBIDDEN, "Only community administrators can manage invites")
        self.send_json({"invites": invitations})

    def community_member_ids(self, path, suffix=""):
        """Parse an exact community member resource path, closing on failure."""
        try:
            parts = path.split("/")
            expected = ["", "api", "communities", parts[3], "members", parts[5]]
            if suffix:
                expected.append(suffix)
            if parts != expected:
                raise ValueError
            return int(parts[3]), int(parts[5])
        except (ValueError, IndexError):
            self.close_connection = True
            self.error(HTTPStatus.BAD_REQUEST, "Invalid community member")
            return None

    def community_ban_ids(self, path):
        try:
            parts = path.split("/")
            if len(parts) != 6 or parts[4] != "bans":
                raise ValueError
            return int(parts[3]), int(parts[5])
        except (ValueError, IndexError):
            self.close_connection = True
            self.error(HTTPStatus.BAD_REQUEST, "Invalid community ban")
            return None

    def community_bans(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            community_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        with connect() as database:
            role = community_role(database, community_id, user["id"])
            if role is None:
                return self.error(HTTPStatus.NOT_FOUND, "Community not found")
            bans = list_community_bans(database, community_id, user["id"])
        if bans is None:
            return self.error(HTTPStatus.FORBIDDEN, "Only community moderators can view bans")
        self.send_json({"bans": bans})

    def moderate_member(self, path, ban):
        user = self.require_user()
        if not user:
            return
        identifiers = self.community_member_ids(path, "ban" if ban else "")
        if not identifiers:
            return
        community_id, member_id = identifiers
        if ban:
            self.json_body()  # validate and consume the bounded request body
        with connect() as database:
            status, member = remove_community_member(
                database, community_id, member_id, user["id"], ban=ban)
        if status == "community_not_found":
            return self.error(HTTPStatus.NOT_FOUND, "Community not found")
        if status == "member_not_found":
            return self.error(HTTPStatus.NOT_FOUND, "Member not found")
        if status != "ok":
            return self.error(HTTPStatus.FORBIDDEN, "You can only moderate members below your role")
        BROKER.publish({"type": "member.removed", "community_id": community_id,
                        "user_id": member_id, "banned": ban})
        self.send_json({"ok": True, "member": member})

    def kick_member(self, path):
        return self.moderate_member(path, ban=False)

    def ban_member(self, path):
        return self.moderate_member(path, ban=True)

    def unban_member(self, path):
        user = self.require_user()
        if not user:
            return
        identifiers = self.community_ban_ids(path)
        if not identifiers:
            return
        community_id, member_id = identifiers
        with connect() as database:
            status = unban_community_member(database, community_id, member_id, user["id"])
        if status == "community_not_found":
            return self.error(HTTPStatus.NOT_FOUND, "Community not found")
        if status == "ban_not_found":
            return self.error(HTTPStatus.NOT_FOUND, "Ban not found")
        if status != "ok":
            return self.error(HTTPStatus.FORBIDDEN, "You can only unban members below your role")
        self.send_json({"ok": True})

    def update_retention(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            community_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            self.close_connection = True
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        body = self.json_body()
        try:
            message_days = int(body.get("message_days", 0))
            attachment_days = int(body.get("attachment_days", 0))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid retention")
        with connect() as db:
            stored = set_community_retention(db, community_id, user["id"],
                                             message_days, attachment_days)
        if stored is None:
            return self.error(HTTPStatus.FORBIDDEN,
                              "Only community administrators can change retention")
        if stored is False:
            return self.error(HTTPStatus.BAD_REQUEST,
                              f"Retention must be 0–{MAX_RETENTION_DAYS} days")
        # Applied immediately rather than at the next sweep: shortening a window
        # should take effect when it is set, not up to an hour later.
        run_retention_sweep()
        self.send_json(stored)

    def update_member_role(self, path):
        user = self.require_user()
        if not user:
            return
        identifiers = self.community_member_ids(path)
        if not identifiers:
            return
        community_id, member_id = identifiers
        role = str(self.json_body().get("role", ""))
        if role not in ASSIGNABLE_ROLES:
            return self.error(HTTPStatus.BAD_REQUEST,
                              "Role must be 'administrator', 'moderator', or 'member'")
        with connect() as database:
            actor_role = database.execute("""
              SELECT CASE WHEN c.owner_id=m.user_id THEN 'owner' ELSE m.role END role
              FROM memberships m JOIN communities c ON c.id=m.community_id
              WHERE m.community_id=? AND m.user_id=?
            """, (community_id, user["id"])).fetchone()
            if not actor_role:
                return self.error(HTTPStatus.NOT_FOUND, "Community not found")
            if actor_role["role"] != "owner":
                return self.error(HTTPStatus.FORBIDDEN, "Only the community owner can change roles")
            updated = set_member_role(database, community_id, member_id, user["id"], role)
        if not updated:
            return self.error(HTTPStatus.CONFLICT if member_id == user["id"] else HTTPStatus.NOT_FOUND,
                              "Community ownership cannot be changed" if member_id == user["id"]
                              else "Member not found")
        updated["online"] = member_id in BROKER.online_user_ids()
        BROKER.publish({"type": "member.updated", "community_id": community_id, "member": updated})
        self.send_json(updated)

    def create_channel(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        name = re.sub(r"[^a-z0-9-]", "-", str(body.get("name", "")).lower().strip()).strip("-")
        kind = str(body.get("kind", "text"))
        try:
            community_id = int(body.get("community_id"))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        with connect() as db:
            if not has_role(db, community_id, user["id"], "administrator"):
                return self.error(HTTPStatus.FORBIDDEN, "Only community administrators can add channels")
            if not 2 <= len(name) <= 30:
                return self.error(HTTPStatus.BAD_REQUEST, "Channel name must be 2–30 characters")
            if kind not in {"text", "voice"}:
                return self.error(HTTPStatus.BAD_REQUEST, "Channel kind must be 'text' or 'voice'")
            try:
                channel_id = db.execute("""INSERT INTO channels(community_id,name,kind,created_at)
                                         VALUES(?,?,?,?)""",
                                        (community_id, name, kind, utc_now())).lastrowid
            except sqlite3.IntegrityError:
                return self.error(HTTPStatus.CONFLICT, "That channel already exists")
        created = {"id": channel_id, "community_id": community_id, "name": name, "kind": kind,
                   "post_min_role": "member",
                   "slow_mode_seconds": 0, "uploads_allowed": True}
        BROKER.publish({"type": "channel.created", "community_id": community_id} | created)
        self.send_json(created, HTTPStatus.CREATED)

    def create_invite(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        try:
            community_id = int(body.get("community_id"))
            max_uses = min(25, max(1, int(body.get("max_uses", 10))))
            lifetime_hours = min(168, max(1, int(body.get("lifetime_hours", 24))))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid invite settings")
        token = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + lifetime_hours * 3600
        with connect() as db:
            if not has_role(db, community_id, user["id"], "administrator"):
                return self.error(HTTPStatus.FORBIDDEN, "Only community administrators can create invites")
            db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,created_at)
              VALUES(?,?,?,?,?,?)""",
              (community_id, user["id"], invite_hash(token), expires_at, max_uses, utc_now()))
        self.send_json({"token": token, "expires_at": expires_at, "max_uses": max_uses}, HTTPStatus.CREATED)

    def revoke_invite(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            invite_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid invite")
        with connect() as database:
            revoked = revoke_community_invite(database, invite_id, user["id"])
        if not revoked:
            return self.error(HTTPStatus.NOT_FOUND, "Invite not found")
        self.send_json({"ok": True})

    def join_invite(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        token = str(body.get("invite", "")).strip()
        with connect() as db:
            invitation = valid_invite(db, token)
            if not invitation:
                return self.error(HTTPStatus.NOT_FOUND, "Invite is invalid, expired, or fully used")
            existing = db.execute("SELECT 1 FROM memberships WHERE community_id=? AND user_id=?",
                                  (invitation["community_id"], user["id"])).fetchone()
            if existing:
                return self.error(HTTPStatus.CONFLICT, "You are already a member of that community")
            if is_banned(db, invitation["community_id"], user["id"]):
                return self.error(HTTPStatus.FORBIDDEN, "You are banned from that community")
            updated = db.execute("UPDATE invitations SET uses=uses+1 WHERE id=? AND uses<max_uses",
                                 (invitation["id"],))
            if updated.rowcount != 1:
                return self.error(HTTPStatus.CONFLICT, "Invite is fully used")
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                       (invitation["community_id"], user["id"]))
            mark_community_read(db, invitation["community_id"], user["id"])
        self.announce_member(invitation["community_id"], user["id"], user["username"])
        self.send_json({"id": invitation["community_id"], "name": invitation["community_name"]},
                       HTTPStatus.CREATED)

    def member_channel(self, db, channel_id, user_id):
        return db.execute("""SELECT ch.id,ch.kind FROM channels ch JOIN memberships m ON m.community_id=ch.community_id
                             WHERE ch.id=? AND m.user_id=?""", (channel_id, user_id)).fetchone()

    def list_messages(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            channel_id = int(path.split("/")[3])
        except ValueError:
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid channel")
        query = parse_qs(urlparse(self.path).query)
        after = self.message_bound(query, "after")
        # `before` walks backwards through history a page at a time. Without it
        # everything past the newest page is stored but unreachable: `after`
        # only ever returns messages newer than one already in hand.
        before = self.message_bound(query, "before")
        conditions, parameters = ["m.channel_id=?", "m.id>?"], [channel_id, after]
        if before:
            conditions.append("m.id<?")
            parameters.append(before)
        with connect() as db:
            channel = self.member_channel(db, channel_id, user["id"])
            if not channel:
                return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
            if channel["kind"] != "text":
                return self.error(HTTPStatus.CONFLICT, "Voice channels do not contain messages")
            rows = db.execute(f"""SELECT m.id,m.channel_id,m.body,m.created_at,m.edited_at,m.attachment_id,
              u.id author_id,u.username,a.original_name,a.mime_type,a.byte_size
              FROM messages m JOIN users u ON u.id=m.author_id
              LEFT JOIN attachments a ON a.id=m.attachment_id
              WHERE {' AND '.join(conditions)} ORDER BY m.id DESC LIMIT ?""",
              (*parameters, MESSAGE_PAGE_SIZE)).fetchall()
            # Asked of the oldest row actually returned rather than inferred
            # from a full page, so a channel holding exactly one page does not
            # offer a button that would fetch nothing.
            older = bool(rows) and db.execute(
                "SELECT 1 FROM messages WHERE channel_id=? AND id<? LIMIT 1",
                (channel_id, rows[-1]["id"])).fetchone() is not None
        self.send_json({"messages": [message_from_row(row) for row in reversed(rows)],
                        "has_more": older})

    def message_bound(self, query, name):
        """Read one non-negative message-id bound from the query string."""
        try:
            return max(0, int(query.get(name, [0])[0]))
        except ValueError:
            return 0

    def create_message(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            channel_id = int(path.split("/")[3])
        except ValueError:
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid channel")
        body = str(self.json_body().get("body", "")).strip()
        if not 1 <= len(body) <= 4000:
            return self.error(HTTPStatus.BAD_REQUEST, "Message must be 1–4000 characters")
        # Charged after the body is validated and read, so a malformed request
        # cannot spend somebody's allowance, and the connection stays framed.
        if not MESSAGE_LIMITER.allow(f"user:{user['id']}"):
            return self.error(HTTPStatus.TOO_MANY_REQUESTS,
                              "You are sending messages faster than this instance accepts. "
                              "Wait a moment and try again")
        created = utc_now()
        with connect() as db:
            context = channel_context(db, channel_id, user["id"])
            if not context:
                return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
            if self.refused_posting(db, context, user["id"], created):
                return
            message_id = db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                                    (channel_id, user["id"], body, created)).lastrowid
        message = {"id": message_id, "channel_id": channel_id, "body": body, "created_at": created,
                   "author_id": user["id"], "username": user["username"], "edited_at": None,
                   "attachment": None}
        BROKER.publish({"type": "message.created", **message})
        self.send_json(message, HTTPStatus.CREATED)

    def refused_posting(self, db, context, user_id, now=None, uploading=False):
        """Answer a contribution the channel's rules refuse; True once answered.

        Returns a boolean rather than the response, because `error` returns
        None: a caller testing its result would send the refusal and then carry
        on and do the thing anyway, answering one request twice.

        Each refusal says which rule stopped it. A composer that simply failed
        would leave someone retyping the same message into the same wall.
        """
        status, detail = may_post(db, context, user_id, uploading=uploading, now=now)
        if status == "role":
            self.error(HTTPStatus.FORBIDDEN,
                       f"Only {detail}s and above can post in #{context['name']}")
        elif status == "uploads_disabled":
            self.error(HTTPStatus.FORBIDDEN, f"#{context['name']} does not accept images")
        elif status == "slow_mode":
            self.error(HTTPStatus.TOO_MANY_REQUESTS,
                       f"Slow mode is on. Try again in {detail} second{'' if detail == 1 else 's'}")
        elif status == "voice_channel":
            self.error(HTTPStatus.CONFLICT, "Voice channels do not accept messages or images")
        else:
            return False
        return True

    def update_channel(self, path):
        user = self.require_user()
        if not user:
            return
        channel_id = self.channel_id_from(path)
        if channel_id is None:
            return
        body = self.json_body()
        try:
            slow_mode = int(body.get("slow_mode_seconds", 0))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid slow mode")
        with connect() as db:
            updated = update_channel_settings(db, channel_id, user["id"],
                                              str(body.get("post_min_role", "")), slow_mode,
                                              bool(body.get("uploads_allowed", True)))
        if updated is None:
            return self.error(HTTPStatus.FORBIDDEN,
                              "Only community administrators can change channel settings")
        if updated is False:
            return self.error(HTTPStatus.BAD_REQUEST,
                              f"Posting role must be one of {', '.join(sorted(POSTING_ROLES))} "
                              f"and slow mode 0–{MAX_SLOW_MODE_SECONDS} seconds")
        BROKER.publish({"type": "channel.updated", **updated})
        self.send_json(updated)

    def voice_channel_id(self, path):
        match = re.fullmatch(r"/api/channels/(\d+)/voice/(?:token|heartbeat|lease)", path)
        if not match:
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid voice channel")
        return int(match.group(1))

    def voice_token(self, path):
        user = self.require_user()
        if not user:
            return
        if not (MEDIA_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
            return self.error(HTTPStatus.SERVICE_UNAVAILABLE,
                              "Voice is not configured on this instance")
        if not MEDIA_TOKEN_LIMITER.allow(f"user:{user['id']}"):
            return self.error(HTTPStatus.TOO_MANY_REQUESTS,
                              "Voice join limit reached. Wait a minute and try again")
        channel_id = self.voice_channel_id(path)
        if channel_id is None:
            return
        fingerprint = str(self.json_body().get("key_fingerprint", ""))
        with connect() as database:
            status, lease = claim_lease(
                database, channel_id, user["id"], fingerprint,
                MAX_VOICE_PARTICIPANTS, VOICE_LEASE_SECONDS)
        if status == "invalid_key":
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid media key fingerprint")
        if status == "forbidden":
            return self.error(HTTPStatus.FORBIDDEN, "No access to this voice channel")
        if status == "not_voice":
            return self.error(HTTPStatus.CONFLICT, "That is not a voice channel")
        if status == "key_mismatch":
            # Two very different situations used to share one message that told
            # people to open a link nobody had given them. Say which one it is:
            # a running call needs its link, a lapsing one only needs a moment.
            if lease["live"]:
                others = ("Somebody else is" if lease["live"] == 1
                          else f"{lease['live']} other people are")
                return self.error(HTTPStatus.CONFLICT,
                                  f"{others} already in this call with a different encryption "
                                  "key. Ask them for the current call link and open it, or wait "
                                  "for the call to end before starting a new one")
            return self.error(HTTPStatus.CONFLICT,
                              "A call that has just ended is still releasing this channel. "
                              f"Try again in {max(1, lease['retry_after'])} seconds")
        if status == "full":
            return self.error(HTTPStatus.CONFLICT,
                              f"This voice channel is limited to {MAX_VOICE_PARTICIPANTS} participants")
        room = f"campfire-{channel_id}"
        token = issue_join_token(LIVEKIT_API_KEY, LIVEKIT_API_SECRET, room,
                                 user["id"], user["username"], fingerprint)
        self.send_json(lease | {"url": MEDIA_URL, "token": token, "room": room,
                                "max_participants": MAX_VOICE_PARTICIPANTS})

    def voice_heartbeat(self, path):
        user = self.require_user()
        if not user:
            return
        channel_id = self.voice_channel_id(path)
        if channel_id is None:
            return
        token = str(self.json_body().get("lease", ""))
        with connect() as database:
            renewed = renew_lease(database, channel_id, user["id"], token,
                                  VOICE_LEASE_SECONDS)
        if not renewed:
            return self.error(HTTPStatus.FORBIDDEN, "Voice lease expired or access was revoked")
        self.send_json({"ok": True})

    def voice_leave(self, path):
        user = self.require_user()
        if not user:
            return
        channel_id = self.voice_channel_id(path)
        if channel_id is None:
            return
        token = str(self.json_body().get("lease", ""))
        with connect() as database:
            released = release_lease(database, channel_id, user["id"], token)
        self.send_json({"ok": released})

    def message_id_from(self, path):
        try:
            return int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid message")

    def edit_message(self, path):
        user = self.require_user()
        if not user:
            return
        message_id = self.message_id_from(path)
        if message_id is None:
            return
        body = str(self.json_body().get("body", "")).strip()
        if not 1 <= len(body) <= 4000:
            return self.error(HTTPStatus.BAD_REQUEST, "Message must be 1–4000 characters")
        edited = utc_now()
        with connect() as db:
            message = visible_message(db, message_id, user["id"])
            if not message:
                return self.error(HTTPStatus.NOT_FOUND, "Message not found")
            if not may_edit(message):
                return self.error(HTTPStatus.FORBIDDEN, "Only the author can edit a message")
            if message["attachment_id"] is not None:
                return self.error(HTTPStatus.CONFLICT, "Shared images cannot be edited, only deleted")
            apply_edit(db, message_id, body, edited)
        updated = message_from_row(message) | {"body": body, "edited_at": edited}
        BROKER.publish({"type": "message.updated", **updated})
        self.send_json(updated)

    def delete_message(self, path):
        user = self.require_user()
        if not user:
            return
        message_id = self.message_id_from(path)
        if message_id is None:
            return
        with connect() as db:
            message = visible_message(db, message_id, user["id"])
            if not message:
                return self.error(HTTPStatus.NOT_FOUND, "Message not found")
            if not may_delete(message):
                return self.error(HTTPStatus.FORBIDDEN,
                                  "Only the author, or a moderator ranked above them, "
                                  "can delete a message")
            channel_id = message["channel_id"]
            orphaned_file = remove_message(db, message)
        if orphaned_file:
            (UPLOAD_DIR / orphaned_file).unlink(missing_ok=True)
        BROKER.publish({"type": "message.deleted", "id": message_id, "channel_id": channel_id})
        self.send_json({"ok": True})

    def upload_attachment(self, path):
        user = self.require_user()
        if not user:
            return
        if not UPLOAD_LIMITER.allow(f"user:{user['id']}"):
            return self.error(HTTPStatus.TOO_MANY_REQUESTS, "Upload limit reached. Wait a minute and try again")
        try:
            channel_id = int(path.split("/")[3])
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.LENGTH_REQUIRED, "A valid Content-Length is required")
        if not 1 <= length <= MAX_UPLOAD_BYTES:
            self.close_connection = True
            return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                              f"Images must be no larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB")
        with connect() as db:
            context = channel_context(db, channel_id, user["id"])
            if not context:
                self.close_connection = True
                return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
            # Refuse before image parsing and storage. The production edge has
            # already bounded and flow-controlled the raw request body.
            if self.refused_posting(db, context, user["id"], uploading=True):
                self.close_connection = True
                return
            reservation_token = secrets.token_hex(32)
            if not reserve_upload(db, reservation_token, MAX_STORAGE_BYTES, length):
                self.close_connection = True
                return self.error(HTTPStatus.INSUFFICIENT_STORAGE,
                                  "This instance is out of image storage. "
                                  "Ask an administrator to free space or raise the limit")
        return self.store_upload_body(channel_id, user, length, reservation_token)

    def store_upload_body(self, channel_id, user, length, reservation_token):
        """Read, validate, and atomically settle one capacity reservation."""
        try:
            return self._store_upload_body(channel_id, user, length, reservation_token)
        finally:
            # Successful settlement already removed it in the attachment
            # transaction. Every validation, disconnect, and I/O failure lands
            # here as well, so capacity cannot leak until the expiry sweep.
            with connect() as db:
                release_upload(db, reservation_token)

    def _store_upload_body(self, channel_id, user, length, reservation_token):
        content = self.rfile.read(length)
        if len(content) != length:
            self.close_connection = True
            return self.error(HTTPStatus.BAD_REQUEST, "Upload ended before the declared size")
        detected = detect_image_type(content)
        if not detected:
            return self.error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Only PNG, JPEG, GIF, and WebP images are accepted")
        mime_type, storage_extension, allowed_extensions = detected
        original_name = safe_original_name(self.headers.get("X-Campfire-Filename", "image"))
        if Path(original_name).suffix.lower() not in allowed_extensions:
            return self.error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Filename extension does not match the image")
        declared_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if declared_type not in {mime_type, "application/octet-stream"}:
            return self.error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Declared file type does not match the image")
        # Store the rebuilt image, never the bytes as uploaded: a photo carries
        # where and when it was taken until that is removed.
        content = strip_metadata(content, mime_type)
        if content is None or detect_image_type(content) != detected:
            return self.error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                              "That image could not be processed safely. Try re-exporting it")
        stored_size = len(content)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(UPLOAD_DIR, 0o700)  # mkdir's mode is subject to the umask; this is not
        storage_name = f"{secrets.token_hex(24)}{storage_extension}"
        destination = UPLOAD_DIR / storage_name
        try:
            with connect() as db:
                if not begin_reserved_upload_write(
                        db, reservation_token, MAX_STORAGE_BYTES, stored_size):
                    return self.error(HTTPStatus.INSUFFICIENT_STORAGE,
                                      "This instance is out of image storage. "
                                      "Ask an administrator to free space or raise the limit")
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stored:
                    stored.write(content)
                created = utc_now()
                attachment_id = db.execute("""INSERT INTO attachments
                  (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
                  VALUES(?,?,?,?,?,?,?)""",
                  (channel_id, user["id"], storage_name, original_name, mime_type,
                   stored_size, created)).lastrowid
                message_id = db.execute("""INSERT INTO messages
                  (channel_id,author_id,body,created_at,attachment_id) VALUES(?,?,?,?,?)""",
                  (channel_id, user["id"], "", created, attachment_id)).lastrowid
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        message = {
            "id": message_id, "channel_id": channel_id, "body": "", "created_at": created,
            "author_id": user["id"], "username": user["username"], "edited_at": None,
            "attachment": {"id": attachment_id, "name": original_name,
                           "mime_type": mime_type, "byte_size": stored_size},
        }
        BROKER.publish({"type": "message.created", **message})
        self.send_json(message, HTTPStatus.CREATED)

    def serve_attachment(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            attachment_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid attachment")
        with connect() as db:
            attachment = db.execute("""SELECT a.storage_name,a.mime_type,a.byte_size
              FROM attachments a JOIN channels ch ON ch.id=a.channel_id
              JOIN memberships m ON m.community_id=ch.community_id
              WHERE a.id=? AND m.user_id=?""", (attachment_id, user["id"])).fetchone()
        if not attachment:
            return self.error(HTTPStatus.NOT_FOUND, "Attachment not found")
        source = UPLOAD_DIR / attachment["storage_name"]
        # Measure the file rather than trusting the recorded size. The body is
        # streamed from disk, so a stored size that has drifted from the file
        # would declare a length the response never sends and desynchronize a
        # keep-alive connection.
        try:
            stored_size = source.stat().st_size if source.is_file() else None
        except OSError:
            stored_size = None
        if stored_size is None:
            return self.error(HTTPStatus.GONE, "Attachment data is unavailable")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", attachment["mime_type"])
        self.send_header("Content-Length", str(stored_size))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with source.open("rb") as content:
            while chunk := content.read(64 * 1024):
                self.wfile.write(chunk)

    def events(self):
        user = self.require_user()
        if not user:
            return
        session_token = self.authenticated_session_token
        subscription, became_online = BROKER.subscribe(user["id"], MAX_EVENT_STREAMS,
                                                       MAX_EVENT_STREAMS_PER_USER)
        if subscription is None:
            # Every stream costs a thread and a connection for as long as it is
            # open, so refuse clearly instead of exhausting the host.
            self.close_connection = True
            return self.error(HTTPStatus.SERVICE_UNAVAILABLE,
                              "Too many open connections. Close a tab and try again")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if became_online:
            BROKER.publish({"type": "presence.online", "user_id": user["id"]})
        last_keepalive = time.time()
        # One connection for the life of the stream: authorization is still
        # re-checked per event, but without reopening the database each time.
        db = connect()
        try:
            while True:
                try:
                    event = subscription.events.get(timeout=DISCONNECT_POLL_SECONDS)
                except queue.Empty:
                    if not self.session_is_active(db, session_token, user["id"]):
                        break
                    # Watch for a closed peer here rather than waiting for a write to
                    # fail, so someone leaving shows as offline in seconds.
                    if self.client_disconnected():
                        break
                    if self.report_missed_events(subscription):
                        last_keepalive = time.time()
                        continue
                    if time.time() - last_keepalive >= KEEPALIVE_SECONDS:
                        last_keepalive = time.time()
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    continue
                if not self.session_is_active(db, session_token, user["id"]):
                    break
                self.report_missed_events(subscription)
                if not self.event_visible_to(db, event, user):
                    continue
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                last_keepalive = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # An SSE response has no finite body or Content-Length. Once its
            # authorization is gone, closing the transport is the only signal
            # that tells EventSource this response truly ended.
            self.close_connection = True
            db.close()
            _, went_offline = BROKER.unsubscribe(subscription)
            if went_offline:
                BROKER.publish({"type": "presence.offline", "user_id": user["id"]})

    def session_is_active(self, database, session_token, user_id):
        """Re-check a stream's session so remote revocation closes it promptly.

        Keyed on the token digest, which is unique and never reissued. A row id
        would not be: revoking the newest session frees its id for the next
        login, and a login inside this poll's window would make the revoked
        stream test as live again and stay open indefinitely.
        """
        return database.execute(
            "SELECT 1 FROM sessions WHERE token=? AND user_id=? AND expires_at>?",
            (session_token, user_id, int(time.time())),
        ).fetchone() is not None

    def report_missed_events(self, subscription):
        """Tell a stream that fell behind to re-read, rather than losing events silently."""
        if not subscription.missed.is_set():
            return False
        subscription.missed.clear()
        self.wfile.write(b'data: {"type": "stream.reset"}\n\n')
        self.wfile.flush()
        return True

    def client_disconnected(self):
        """True once the peer has closed its end of an idle stream."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def event_visible_to(self, db, event, user):
        """Re-check authorization per event, since membership can change mid-stream."""
        subject = event["type"].split(".")[0]
        if subject == "presence":
            return shares_community(db, event["user_id"], user["id"])
        if event["type"] == "member.removed" and event["user_id"] == user["id"]:
            return True
        if subject in {"member", "channel"}:
            return is_member(db, event["community_id"], user["id"])
        return bool(self.member_channel(db, event["channel_id"], user["id"]))

    def static_file(self, path):
        decoded_path = unquote(path)
        if ("\\" in decoded_path
                or any(ord(character) < 32 or ord(character) == 127
                       for character in decoded_path)
                or any(segment in {".", ".."} for segment in decoded_path.split("/"))):
            return self.error(HTTPStatus.NOT_FOUND, "Not found")
        if path in _SCRIPT_PATHS:
            content = _STATIC_RESPONSES[path]
            content_type = "application/javascript; charset=utf-8"
        elif path in _STYLESHEET_PATHS:
            content = _STATIC_RESPONSES[path]
            content_type = "text/css; charset=utf-8"
        else:
            content = _INDEX_RESPONSE
            content_type = "text/html; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)


def run_retention_sweep():
    """One pass of the retention rules over every community that set them."""
    with connect() as db:
        removed, orphaned = purge_expired(db)
    for orphaned_file in orphaned:
        (UPLOAD_DIR / orphaned_file).unlink(missing_ok=True)
    for (community_id, channel_id), count in removed.items():
        # A count, not the ids: a client re-reads the channel rather than being
        # walked through what may be thousands of individual deletions.
        BROKER.publish({"type": "channel.purged", "community_id": community_id,
                        "channel_id": channel_id, "removed": count})
    return removed


def start_retention_sweeper():
    """Sweep on a timer, off the request path.

    A failed sweep must not end the thread; retention that silently stopped
    running would be worse than retention that logged a bad hour.
    """
    def sweep_forever():
        while True:
            try:
                run_retention_sweep()
            except Exception as failure:  # noqa: BLE001 - the loop must outlive one bad pass
                print(f"retention sweep failed: {failure}")
            time.sleep(RETENTION_SWEEP_SECONDS)
    threading.Thread(target=sweep_forever, daemon=True, name="retention").start()


def main():
    validate_configuration()
    with server_lock(DB_PATH), operation_lock(DB_PATH, exclusive=False):
        initialize_database()
        start_retention_sweeper()
        print(f"Campfire is running at http://{HOST}:{PORT}")
        from .asgi import serve
        serve()


if __name__ == "__main__":
    main()
