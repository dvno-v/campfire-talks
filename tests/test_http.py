"""End-to-end checks for authentication and request framing.

These drive a real socket because the behavior they cover — which credentials
the server accepts, and how many responses a request produces — is invisible to
tests that only call service functions.
"""

import contextlib
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
            cls.channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                        (cls.community_id, "general", database.utc_now())).lastrowid
            cls.owner_session = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (security.session_hash(cls.owner_session), cls.owner_id, int(time.time()) + 600))
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

    def signed_in_user(self, username, member=True):
        """Create a user with an active session, without paying for a password hash."""
        token = secrets.token_urlsafe(32)
        with database.connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 (username, "unused", database.utc_now())).lastrowid
            if member:
                db.execute("INSERT INTO memberships VALUES(?,?)", (self.community_id, user_id))
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (security.session_hash(token), user_id, int(time.time()) + 600))
        return token

    def user_id_for(self, username):
        with database.connect() as db:
            return db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    @contextlib.contextmanager
    def event_stream(self, cookie):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        connection.request("GET", "/api/events", headers={"Cookie": f"campfire_session={cookie}"})
        try:
            yield connection.getresponse()
        finally:
            connection.close()

    def await_event(self, stream, matches, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = stream.fp.readline()
            if not line:
                return None
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                if matches(event):
                    return event
        return None

    def post_message(self, cookie, body="original text"):
        status, message, _ = self.request("POST", f"/api/channels/{self.channel_id}/messages",
                                          {"body": body}, cookie=cookie)
        self.assertEqual(status, 201)
        return message

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

    def test_only_the_author_may_edit_over_http(self):
        author = self.signed_in_user("http_edit_author")
        bystander = self.signed_in_user("http_edit_bystander")
        message = self.post_message(author, "first draft")

        status, _, _ = self.request("PATCH", f"/api/messages/{message['id']}", {"body": "tampered"}, cookie=bystander)
        self.assertEqual(status, 403)
        status, _, _ = self.request("PATCH", f"/api/messages/{message['id']}", {"body": "rewritten"},
                                    cookie=self.owner_session)
        self.assertEqual(status, 403, "community ownership must not confer the right to rewrite others' words")

        status, updated, _ = self.request("PATCH", f"/api/messages/{message['id']}", {"body": "second draft"},
                                          cookie=author)
        self.assertEqual(status, 200)
        self.assertEqual(updated["body"], "second draft")
        self.assertIsNotNone(updated["edited_at"])

    def test_community_owner_may_delete_a_members_message(self):
        author = self.signed_in_user("http_moderated_author")
        message = self.post_message(author, "needs moderation")
        status, _, _ = self.request("DELETE", f"/api/messages/{message['id']}", cookie=self.owner_session)
        self.assertEqual(status, 200)
        status, _, _ = self.request("PATCH", f"/api/messages/{message['id']}", {"body": "back"}, cookie=author)
        self.assertEqual(status, 404)

    def test_bystanders_cannot_delete_and_outsiders_cannot_see(self):
        author = self.signed_in_user("http_delete_author")
        bystander = self.signed_in_user("http_delete_bystander")
        outsider = self.signed_in_user("http_delete_outsider", member=False)
        message = self.post_message(author)

        status, _, _ = self.request("DELETE", f"/api/messages/{message['id']}", cookie=bystander)
        self.assertEqual(status, 403)
        status, _, _ = self.request("DELETE", f"/api/messages/{message['id']}", cookie=outsider)
        self.assertEqual(status, 404, "outsiders must not learn that the message exists")
        status, _, _ = self.request("DELETE", f"/api/messages/{message['id']}", cookie=author)
        self.assertEqual(status, 200)

    def test_a_community_peer_is_told_when_someone_connects(self):
        watcher = self.signed_in_user("presence_watcher")
        joiner = self.signed_in_user("presence_joiner")
        joiner_id = self.user_id_for("presence_joiner")
        with self.event_stream(watcher) as stream:
            time.sleep(0.3)  # let the watcher's own subscription register first
            with self.event_stream(joiner):
                event = self.await_event(stream, lambda e: e.get("user_id") == joiner_id)
        self.assertIsNotNone(event, "the peer never received a presence event")
        self.assertEqual(event["type"], "presence.online")

    def test_the_member_list_reports_an_open_stream_as_online(self):
        viewer = self.signed_in_user("presence_viewer")
        with self.event_stream(viewer):
            time.sleep(0.3)
            _, payload, _ = self.request("GET", f"/api/communities/{self.community_id}/members", cookie=viewer)
        online = {member["username"] for member in payload["members"] if member["online"]}
        self.assertIn("presence_viewer", online)
        self.assertNotIn("http_owner", online, "an account with no open stream must read as offline")

    def test_non_object_body_is_rejected_once(self):
        token = self.signed_in_user("array_body_user")
        status, payload, _ = self.request("POST", "/api/communities", ["not", "an", "object"], cookie=token)
        self.assertEqual(status, 400)
        self.assertIn("JSON object", payload["error"])


if __name__ == "__main__":
    unittest.main()
