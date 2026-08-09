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
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

temporary = tempfile.TemporaryDirectory()
os.environ.setdefault("CAMPFIRE_DB", str(Path(temporary.name) / "http.db"))
os.environ.setdefault("CAMPFIRE_UPLOAD_DIR", str(Path(temporary.name) / "uploads"))

from campfire import config, database, realtime, security
from campfire import http as campfire_http
from campfire.http import App

PASSWORD = "a sufficiently long password"


def png_chunk(kind, payload):
    return (len(payload).to_bytes(4, "big") + kind + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big"))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize_database()
        with database.connect() as db:
            cls.owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                      ("http_owner", "unused", database.utc_now())).lastrowid
            cls.community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                          ("HTTP Tests", cls.owner_id, database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                       (cls.community_id, cls.owner_id))
            cls.channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                        (cls.community_id, "general", database.utc_now())).lastrowid
            cls.owner_session = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (security.session_hash(cls.owner_session), cls.owner_id,
                        int(time.time()) + 600, database.utc_now()))
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
                db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                           (self.community_id, user_id))
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (security.session_hash(token), user_id, int(time.time()) + 600,
                        database.utc_now()))
        return token

    def signed_in_with_password(self, username, password=PASSWORD):
        """A signed-in account whose real password is known.

        Registration is rate-limited on one bucket per client address, so tests
        that only need an account — rather than to exercise registering — build
        it directly instead of spending that shared allowance.
        """
        token = secrets.token_urlsafe(32)
        with database.connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 (username, security.password_hash(password),
                                  database.utc_now())).lastrowid
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (security.session_hash(token), user_id, int(time.time()) + 600,
                        database.utc_now()))
        return token

    def user_id_for(self, username):
        with database.connect() as db:
            return db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    def additional_session(self, user_id):
        token = secrets.token_urlsafe(32)
        with database.connect() as db:
            session_id = db.execute(
                "INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (security.session_hash(token), user_id, int(time.time()) + 600,
                 database.utc_now())).lastrowid
        return token, session_id

    @contextlib.contextmanager
    def event_stream(self, cookie, timeout=8):
        """Open an SSE stream. The timeout also bounds how long await_event blocks."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        connection.request("GET", "/api/events", headers={"Cookie": f"campfire_session={cookie}"})
        try:
            yield connection.getresponse()
        finally:
            connection.close()

    def await_event(self, stream, matches, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = stream.fp.readline()
            except TimeoutError:
                return None
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

    def test_owner_assigns_roles_and_privileges_take_effect_immediately(self):
        candidate = self.signed_in_user("http_role_candidate")
        candidate_id = self.user_id_for("http_role_candidate")
        outsider = self.signed_in_user("http_role_outsider", member=False)

        status, _, _ = self.request(
            "PATCH", f"/api/communities/{self.community_id}/members/{candidate_id}",
            {"role": "moderator"}, cookie=candidate)
        self.assertEqual(status, 403, "members must not promote themselves")
        status, _, _ = self.request(
            "PATCH", f"/api/communities/{self.community_id}/members/{candidate_id}",
            {"role": "moderator"}, cookie=outsider)
        self.assertEqual(status, 404, "outsiders must not learn whether a private community exists")
        status, _, _ = self.request(
            "PATCH", f"/api/communities/{self.community_id}/members/{candidate_id}",
            {"role": "owner"}, cookie=self.owner_session)
        self.assertEqual(status, 400, "ownership is not an assignable role")

        with self.event_stream(candidate) as stream:
            time.sleep(0.3)
            status, updated, _ = self.request(
                "PATCH", f"/api/communities/{self.community_id}/members/{candidate_id}",
                {"role": "moderator"}, cookie=self.owner_session)
            event = self.await_event(stream, lambda item: item.get("type") == "member.updated")
        self.assertEqual(status, 200)
        self.assertEqual(updated["role"], "moderator")
        self.assertIsNotNone(event)
        self.assertEqual(event["member"]["id"], candidate_id)

        author = self.signed_in_user("http_role_author")
        message = self.post_message(author, "moderated by role")
        status, _, _ = self.request("DELETE", f"/api/messages/{message['id']}", cookie=candidate)
        self.assertEqual(status, 200, "moderators should be able to remove another member's message")
        status, _, _ = self.request("POST", "/api/channels",
                                    {"community_id": self.community_id, "name": "moderator-denied"},
                                    cookie=candidate)
        self.assertEqual(status, 403)

        self.request("PATCH", f"/api/communities/{self.community_id}/members/{candidate_id}",
                     {"role": "administrator"}, cookie=self.owner_session)
        status, _, _ = self.request("POST", "/api/channels",
                                    {"community_id": self.community_id, "name": "admin-created"},
                                    cookie=candidate)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/invites",
                                    {"community_id": self.community_id}, cookie=candidate)
        self.assertEqual(status, 201)
        status, invitations, _ = self.request(
            "GET", f"/api/communities/{self.community_id}/invites", cookie=candidate)
        self.assertEqual(status, 200)
        own_invite = next(invite for invite in invitations["invites"]
                          if invite["creator_id"] == candidate_id)
        status, _, _ = self.request("DELETE", f"/api/invites/{own_invite['id']}", cookie=candidate)
        self.assertEqual(status, 200)

        _, bootstrap, _ = self.request("GET", "/api/bootstrap", cookie=candidate)
        community = next(item for item in bootstrap["communities"] if item["id"] == self.community_id)
        self.assertEqual(community["role"], "administrator")

    def test_kicks_allow_rejoining_while_bans_block_it_until_removed(self):
        moderator = self.signed_in_user("http_kickban_moderator")
        moderator_id = self.user_id_for("http_kickban_moderator")
        self.request("PATCH", f"/api/communities/{self.community_id}/members/{moderator_id}",
                     {"role": "moderator"}, cookie=self.owner_session)

        kicked = self.signed_in_user("http_kicked_member")
        kicked_id = self.user_id_for("http_kicked_member")
        with self.event_stream(kicked) as stream:
            time.sleep(0.3)
            status, _, _ = self.request(
                "DELETE", f"/api/communities/{self.community_id}/members/{kicked_id}",
                cookie=moderator)
            event = self.await_event(stream, lambda item: item.get("type") == "member.removed")
        self.assertEqual(status, 200)
        self.assertIsNotNone(event, "the kicked account must learn that its access changed")
        self.assertIs(event["banned"], False)
        status, _, _ = self.request("POST", "/api/invites/join",
                                    {"invite": self.invite_token()}, cookie=kicked)
        self.assertEqual(status, 201, "a kick must not silently become a permanent ban")

        banned = self.signed_in_user("http_banned_member")
        banned_id = self.user_id_for("http_banned_member")
        token = self.invite_token()
        token_hash = security.invite_hash(token)
        with self.event_stream(banned) as stream:
            time.sleep(0.3)
            status, _, _ = self.request(
                "POST", f"/api/communities/{self.community_id}/members/{banned_id}/ban",
                {}, cookie=moderator)
            event = self.await_event(stream, lambda item: item.get("type") == "member.removed")
        self.assertEqual(status, 200)
        self.assertIsNotNone(event)
        self.assertIs(event["banned"], True)

        status, payload, _ = self.request("POST", "/api/invites/join", {"invite": token}, cookie=banned)
        self.assertEqual(status, 403)
        self.assertIn("banned", payload["error"])
        with database.connect() as db:
            uses = db.execute("SELECT uses FROM invitations WHERE token_hash=?", (token_hash,)).fetchone()[0]
        self.assertEqual(uses, 0, "refusing a banned account must not consume the invite")

        status, payload, _ = self.request(
            "GET", f"/api/communities/{self.community_id}/bans", cookie=moderator)
        self.assertEqual(status, 200)
        self.assertIn(banned_id, {entry["user_id"] for entry in payload["bans"]})
        status, _, _ = self.request(
            "DELETE", f"/api/communities/{self.community_id}/bans/{banned_id}", cookie=moderator)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/invites/join", {"invite": token}, cookie=banned)
        self.assertEqual(status, 201)

    def test_moderators_cannot_remove_peers_or_higher_roles(self):
        moderator = self.signed_in_user("http_hierarchy_moderator")
        moderator_id = self.user_id_for("http_hierarchy_moderator")
        administrator = self.signed_in_user("http_hierarchy_administrator")
        administrator_id = self.user_id_for("http_hierarchy_administrator")
        self.request("PATCH", f"/api/communities/{self.community_id}/members/{moderator_id}",
                     {"role": "moderator"}, cookie=self.owner_session)
        self.request("PATCH", f"/api/communities/{self.community_id}/members/{administrator_id}",
                     {"role": "administrator"}, cookie=self.owner_session)

        status, _, _ = self.request(
            "POST", f"/api/communities/{self.community_id}/members/{administrator_id}/ban",
            {}, cookie=moderator)
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "DELETE", f"/api/communities/{self.community_id}/members/{moderator_id}",
            cookie=moderator)
        self.assertEqual(status, 403, "moderators must not remove themselves")

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

    def test_a_departure_is_announced_without_waiting_for_a_keepalive(self):
        watcher = self.signed_in_user("departure_watcher")
        leaver = self.signed_in_user("departure_leaver")
        leaver_id = self.user_id_for("departure_leaver")
        with self.event_stream(watcher) as stream:
            time.sleep(0.3)
            leaving = socket.create_connection(("127.0.0.1", self.port), timeout=8)
            leaving.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                            + f"Cookie: campfire_session={leaver}\r\n\r\n".encode())
            self.assertIsNotNone(self.await_event(stream, lambda e: e.get("user_id") == leaver_id))
            leaving.close()
            started = time.time()
            event = self.await_event(stream, lambda e: e.get("type") == "presence.offline"
                                     and e.get("user_id") == leaver_id, timeout=15)
        self.assertIsNotNone(event, "departure never reached the peer")
        self.assertLess(time.time() - started, 10, "departure should not wait for the 20s keepalive")

    def test_arrivals_and_new_channels_reach_existing_members(self):
        watcher = self.signed_in_user("liveupdate_watcher")
        with self.event_stream(watcher) as stream:
            time.sleep(0.3)
            status, _, _ = self.request("POST", "/api/register", {
                "username": "liveupdate_arrival", "password": PASSWORD, "invite": self.invite_token()})
            self.assertEqual(status, 201)
            joined = self.await_event(stream, lambda e: e.get("type") == "member.joined")
            self.assertIsNotNone(joined, "existing members were not told about the arrival")
            self.assertEqual(joined["member"]["username"], "liveupdate_arrival")
            self.assertEqual(joined["member"]["role"], "member")
            self.assertIs(joined["member"]["online"], False)

            status, _, _ = self.request("POST", "/api/channels",
                                        {"community_id": self.community_id, "name": "live-channel"},
                                        cookie=self.owner_session)
            self.assertEqual(status, 201)
            created = self.await_event(stream, lambda e: e.get("type") == "channel.created")
        self.assertIsNotNone(created, "existing members were not told about the new channel")
        self.assertEqual(created["name"], "live-channel")

    def test_community_events_stay_inside_the_community(self):
        outsider = self.signed_in_user("liveupdate_outsider", member=False)
        with self.event_stream(outsider, timeout=1.5) as stream:
            time.sleep(0.3)
            self.request("POST", "/api/channels", {"community_id": self.community_id, "name": "private-channel"},
                         cookie=self.owner_session)
            leaked = self.await_event(stream, lambda e: e.get("type") == "channel.created", timeout=1.5)
        self.assertIsNone(leaked, "a non-member must not learn about another community's channels")

    def upload(self, cookie, content, filename="photo.png", content_type="image/png"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", f"/api/channels/{self.channel_id}/uploads", content,
                           {"Content-Type": content_type, "X-Campfire-Filename": filename,
                            "Cookie": f"campfire_session={cookie}"})
        response = connection.getresponse()
        payload = json.loads(response.read() or b"null")
        connection.close()
        return response.status, payload

    def test_an_uploaded_photo_is_stored_without_its_location(self):
        user = self.signed_in_user("exif_uploader")
        pixels = zlib.compress(b"\x00\xff\x00\x00")
        original = (b"\x89PNG\r\n\x1a\n"
                    + png_chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
                    + png_chunk(b"eXIf", b"GPS 51.5074 N 0.1278 W")
                    + png_chunk(b"IDAT", pixels)
                    + png_chunk(b"IEND", b""))
        self.assertIn(b"GPS", original)

        status, message = self.upload(user, original)
        self.assertEqual(status, 201)
        with database.connect() as db:
            stored_name = db.execute("SELECT storage_name FROM attachments WHERE id=?",
                                     (message["attachment"]["id"],)).fetchone()[0]
        stored = (config.UPLOAD_DIR / stored_name).read_bytes()
        self.assertNotIn(b"GPS", stored, "the location was written to disk")
        self.assertIn(pixels, stored, "the image itself must survive")
        self.assertEqual(message["attachment"]["byte_size"], len(stored),
                         "the recorded size must describe the rewritten file, not the upload")

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", f"/api/attachments/{message['attachment']['id']}",
                           headers={"Cookie": f"campfire_session={user}"})
        response = connection.getresponse()
        served = response.read()
        connection.close()
        self.assertEqual(int(response.getheader("Content-Length")), len(served))
        self.assertNotIn(b"GPS", served)

    def test_an_image_that_cannot_be_rebuilt_is_refused(self):
        user = self.signed_in_user("broken_uploader")
        truncated = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", b"\x00" * 13)[:-2]
        status, payload = self.upload(user, truncated)
        self.assertEqual(status, 415)
        self.assertIn("could not be processed safely", payload["error"])

    def test_a_client_that_fell_behind_is_told_to_re_read(self):
        viewer = self.signed_in_user("resync_viewer")
        with self.event_stream(viewer) as stream:
            time.sleep(0.3)
            # Reaching into the broker makes the overflow deterministic; provoking
            # it by flooding would race the consumer draining the queue.
            for subscription in list(realtime.BROKER._subscriptions):
                subscription.missed.set()
            event = self.await_event(stream, lambda e: e.get("type") == "stream.reset", timeout=8)
        self.assertIsNotNone(event, "a stream that dropped events never told the client")

    def test_an_extra_stream_is_refused_rather_than_exhausting_the_host(self):
        original = campfire_http.MAX_EVENT_STREAMS_PER_USER
        campfire_http.MAX_EVENT_STREAMS_PER_USER = 1
        try:
            user = self.signed_in_user("stream_limit_user")
            with self.event_stream(user):
                time.sleep(0.3)
                status, payload, _ = self.request("GET", "/api/events", cookie=user)
        finally:
            campfire_http.MAX_EVENT_STREAMS_PER_USER = original
        self.assertEqual(status, 503)
        self.assertIn("Too many open connections", payload["error"])

    def test_the_member_list_reports_an_open_stream_as_online(self):
        viewer = self.signed_in_user("presence_viewer")
        with self.event_stream(viewer):
            time.sleep(0.3)
            _, payload, _ = self.request("GET", f"/api/communities/{self.community_id}/members", cookie=viewer)
        online = {member["username"] for member in payload["members"] if member["online"]}
        self.assertIn("presence_viewer", online)
        self.assertNotIn("http_owner", online, "an account with no open stream must read as offline")

    def test_unread_state_travels_with_bootstrap_and_clears_when_read(self):
        reader = self.signed_in_user("unread_reader")
        writer = self.signed_in_user("unread_writer")
        message = self.post_message(writer, "something to catch up on")

        _, payload, _ = self.request("GET", "/api/bootstrap", cookie=reader)
        channels = {channel["id"]: channel
                    for community in payload["communities"] for channel in community["channels"]}
        self.assertGreaterEqual(channels[self.channel_id]["unread"], 1)
        self.assertIsNone(channels[self.channel_id]["notify"])
        self.assertEqual(payload["notifications"]["default_mode"], "all")

        status, marked, _ = self.request("POST", f"/api/channels/{self.channel_id}/read",
                                         {"message_id": message["id"]}, cookie=reader)
        self.assertEqual(status, 200)
        self.assertEqual(marked["unread"], 0)
        self.assertEqual(marked["last_read_message_id"], message["id"])

        _, unread, _ = self.request("GET", "/api/unread", cookie=reader)
        by_channel = {entry["channel_id"]: entry for entry in unread["channels"]}
        self.assertEqual(by_channel[self.channel_id]["unread"], 0)

    def test_a_non_member_cannot_mark_a_channel_read_or_mute_it(self):
        outsider = self.signed_in_user("unread_outsider_http", member=False)
        status, _, _ = self.request("POST", f"/api/channels/{self.channel_id}/read",
                                    {"message_id": 1}, cookie=outsider)
        self.assertEqual(status, 403)
        status, _, _ = self.request("PATCH", f"/api/channels/{self.channel_id}/notifications",
                                    {"mode": "none"}, cookie=outsider)
        self.assertEqual(status, 403)

    def test_notification_modes_are_stored_and_unknown_ones_refused(self):
        user = self.signed_in_user("notify_http_user")
        status, payload, _ = self.request("PATCH", "/api/preferences/notifications",
                                          {"default_mode": "none"}, cookie=user)
        self.assertEqual(status, 200)
        self.assertEqual(payload["default_mode"], "none")
        status, _, _ = self.request("PATCH", "/api/preferences/notifications",
                                    {"default_mode": "everything"}, cookie=user)
        self.assertEqual(status, 400)

        status, payload, _ = self.request("PATCH", f"/api/channels/{self.channel_id}/notifications",
                                          {"mode": "all"}, cookie=user)
        self.assertEqual(status, 200)
        self.assertEqual(payload["notify"], "all")
        status, payload, _ = self.request("PATCH", f"/api/channels/{self.channel_id}/notifications",
                                          {"mode": "default"}, cookie=user)
        self.assertEqual(status, 200)
        self.assertIsNone(payload["notify"], "clearing an override restores the account default")
        status, _, _ = self.request("PATCH", f"/api/channels/{self.channel_id}/notifications",
                                    {"mode": "sometimes"}, cookie=user)
        self.assertEqual(status, 400)

    def test_registering_with_an_invite_settles_the_backlog(self):
        self.post_message(self.owner_session, "said before you arrived")
        status, _, session = self.request("POST", "/api/register", {
            "username": "unread_invited", "password": PASSWORD, "invite": self.invite_token()})
        self.assertEqual(status, 201)
        _, payload, _ = self.request("GET", "/api/unread", cookie=session)
        by_channel = {entry["channel_id"]: entry for entry in payload["channels"]}
        self.assertEqual(by_channel[self.channel_id]["unread"], 0,
                         "a new member did not miss the conversation from before they were invited")

    def test_sessions_are_private_and_remote_revocation_closes_the_live_stream(self):
        primary = self.signed_in_user("session_manager")
        user_id = self.user_id_for("session_manager")
        remote, remote_id = self.additional_session(user_id)

        status, payload, _ = self.request("GET", "/api/sessions", cookie=primary)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["sessions"]), 2)
        self.assertEqual(sum(session["current"] for session in payload["sessions"]), 1)
        self.assertEqual(set(payload["sessions"][0]),
                         {"id", "created_at", "expires_at", "current"},
                         "session tokens and device metadata must not cross the API")

        outsider = self.signed_in_user("session_outsider", member=False)
        status, _, _ = self.request("DELETE", f"/api/sessions/{remote_id}", cookie=outsider)
        self.assertEqual(status, 404, "another account must not be able to revoke or enumerate a session")

        with self.event_stream(remote, timeout=6) as stream:
            self.assertEqual(stream.status, 200)
            time.sleep(0.3)
            status, result, _ = self.request("DELETE", f"/api/sessions/{remote_id}", cookie=primary)
            self.assertEqual(status, 200)
            self.assertIs(result["current"], False)
            disconnected = False
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    line = stream.fp.readline()
                except TimeoutError:
                    break
                if not line:
                    disconnected = True
                    break
        self.assertTrue(disconnected, "a revoked session's live stream stayed connected")

        _, payload, _ = self.request("GET", "/api/me", cookie=remote)
        self.assertIsNone(payload["user"])
        _, payload, _ = self.request("GET", "/api/sessions", cookie=primary)
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertIs(payload["sessions"][0]["current"], True)

    def test_password_change_verifies_the_current_password_and_revokes_other_sessions(self):
        status, _, current = self.request("POST", "/api/register", {
            "username": "password_changer", "password": PASSWORD, "invite": self.invite_token()})
        self.assertEqual(status, 201)
        remote, _ = self.additional_session(self.user_id_for("password_changer"))
        replacement = "a different sufficiently long password"

        status, payload, _ = self.request("PATCH", "/api/account/password", {
            "current_password": "not the current password", "new_password": replacement}, cookie=current)
        self.assertEqual(status, 403)
        self.assertIn("incorrect", payload["error"].lower())

        status, payload, rotated = self.request("PATCH", "/api/account/password", {
            "current_password": PASSWORD, "new_password": replacement}, cookie=current)
        self.assertEqual(status, 200)
        self.assertEqual(payload["revoked_sessions"], 1)
        self.assertTrue(rotated and rotated != current,
                        "a password change must reissue the session that confirmed it")

        _, payload, _ = self.request("GET", "/api/me", cookie=rotated)
        self.assertEqual(payload["user"]["username"], "password_changer")
        _, payload, _ = self.request("GET", "/api/me", cookie=current)
        self.assertIsNone(payload["user"],
                          "the cookie in play before the change must stop working too")
        _, payload, _ = self.request("GET", "/api/me", cookie=remote)
        self.assertIsNone(payload["user"])
        status, _, _ = self.request("POST", "/api/login", {
            "username": "password_changer", "password": PASSWORD})
        self.assertEqual(status, 401)
        status, _, replacement_session = self.request("POST", "/api/login", {
            "username": "password_changer", "password": replacement})
        self.assertEqual(status, 200)
        self.assertTrue(replacement_session)

    def test_a_rejected_new_password_does_not_spend_the_rate_limit(self):
        """Fumbling the form is the client's own mistake, not an attempt to guess."""
        cookie = self.signed_in_with_password("limit_spender")
        # Comfortably past the eight attempts a five-minute window allows.
        for _ in range(12):
            status, _, _ = self.request("PATCH", "/api/account/password", {
                "current_password": PASSWORD, "new_password": "short"}, cookie=cookie)
            self.assertEqual(status, 400)

        replacement = "a different sufficiently long password"
        status, _, rotated = self.request("PATCH", "/api/account/password", {
            "current_password": PASSWORD, "new_password": replacement}, cookie=cookie)
        self.assertEqual(status, 200, "validation failures must not lock the account out")
        self.assertTrue(rotated)

    def test_a_wrong_current_password_still_spends_the_rate_limit(self):
        cookie = self.signed_in_with_password("limit_guesser")
        replacement = "a different sufficiently long password"
        seen = set()
        for _ in range(12):
            status, _, _ = self.request("PATCH", "/api/account/password", {
                "current_password": "not the current password",
                "new_password": replacement}, cookie=cookie)
            seen.add(status)
        self.assertEqual(seen, {403, 429}, "guessing the current password must still be limited")

    def test_the_current_session_can_sign_itself_out(self):
        current = self.signed_in_user("self_revoker")
        _, payload, _ = self.request("GET", "/api/sessions", cookie=current)
        session_id = payload["sessions"][0]["id"]
        status, payload, cleared_cookie = self.request(
            "DELETE", f"/api/sessions/{session_id}", cookie=current)
        self.assertEqual(status, 200)
        self.assertIs(payload["current"], True)
        self.assertEqual(cleared_cookie, "")
        _, payload, _ = self.request("GET", "/api/me", cookie=current)
        self.assertIsNone(payload["user"])

    def download(self, path, cookie):
        """Like `request`, but keeps the headers a download depends on."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path, headers={"Cookie": f"campfire_session={cookie}"})
        response = connection.getresponse()
        payload = json.loads(response.read() or b"null")
        disposition = response.getheader("Content-Disposition")
        connection.close()
        return response.status, payload, disposition

    def test_account_export_is_private_and_offered_as_a_download(self):
        token = self.signed_in_user("exporting_user")
        user_id = self.user_id_for("exporting_user")
        with database.connect() as db:
            db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                       (self.channel_id, user_id, "something I wrote", database.utc_now()))
            db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                       (self.channel_id, self.owner_id, "something they wrote", database.utc_now()))

        status, payload, _ = self.request("GET", "/api/account/export")
        self.assertEqual(status, 401, "an export must never be readable without a session")

        status, export, disposition = self.download("/api/account/export", token)
        self.assertEqual(status, 200)
        self.assertEqual(disposition, 'attachment; filename="campfire-exporting_user-'
                                      f'{export["exported_at"][:10]}.json"')
        self.assertEqual([entry["body"] for entry in export["messages"]], ["something I wrote"])
        self.assertNotIn("password_hash", export["account"])
        self.assertTrue(all("token" not in session for session in export["sessions"]))

    def test_account_deletion_needs_the_password_and_erases_what_the_account_wrote(self):
        status, _, cookie = self.request("POST", "/api/register", {
            "username": "departing_user", "password": PASSWORD, "invite": self.invite_token()})
        self.assertEqual(status, 201)
        user_id = self.user_id_for("departing_user")
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stored = config.UPLOAD_DIR / "departing_user_stored.png"
        stored.write_bytes(b"pretend image")
        with database.connect() as db:
            attachment_id = db.execute("""INSERT INTO attachments
              (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (self.channel_id, user_id, stored.name, "photo.png", "image/png", 13,
               database.utc_now())).lastrowid
            message_id = db.execute("""INSERT INTO messages
              (channel_id,author_id,body,created_at,attachment_id) VALUES(?,?,?,?,?)""",
              (self.channel_id, user_id, "goodbye", database.utc_now(), attachment_id)).lastrowid

        status, payload, _ = self.request("GET", "/api/account/deletion", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["messages"], 1)
        self.assertEqual(payload["attachments"], 1)

        status, payload, _ = self.request("DELETE", "/api/account",
                                          {"current_password": "not the password"}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertTrue(stored.exists(), "a refused deletion must not touch stored files")

        with self.event_stream(self.owner_session) as stream:
            time.sleep(0.3)
            status, payload, cleared_cookie = self.request(
                "DELETE", "/api/account", {"current_password": PASSWORD}, cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(cleared_cookie, "")
            removal = self.await_event(stream, lambda event: event["type"] == "member.removed")
        self.assertEqual(removal["user_id"], user_id)
        self.assertIs(removal["deleted_account"], True,
                      "remaining members need to know history changed, not just the member list")

        _, payload, _ = self.request("GET", "/api/me", cookie=cookie)
        self.assertIsNone(payload["user"])
        self.assertFalse(stored.exists(), "a deleted account's images must leave the disk")
        with database.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sessions WHERE user_id=?", (user_id,)).fetchone())

    def test_channel_rules_are_enforced_on_posting_and_uploading(self):
        member = self.signed_in_user("http_rules_member")
        status, _, _ = self.request("PATCH", f"/api/channels/{self.channel_id}",
                                    {"post_min_role": "moderator", "slow_mode_seconds": 0,
                                     "uploads_allowed": False}, cookie=member)
        self.assertEqual(status, 403, "a member cannot rewrite the rules they are held to")

        status, updated, _ = self.request("PATCH", f"/api/channels/{self.channel_id}",
                                          {"post_min_role": "moderator", "slow_mode_seconds": 0,
                                           "uploads_allowed": False}, cookie=self.owner_session)
        self.assertEqual(status, 200)
        self.assertEqual(updated["post_min_role"], "moderator")
        try:
            status, payload, _ = self.request("POST", f"/api/channels/{self.channel_id}/messages",
                                              {"body": "may i speak"}, cookie=member)
            self.assertEqual(status, 403)
            self.assertIn("moderator", payload["error"])
            # Reading is deliberately untouched by a posting rule.
            status, _, _ = self.request("GET", f"/api/channels/{self.channel_id}/messages",
                                        cookie=member)
            self.assertEqual(status, 200)

            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            connection.request("POST", f"/api/channels/{self.channel_id}/uploads", b"x" * 8,
                               {"Content-Type": "image/png", "Content-Length": "8",
                                "Cookie": f"campfire_session={self.owner_session}"})
            response = connection.getresponse()
            body = json.loads(response.read() or b"null")
            connection.close()
            self.assertEqual(response.status, 403)
            self.assertIn("does not accept images", body["error"])
        finally:
            self.request("PATCH", f"/api/channels/{self.channel_id}",
                         {"post_min_role": "member", "slow_mode_seconds": 0,
                          "uploads_allowed": True}, cookie=self.owner_session)

    def test_slow_mode_refuses_a_second_message_with_the_wait(self):
        member = self.signed_in_user("http_slow_member")
        self.request("PATCH", f"/api/channels/{self.channel_id}",
                     {"post_min_role": "member", "slow_mode_seconds": 60,
                      "uploads_allowed": True}, cookie=self.owner_session)
        try:
            self.assertEqual(self.request("POST", f"/api/channels/{self.channel_id}/messages",
                                          {"body": "first"}, cookie=member)[0], 201)
            status, payload, _ = self.request("POST", f"/api/channels/{self.channel_id}/messages",
                                              {"body": "second"}, cookie=member)
            self.assertEqual(status, 429)
            self.assertIn("Slow mode", payload["error"])
            # The owner is a moderator-or-above, so the same channel stays open to them.
            self.assertEqual(self.request("POST", f"/api/channels/{self.channel_id}/messages",
                                          {"body": "unhindered"}, cookie=self.owner_session)[0], 201)
        finally:
            self.request("PATCH", f"/api/channels/{self.channel_id}",
                         {"post_min_role": "member", "slow_mode_seconds": 0,
                          "uploads_allowed": True}, cookie=self.owner_session)

    def test_retention_is_administrator_only_and_applies_immediately(self):
        member = self.signed_in_user("http_retention_member")
        status, _, _ = self.request("PATCH", f"/api/communities/{self.community_id}/retention",
                                    {"message_days": 30, "attachment_days": 30}, cookie=member)
        self.assertEqual(status, 403)
        status, payload, _ = self.request("PATCH", f"/api/communities/{self.community_id}/retention",
                                          {"message_days": 99999, "attachment_days": 0},
                                          cookie=self.owner_session)
        self.assertEqual(status, 400)

        stale = self.post_message(self.owner_session, "long ago")
        with database.connect() as db:
            db.execute("UPDATE messages SET created_at='2020-01-01T00:00:00Z' WHERE id=?",
                       (stale["id"],))
        fresh = self.post_message(self.owner_session, "written just now")

        with self.event_stream(self.owner_session) as stream:
            time.sleep(0.3)
            status, stored, _ = self.request(
                "PATCH", f"/api/communities/{self.community_id}/retention",
                {"message_days": 30, "attachment_days": 0}, cookie=self.owner_session)
            self.assertEqual(status, 200)
            self.assertEqual(stored["message_days"], 30)
            purge = self.await_event(stream, lambda event: event["type"] == "channel.purged")
        try:
            self.assertIsNotNone(purge, "members need telling that history moved underneath them")
            self.assertEqual(purge["channel_id"], self.channel_id)
            with database.connect() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM messages WHERE id=?",
                                             (stale["id"],)).fetchone(),
                                  "setting a window must apply now, not at the next sweep")
                self.assertIsNotNone(db.execute("SELECT 1 FROM messages WHERE id=?",
                                                (fresh["id"],)).fetchone())
        finally:
            self.request("PATCH", f"/api/communities/{self.community_id}/retention",
                         {"message_days": 0, "attachment_days": 0}, cookie=self.owner_session)

    def test_storage_reporting_and_the_upload_ceiling(self):
        member = self.signed_in_user("http_storage_member")
        status, _, _ = self.request("GET", "/api/storage", cookie=member)
        self.assertEqual(status, 403)

        status, report, _ = self.request("GET", "/api/storage", cookie=self.owner_session)
        self.assertEqual(status, 200)
        self.assertEqual(report["limit_bytes"], 0, "no ceiling is configured by default")
        self.assertIsNone(report["available_bytes"])
        self.assertIn(self.community_id, [entry["id"] for entry in report["communities"]])

        original = campfire_http.MAX_STORAGE_BYTES
        campfire_http.MAX_STORAGE_BYTES = 1
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            connection.request("POST", f"/api/channels/{self.channel_id}/uploads", b"x" * 64,
                               {"Content-Type": "image/png", "Content-Length": "64",
                                "Cookie": f"campfire_session={self.owner_session}"})
            response = connection.getresponse()
            payload = json.loads(response.read() or b"null")
            connection.close()
            self.assertEqual(response.status, 507)
            self.assertIn("out of image storage", payload["error"])
        finally:
            campfire_http.MAX_STORAGE_BYTES = original

    def test_non_object_body_is_rejected_once(self):
        token = self.signed_in_user("array_body_user")
        status, payload, _ = self.request("POST", "/api/communities", ["not", "an", "object"], cookie=token)
        self.assertEqual(status, 400)
        self.assertIn("JSON object", payload["error"])


if __name__ == "__main__":
    unittest.main()
