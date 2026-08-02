#!/usr/bin/env python3
"""Campfire HTTP application and request handlers."""

from __future__ import annotations

import hmac
import http.cookies
import json
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ACCESS_LOGS, HOST, MAX_UPLOAD_BYTES, PORT, PUBLIC_ORIGIN
from .config import SECURE_COOKIES, STATIC_DIR, UPLOAD_DIR
from .database import connect, initialize_database, message_from_row, utc_now
from .realtime import BROKER
from .security import AUTH_LIMITER, PASSWORD_ITERATIONS, UPLOAD_LIMITER, USERNAME_RE
from .security import invite_hash, password_hash, password_matches, session_hash, valid_invite
from .services.communities import list_active_invites, list_community_members
from .services.communities import revoke_invite as revoke_community_invite
from .uploads import detect_image_type, safe_original_name

class App(BaseHTTPRequestHandler):
    server_version = "Campfire"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(self), display-capture=(self), geolocation=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        if SECURE_COOKIES:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def log_message(self, fmt, *args):
        if ACCESS_LOGS:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    def json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 70_000:
                self.close_connection = True
                raise ValueError
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")

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
            return None
        with connect() as db:
            row = db.execute("""
              SELECT users.id, users.username FROM sessions
              JOIN users ON users.id = sessions.user_id
              WHERE token IN (?,?) AND expires_at > ?
            """, (session_hash(token.value), token.value, int(time.time()))).fetchone()
        return dict(row) if row else None

    def require_user(self):
        user = self.current_user()
        if user:
            return user
        if self.command == "POST":
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

    def auth_limit_key(self, scope):
        client = self.client_address[0] if self.client_address else "unknown"
        return f"{client}:{scope.casefold()}"

    def rate_limit_auth(self, scope):
        if AUTH_LIMITER.allow(self.auth_limit_key(scope)):
            return True
        self.error(HTTPStatus.TOO_MANY_REQUESTS, "Too many attempts. Wait a few minutes and try again")
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/me":
            user = self.current_user()
            return self.send_json({"user": user})
        if path == "/api/bootstrap":
            return self.bootstrap()
        if path == "/api/events":
            return self.events()
        if path.startswith("/api/communities/") and path.endswith("/members"):
            return self.community_members(path)
        if path.startswith("/api/communities/") and path.endswith("/invites"):
            return self.community_invites(path)
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
            "/api/logout": self.logout,
            "/api/communities": self.create_community,
            "/api/channels": self.create_channel,
            "/api/invites": self.create_invite,
            "/api/invites/join": self.join_invite,
        }
        if path in routes:
            return routes[path]()
        if path.startswith("/api/channels/") and path.endswith("/messages"):
            return self.create_message(path)
        if path.startswith("/api/channels/") and path.endswith("/uploads"):
            return self.upload_attachment(path)
        return self.error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self):
        if not self.valid_request_source():
            self.close_connection = True
            return self.error(HTTPStatus.FORBIDDEN, "Cross-origin request blocked")
        path = urlparse(self.path).path
        if path.startswith("/api/invites/"):
            return self.revoke_invite(path)
        return self.error(HTTPStatus.NOT_FOUND, "Not found")

    def register(self):
        body = self.json_body()
        if body is None:
            return
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
                    db.execute("INSERT INTO memberships VALUES(?,?)", (invitation["community_id"], user_id))
                else:
                    community = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                           (f"{username}'s place", user_id, utc_now())).lastrowid
                    db.execute("INSERT INTO memberships VALUES(?,?)", (community, user_id))
                    db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                               (community, "general", utc_now()))
        except sqlite3.IntegrityError:
            return self.error(HTTPStatus.CONFLICT, "That username is already taken")
        return self.start_session(user_id, username, HTTPStatus.CREATED)

    def login(self):
        body = self.json_body()
        if body is None:
            return
        attempted_username = str(body.get("username", "")).strip()
        if not self.rate_limit_auth(f"login:{attempted_username}"):
            return
        with connect() as db:
            row = db.execute("SELECT id,username,password_hash FROM users WHERE username = ? COLLATE NOCASE",
                             (attempted_username,)).fetchone()
        if not row or not password_matches(str(body.get("password", "")), row["password_hash"]):
            return self.error(HTTPStatus.UNAUTHORIZED, "Incorrect username or password")
        if not row["password_hash"].startswith(f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"):
            with connect() as db:
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (password_hash(str(body.get("password", ""))), row["id"]))
        AUTH_LIMITER.clear(self.auth_limit_key(f"login:{attempted_username}"))
        return self.start_session(row["id"], row["username"])

    def start_session(self, user_id, username, status=HTTPStatus.OK):
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + 60 * 60 * 24 * 30
        with connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (session_hash(token), user_id, expires))
        secure = "; Secure" if SECURE_COOKIES else ""
        cookie = f"campfire_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}"
        return self.send_json({"user": {"id": user_id, "username": username}}, status, {"Set-Cookie": cookie})

    def logout(self):
        cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("campfire_session")
        if token:
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token IN (?,?)", (session_hash(token.value), token.value))
        secure = "; Secure" if SECURE_COOKIES else ""
        return self.send_json({"ok": True}, headers={"Set-Cookie": f"campfire_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"})

    def bootstrap(self):
        user = self.require_user()
        if not user:
            return
        with connect() as db:
            rows = db.execute("""
              SELECT c.id community_id,c.name community_name,ch.id channel_id,ch.name channel_name
              FROM communities c JOIN memberships m ON m.community_id=c.id
              JOIN channels ch ON ch.community_id=c.id WHERE m.user_id=?
              ORDER BY c.id,ch.id
            """, (user["id"],)).fetchall()
        communities = {}
        for row in rows:
            community = communities.setdefault(row["community_id"], {
                "id": row["community_id"], "name": row["community_name"], "channels": []})
            community["channels"].append({"id": row["channel_id"], "name": row["channel_name"]})
        self.send_json({"user": user, "communities": list(communities.values())})

    def create_community(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        name = str((body or {}).get("name", "")).strip()
        if not 2 <= len(name) <= 40:
            return self.error(HTTPStatus.BAD_REQUEST, "Community name must be 2–40 characters")
        with connect() as db:
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      (name, user["id"], utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, user["id"]))
            channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                    (community_id, "general", utc_now())).lastrowid
        self.send_json({"id": community_id, "name": name, "channels": [{"id": channel_id, "name": "general"}]}, HTTPStatus.CREATED)

    def community_members(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            community_id = int(path.split("/")[3])
        except (ValueError, IndexError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        with connect() as database:
            members = list_community_members(database, community_id, user["id"])
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
            return self.error(HTTPStatus.FORBIDDEN, "Only the community owner can manage invites")
        self.send_json({"invites": invitations})

    def create_channel(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body() or {}
        name = re.sub(r"[^a-z0-9-]", "-", str(body.get("name", "")).lower().strip()).strip("-")
        try:
            community_id = int(body.get("community_id"))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid community")
        with connect() as db:
            owns = db.execute("SELECT 1 FROM communities WHERE id=? AND owner_id=?", (community_id, user["id"])).fetchone()
            if not owns:
                return self.error(HTTPStatus.FORBIDDEN, "Only the community owner can add channels")
            if not 2 <= len(name) <= 30:
                return self.error(HTTPStatus.BAD_REQUEST, "Channel name must be 2–30 characters")
            try:
                channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                        (community_id, name, utc_now())).lastrowid
            except sqlite3.IntegrityError:
                return self.error(HTTPStatus.CONFLICT, "That channel already exists")
        self.send_json({"id": channel_id, "name": name}, HTTPStatus.CREATED)

    def create_invite(self):
        user = self.require_user()
        if not user:
            return
        body = self.json_body() or {}
        try:
            community_id = int(body.get("community_id"))
            max_uses = min(25, max(1, int(body.get("max_uses", 10))))
            lifetime_hours = min(168, max(1, int(body.get("lifetime_hours", 24))))
        except (TypeError, ValueError):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid invite settings")
        token = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + lifetime_hours * 3600
        with connect() as db:
            owns = db.execute("SELECT 1 FROM communities WHERE id=? AND owner_id=?",
                              (community_id, user["id"])).fetchone()
            if not owns:
                return self.error(HTTPStatus.FORBIDDEN, "Only the community owner can create invites")
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
        body = self.json_body() or {}
        token = str(body.get("invite", "")).strip()
        with connect() as db:
            invitation = valid_invite(db, token)
            if not invitation:
                return self.error(HTTPStatus.NOT_FOUND, "Invite is invalid, expired, or fully used")
            existing = db.execute("SELECT 1 FROM memberships WHERE community_id=? AND user_id=?",
                                  (invitation["community_id"], user["id"])).fetchone()
            if existing:
                return self.error(HTTPStatus.CONFLICT, "You are already a member of that community")
            updated = db.execute("UPDATE invitations SET uses=uses+1 WHERE id=? AND uses<max_uses",
                                 (invitation["id"],))
            if updated.rowcount != 1:
                return self.error(HTTPStatus.CONFLICT, "Invite is fully used")
            db.execute("INSERT INTO memberships VALUES(?,?)", (invitation["community_id"], user["id"]))
        self.send_json({"id": invitation["community_id"], "name": invitation["community_name"]},
                       HTTPStatus.CREATED)

    def member_channel(self, db, channel_id, user_id):
        return db.execute("""SELECT ch.id FROM channels ch JOIN memberships m ON m.community_id=ch.community_id
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
        try:
            after = max(0, int(query.get("after", [0])[0]))
        except ValueError:
            after = 0
        with connect() as db:
            if not self.member_channel(db, channel_id, user["id"]):
                return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
            rows = db.execute("""SELECT m.id,m.channel_id,m.body,m.created_at,m.attachment_id,
              u.id author_id,u.username,a.original_name,a.mime_type,a.byte_size
              FROM messages m JOIN users u ON u.id=m.author_id
              LEFT JOIN attachments a ON a.id=m.attachment_id
              WHERE m.channel_id=? AND m.id>? ORDER BY m.id DESC LIMIT 100""", (channel_id, after)).fetchall()
        self.send_json({"messages": [message_from_row(row) for row in reversed(rows)]})

    def create_message(self, path):
        user = self.require_user()
        if not user:
            return
        try:
            channel_id = int(path.split("/")[3])
        except ValueError:
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid channel")
        body = str((self.json_body() or {}).get("body", "")).strip()
        if not 1 <= len(body) <= 4000:
            return self.error(HTTPStatus.BAD_REQUEST, "Message must be 1–4000 characters")
        created = utc_now()
        with connect() as db:
            if not self.member_channel(db, channel_id, user["id"]):
                return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
            message_id = db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                                    (channel_id, user["id"], body, created)).lastrowid
        message = {"id": message_id, "channel_id": channel_id, "body": body, "created_at": created,
                   "author_id": user["id"], "username": user["username"], "attachment": None}
        BROKER.publish(message)
        self.send_json(message, HTTPStatus.CREATED)

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
            if not self.member_channel(db, channel_id, user["id"]):
                self.close_connection = True
                return self.error(HTTPStatus.FORBIDDEN, "No access to this channel")
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
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        storage_name = f"{secrets.token_hex(24)}{storage_extension}"
        destination = UPLOAD_DIR / storage_name
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stored:
                stored.write(content)
            created = utc_now()
            with connect() as db:
                attachment_id = db.execute("""INSERT INTO attachments
                  (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
                  VALUES(?,?,?,?,?,?,?)""",
                  (channel_id, user["id"], storage_name, original_name, mime_type, length, created)).lastrowid
                message_id = db.execute("""INSERT INTO messages
                  (channel_id,author_id,body,created_at,attachment_id) VALUES(?,?,?,?,?)""",
                  (channel_id, user["id"], "", created, attachment_id)).lastrowid
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        message = {
            "id": message_id, "channel_id": channel_id, "body": "", "created_at": created,
            "author_id": user["id"], "username": user["username"],
            "attachment": {"id": attachment_id, "name": original_name,
                           "mime_type": mime_type, "byte_size": length},
        }
        BROKER.publish(message)
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
        if not source.is_file():
            return self.error(HTTPStatus.GONE, "Attachment data is unavailable")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", attachment["mime_type"])
        self.send_header("Content-Length", str(attachment["byte_size"]))
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
        channel = BROKER.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event = channel.get(timeout=20)
                    with connect() as db:
                        allowed = self.member_channel(db, event["channel_id"], user["id"])
                    if not allowed:
                        continue
                    payload = f"data: {json.dumps(event)}\n\n".encode()
                except queue.Empty:
                    payload = b": keepalive\n\n"
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            BROKER.unsubscribe(channel)

    def static_file(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            return self.error(HTTPStatus.NOT_FOUND, "Not found")
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)


def main():
    initialize_database()
    print(f"Campfire is running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()


if __name__ == "__main__":
    main()
