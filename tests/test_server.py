import os
import sqlite3
import stat
import tempfile
import time
import unittest
from ipaddress import ip_network
from pathlib import Path

temporary = tempfile.TemporaryDirectory()
os.environ["CAMPFIRE_DB"] = str(Path(temporary.name) / "test.db")

from campfire import database, realtime, security, uploads
from campfire.services.communities import list_active_invites, list_community_members, revoke_invite
from campfire.services.communities import shares_community
from campfire.services.messages import apply_edit, may_delete, may_edit, remove_message, visible_message


class CampfireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize_database()

    def test_password_round_trip(self):
        encoded = security.password_hash("correct horse battery staple")
        self.assertTrue(encoded.startswith("pbkdf2_sha256$600000$"))
        self.assertTrue(security.password_matches("correct horse battery staple", encoded))
        self.assertFalse(security.password_matches("wrong password", encoded))

    def test_schema_and_relations(self):
        with database.connect() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "sessions", "communities", "memberships", "channels", "messages", "invitations", "attachments"} <= names)
        with database.connect() as db:
            message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        self.assertIn("attachment_id", message_columns)

    def test_image_signature_allowlist(self):
        self.assertEqual(uploads.detect_image_type(b"\x89PNG\r\n\x1a\nrest")[0], "image/png")
        self.assertEqual(uploads.detect_image_type(b"GIF89arest")[0], "image/gif")
        self.assertEqual(uploads.detect_image_type(b"\xff\xd8\xffrest")[0], "image/jpeg")
        self.assertEqual(uploads.detect_image_type(b"RIFFxxxxWEBPrest")[0], "image/webp")
        self.assertIsNone(uploads.detect_image_type(b"<svg><script>alert(1)</script></svg>"))

    def test_upload_filenames_cannot_choose_storage_paths(self):
        self.assertEqual(uploads.safe_original_name("..%2F..%2Fprivate.png"), "private.png")
        self.assertEqual(uploads.safe_original_name("meme%0AInjected.gif"), "memeInjected.gif")

    def test_invites_are_hashed_and_expire(self):
        token = "private-invite-code"
        with database.connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 ("invite_owner", security.password_hash("a sufficiently long password"), database.utc_now())).lastrowid
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      ("Friends", user_id, database.utc_now())).lastrowid
            db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,created_at)
              VALUES(?,?,?,?,?,?)""",
              (community_id, user_id, security.invite_hash(token), int(time.time()) + 60, 1, database.utc_now()))
            invite = security.valid_invite(db, token)
            stored = db.execute("SELECT token_hash FROM invitations").fetchone()[0]
        self.assertEqual(invite["community_id"], community_id)
        self.assertNotEqual(stored, token)

    def test_owner_can_list_and_immediately_revoke_active_invites(self):
        with database.connect() as db:
            owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                  ("invite_manager", "unused", database.utc_now())).lastrowid
            member_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                   ("invite_member", "unused", database.utc_now())).lastrowid
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      ("Invite Management", owner_id, database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, member_id))
            active_id = db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,uses,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (community_id, owner_id, security.invite_hash("active"), 2000, 3, 1, database.utc_now())).lastrowid
            db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,uses,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (community_id, owner_id, security.invite_hash("expired"), 999, 3, 0, database.utc_now()))
            db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,uses,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (community_id, owner_id, security.invite_hash("exhausted"), 2000, 1, 1, database.utc_now()))
            visible = list_active_invites(db, community_id, owner_id, timestamp=1000)
            forbidden = list_active_invites(db, community_id, member_id, timestamp=1000)
            member_revoked = revoke_invite(db, active_id, member_id)
            owner_revoked = revoke_invite(db, active_id, owner_id)
            remaining = db.execute("SELECT 1 FROM invitations WHERE id=?", (active_id,)).fetchone()
        self.assertEqual([invite["id"] for invite in visible], [active_id])
        self.assertIsNone(forbidden)
        self.assertFalse(member_revoked)
        self.assertTrue(owner_revoked)
        self.assertIsNone(remaining)

    def test_member_list_marks_owner_and_hides_from_outsiders(self):
        with database.connect() as db:
            owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                  ("member_owner", "unused", database.utc_now())).lastrowid
            member_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                   ("regular_member", "unused", database.utc_now())).lastrowid
            outsider_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                     ("member_outsider", "unused", database.utc_now())).lastrowid
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      ("Member Test", owner_id, database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, member_id))
            visible = list_community_members(db, community_id, member_id)
            hidden = list_community_members(db, community_id, outsider_id)
        self.assertEqual([member["role"] for member in visible], ["owner", "member"])
        self.assertEqual(visible[0]["id"], owner_id)
        self.assertIsNone(hidden)

    def message_fixture(self, db, label, attachment=False):
        """Build an owner, an author, a bystander, an outsider, and one message."""
        actors = {}
        for role in ("owner", "author", "bystander", "outsider"):
            actors[role] = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                      (f"{label}_{role}", "unused", database.utc_now())).lastrowid
        community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                  (label, actors["owner"], database.utc_now())).lastrowid
        for role in ("owner", "author", "bystander"):
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, actors[role]))
        channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                (community_id, "general", database.utc_now())).lastrowid
        attachment_id = None
        if attachment:
            attachment_id = db.execute("""INSERT INTO attachments
              (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (channel_id, actors["author"], f"{label}_stored.png", "photo.png", "image/png", 12,
               database.utc_now())).lastrowid
        message_id = db.execute("""INSERT INTO messages
          (channel_id,author_id,body,created_at,attachment_id) VALUES(?,?,?,?,?)""",
          (channel_id, actors["author"], "original text", database.utc_now(), attachment_id)).lastrowid
        return actors, message_id

    def test_only_the_author_may_edit_a_message(self):
        with database.connect() as db:
            actors, message_id = self.message_fixture(db, "edit_rules")
            self.assertTrue(may_edit(visible_message(db, message_id, actors["author"])))
            self.assertFalse(may_edit(visible_message(db, message_id, actors["owner"])))
            self.assertFalse(may_edit(visible_message(db, message_id, actors["bystander"])))
            apply_edit(db, message_id, "corrected text", "2026-01-01T00:00:00Z")
            edited = visible_message(db, message_id, actors["author"])
        self.assertEqual(edited["body"], "corrected text")
        self.assertEqual(edited["edited_at"], "2026-01-01T00:00:00Z")

    def test_author_and_community_owner_may_delete_but_others_may_not(self):
        with database.connect() as db:
            actors, message_id = self.message_fixture(db, "delete_rules")
            self.assertTrue(may_delete(visible_message(db, message_id, actors["author"])))
            self.assertTrue(may_delete(visible_message(db, message_id, actors["owner"])))
            self.assertFalse(may_delete(visible_message(db, message_id, actors["bystander"])))

    def test_messages_are_invisible_outside_their_community(self):
        with database.connect() as db:
            actors, message_id = self.message_fixture(db, "visibility")
            self.assertIsNone(visible_message(db, message_id, actors["outsider"]))

    def test_deleting_a_message_removes_its_attachment_row(self):
        with database.connect() as db:
            actors, message_id = self.message_fixture(db, "attachment_delete", attachment=True)
            message = visible_message(db, message_id, actors["owner"])
            orphaned = remove_message(db, message)
            remaining_message = db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone()
            remaining_attachment = db.execute("SELECT 1 FROM attachments WHERE storage_name=?",
                                              ("attachment_delete_stored.png",)).fetchone()
        self.assertEqual(orphaned, "attachment_delete_stored.png")
        self.assertIsNone(remaining_message)
        self.assertIsNone(remaining_attachment)

    def test_deleting_a_text_message_orphans_no_file(self):
        with database.connect() as db:
            actors, message_id = self.message_fixture(db, "text_delete")
            self.assertIsNone(remove_message(db, visible_message(db, message_id, actors["author"])))

    def test_presence_tracks_the_first_and_last_stream_per_user(self):
        """One person often has several tabs open; only the edges change presence."""
        broker = realtime.Broker()
        first, became_online = broker.subscribe(7)
        self.assertTrue(became_online)
        second, became_online = broker.subscribe(7)
        self.assertFalse(became_online)
        self.assertEqual(broker.online_user_ids(), frozenset({7}))

        user_id, went_offline = broker.unsubscribe(first)
        self.assertEqual(user_id, 7)
        self.assertFalse(went_offline)
        self.assertEqual(broker.online_user_ids(), frozenset({7}))

        _, went_offline = broker.unsubscribe(second)
        self.assertTrue(went_offline)
        self.assertEqual(broker.online_user_ids(), frozenset())
        self.assertEqual(broker.unsubscribe(second), (None, False))

    def test_stream_limits_are_enforced_and_released(self):
        broker = realtime.Broker()
        first, _ = broker.subscribe(1, max_total=3, max_per_user=2)
        second, _ = broker.subscribe(1, max_total=3, max_per_user=2)
        refused, _ = broker.subscribe(1, max_total=3, max_per_user=2)
        self.assertIsNone(refused, "one account must not open unlimited streams")

        other, _ = broker.subscribe(2, max_total=3, max_per_user=2)
        self.assertIsNotNone(other, "a different account still has room")
        self.assertIsNone(broker.subscribe(3, max_total=3, max_per_user=2)[0],
                          "the host-wide limit must hold")

        broker.unsubscribe(first)
        self.assertIsNotNone(broker.subscribe(3, max_total=3, max_per_user=2)[0],
                             "closing a stream must free its slot")
        self.assertEqual(broker.stream_counts()[0], 3)

    def test_a_refused_stream_does_not_mark_anyone_online(self):
        broker = realtime.Broker()
        broker.subscribe(1, max_total=1)
        refused, became_online = broker.subscribe(2, max_total=1)
        self.assertIsNone(refused)
        self.assertFalse(became_online)
        self.assertEqual(broker.online_user_ids(), frozenset({1}))

    def test_a_stream_that_falls_behind_is_flagged_rather_than_losing_events(self):
        broker = realtime.Broker()
        subscription, _ = broker.subscribe(1)
        for index in range(60):
            broker.publish({"type": "message.created", "channel_id": 1, "id": index})
        self.assertTrue(subscription.missed.is_set(),
                        "an overflowing stream must be told to re-read, not left silently wrong")
        self.assertEqual(subscription.events.qsize(), 50)

    def test_presence_is_only_visible_across_a_shared_community(self):
        with database.connect() as db:
            actors = {}
            for role in ("resident", "neighbour", "stranger"):
                actors[role] = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                          (f"presence_{role}", "unused", database.utc_now())).lastrowid
            shared = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                ("Shared", actors["resident"], database.utc_now())).lastrowid
            elsewhere = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                   ("Elsewhere", actors["stranger"], database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (shared, actors["resident"]))
            db.execute("INSERT INTO memberships VALUES(?,?)", (shared, actors["neighbour"]))
            db.execute("INSERT INTO memberships VALUES(?,?)", (elsewhere, actors["stranger"]))

            self.assertTrue(shares_community(db, actors["resident"], actors["neighbour"]))
            self.assertTrue(shares_community(db, actors["resident"], actors["resident"]))
            self.assertFalse(shares_community(db, actors["resident"], actors["stranger"]))

    def test_member_list_reports_who_is_connected(self):
        with database.connect() as db:
            owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                  ("presence_owner", "unused", database.utc_now())).lastrowid
            absent_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                   ("presence_absent", "unused", database.utc_now())).lastrowid
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      ("Presence", owner_id, database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships VALUES(?,?)", (community_id, absent_id))
            members = list_community_members(db, community_id, owner_id, frozenset({owner_id}))
            without_presence = list_community_members(db, community_id, owner_id)
        self.assertEqual({member["username"]: member["online"] for member in members},
                         {"presence_owner": True, "presence_absent": False})
        self.assertTrue(all(member["online"] is False for member in without_presence))

    def test_database_files_are_private_and_journalled(self):
        with database.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(stat.S_IMODE(database.DB_PATH.stat().st_mode), 0o600,
                         "the database holds password hashes and messages")

    def test_forwarded_addresses_are_ignored_without_a_trusted_proxy(self):
        """An unconfigured deployment must never believe a client-supplied header."""
        self.assertEqual(security.client_address("203.0.113.9", "1.2.3.4", ()), "203.0.113.9")
        self.assertEqual(security.client_address("203.0.113.9", "1.2.3.4", (ip_network("10.0.0.0/8"),)),
                         "203.0.113.9")

    def test_forwarded_addresses_are_read_only_from_a_trusted_proxy(self):
        trusted = (ip_network("10.0.0.0/8"), ip_network("127.0.0.1/32"))
        self.assertEqual(security.client_address("127.0.0.1", "198.51.100.7", trusted), "198.51.100.7")
        # A client may prepend anything; only the entry our own proxy appended counts.
        self.assertEqual(security.client_address("127.0.0.1", "evil, 198.51.100.7", trusted), "198.51.100.7")
        self.assertEqual(security.client_address("127.0.0.1", "198.51.100.7, 10.0.0.5", trusted), "198.51.100.7")
        self.assertEqual(security.client_address("127.0.0.1", "not-an-address", trusted), "127.0.0.1")
        self.assertEqual(security.client_address("127.0.0.1", None, trusted), "127.0.0.1")
        self.assertEqual(security.client_address("", "198.51.100.7", trusted), "unknown")

    def test_one_attacker_cannot_share_a_bucket_with_everyone_behind_a_proxy(self):
        trusted = (ip_network("127.0.0.1/32"),)
        limiter = security.RateLimiter(attempts=2, window=60)
        attacker = security.client_address("127.0.0.1", "198.51.100.7", trusted)
        bystander = security.client_address("127.0.0.1", "198.51.100.8", trusted)
        self.assertTrue(limiter.allow(attacker, timestamp=100))
        self.assertTrue(limiter.allow(attacker, timestamp=101))
        self.assertFalse(limiter.allow(attacker, timestamp=102))
        self.assertTrue(limiter.allow(bystander, timestamp=103), "a bystander must not be locked out")

    def test_rate_limiter_window(self):
        limiter = security.RateLimiter(attempts=2, window=10)
        self.assertTrue(limiter.allow("client", timestamp=100))
        self.assertTrue(limiter.allow("client", timestamp=101))
        self.assertFalse(limiter.allow("client", timestamp=102))
        self.assertTrue(limiter.allow("client", timestamp=111))

    def test_rate_limiter_forgets_expired_keys(self):
        """Keys embed attempted usernames, so expired ones must not accumulate."""
        limiter = security.RateLimiter(attempts=2, window=10)
        limiter.allow("login:first", timestamp=100)
        limiter.allow("login:second", timestamp=101)
        self.assertEqual(len(limiter._entries), 2)
        limiter.allow("login:later", timestamp=200)
        self.assertEqual(set(limiter._entries), {"login:later"})

    def test_startup_refuses_usernames_that_differ_only_by_case(self):
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        memory.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)")
        memory.execute("INSERT INTO users(username) VALUES('Sam'),('sam')")
        with self.assertRaises(RuntimeError) as failure:
            database.enforce_username_case_uniqueness(memory)
        self.assertIn("Sam", str(failure.exception))

    def test_username_rules(self):
        self.assertTrue(security.USERNAME_RE.fullmatch("friendly_user"))
        self.assertFalse(security.USERNAME_RE.fullmatch("x"))
        self.assertFalse(security.USERNAME_RE.fullmatch("not friendly"))


if __name__ == "__main__":
    unittest.main()
