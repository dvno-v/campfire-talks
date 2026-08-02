"""End-to-end checks for authentication and request framing.

These drive a real socket because the behavior they cover — which credentials
the server accepts, and how many responses a request produces — is invisible to
tests that only call service functions.
"""

import hashlib
import http.client
import json
import os
import secrets
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

temporary = tempfile.TemporaryDirectory()
os.environ.setdefault("CAMPFIRE_DB", str(Path(temporary.name) / "http.db"))
os.environ.setdefault("CAMPFIRE_UPLOAD_DIR", str(Path(temporary.name) / "uploads"))

from campfire import database, security
from campfire.http import App

PASSWORD = "a sufficiently long password"


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize_database()
        with database.connect() as db:
            cls.owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                      ("http_owner", "unused", database.utc_now())).lastrowid
            cls.community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                          ("HTTP Tests", cls.owner_id, database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (cls.community_id, cls.owner_id))
            db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                       (cls.community_id, "general", database.utc_now()))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), App)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"campfire_session={cookie}"
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        payload = json.loads(response.read() or b"null")
        raw_cookie = response.getheader("Set-Cookie")
        session = raw_cookie.split(";", 1)[0].split("=", 1)[1] if raw_cookie else None
        connection.close()
        return response.status, payload, session

    def invite_token(self):
        token = secrets.token_urlsafe(16)
        with database.connect() as db:
            db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,created_at) VALUES(?,?,?,?,?,?)""",
              (self.community_id, self.owner_id, security.invite_hash(token),
               int(time.time()) + 600, 5, database.utc_now()))
        return token

    def signed_in_user(self, username):
        """Create a member with an active session, without paying for a password hash."""
        token = secrets.token_urlsafe(32)
        with database.connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 (username, "unused", database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (self.community_id, user_id))
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (security.session_hash(token), user_id, int(time.time()) + 600))
        return token

    def test_registration_stores_only_a_session_digest(self):
        status, _, session = self.request("POST", "/api/register", {
            "username": "digest_registrant", "password": PASSWORD, "invite": self.invite_token()})
        self.assertEqual(status, 201)
        with database.connect() as db:
            stored = db.execute("SELECT token FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)",
                                ("digest_registrant",)).fetchone()[0]
        self.assertNotEqual(stored, session)
        self.assertEqual(stored, hashlib.sha256(session.encode()).hexdigest())

    def test_stored_session_digest_is_not_a_credential(self):
        """Anyone reading the database must not be able to replay what it holds."""
        token = self.signed_in_user("digest_replay")
        digest = security.session_hash(token)
        _, payload, _ = self.request("GET", "/api/me", cookie=token)
        self.assertEqual(payload["user"]["username"], "digest_replay")
        _, payload, _ = self.request("GET", "/api/me", cookie=digest)
        self.assertIsNone(payload["user"])

    def test_logout_ignores_a_replayed_digest(self):
        token = self.signed_in_user("digest_logout")
        self.request("POST", "/api/logout", {}, cookie=security.session_hash(token))
        _, payload, _ = self.request("GET", "/api/me", cookie=token)
        self.assertEqual(payload["user"]["username"], "digest_logout")

    def test_usernames_cannot_differ_only_by_capitalization(self):
        status, _, _ = self.request("POST", "/api/register", {
            "username": "CaseUser", "password": PASSWORD, "invite": self.invite_token()})
        self.assertEqual(status, 201)
        status, payload, _ = self.request("POST", "/api/register", {
            "username": "caseuser", "password": PASSWORD, "invite": self.invite_token()})
        self.assertEqual(status, 409)
        self.assertIn("taken", payload["error"])

    def test_sign_in_resolves_the_registered_capitalization(self):
        self.request("POST", "/api/register", {
            "username": "MixedCase", "password": PASSWORD, "invite": self.invite_token()})
        status, payload, _ = self.request("POST", "/api/login", {"username": "mixedcase", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["username"], "MixedCase")

    def test_malformed_body_produces_exactly_one_response(self):
        """A second response to one request desynchronizes a keep-alive connection."""
        token = self.signed_in_user("framing_user")
        body = b"not json at all"
        request = (b"POST /api/channels HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   b"Content-Type: application/json\r\n"
                   + f"Cookie: campfire_session={token}\r\n".encode()
                   + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        connection.sendall(request)
        connection.settimeout(1.5)
        received = b""
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                received += chunk
        except socket.timeout:
            pass
        connection.close()
        self.assertEqual(received.count(b"HTTP/1.1 "), 1)
        self.assertIn(b"400 Bad Request", received)

    def test_non_object_body_is_rejected_once(self):
        token = self.signed_in_user("array_body_user")
        status, payload, _ = self.request("POST", "/api/communities", ["not", "an", "object"], cookie=token)
        self.assertEqual(status, 400)
        self.assertIn("JSON object", payload["error"])


if __name__ == "__main__":
    unittest.main()
