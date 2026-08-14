import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import closing
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

temporary = tempfile.TemporaryDirectory()
os.environ["CAMPFIRE_DB"] = str(Path(temporary.name) / "test.db")

from campfire import database, realtime, security, uploads
from campfire.services.accounts import change_password, delete_account, deletion_plan
from campfire.services.accounts import export_account, list_sessions, revoke_session
from campfire.services.channels import channel_context, may_post, slow_mode_remaining
from campfire.services.channels import update_settings as update_channel_settings
from campfire.services.communities import community_role, has_role, is_banned, list_active_invites
from campfire.services.communities import list_community_bans, list_community_members, remove_member
from campfire.services.communities import revoke_invite, set_member_role, shares_community, unban_member
from campfire.services.messages import apply_edit, may_delete, may_edit, remove_message, visible_message
from campfire.services.retention import purge_expired, set_retention
from campfire.services.storage import begin_reserved_upload_write, capacity_warnings, exceeds_limit
from campfire.services.storage import filesystems_have_space, release_upload, reserve_upload
from campfire.services.storage import tracked_bytes
from campfire.services.storage import usage as storage_usage, writable_location
from campfire.services.notifications import account_mode, channel_states, mark_community_read, mark_read
from campfire.services.notifications import set_account_mode, set_channel_mode
from campfire.services.passkeys import PasskeyError, authenticate_passkey, authentication_options
from campfire.services.passkeys import list_passkeys, register_passkey, registration_options
from campfire.services.passkeys import remove_passkey


def png_chunk(kind, payload):
    return (len(payload).to_bytes(4, "big") + kind + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big"))


def png_chunks(content):
    offset = len(uploads.PNG_SIGNATURE)
    while offset + 12 <= len(content):
        length = int.from_bytes(content[offset:offset + 4], "big")
        yield content[offset + 4:offset + 8], content[offset + 8:offset + 8 + length]
        offset += 12 + length


def png_chunk_types(content):
    return [kind for kind, _ in png_chunks(content)]


def png_chunk_payload(content, wanted):
    return b"".join(payload for kind, payload in png_chunks(content) if kind == wanted)


def jpeg_segment(marker, payload):
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def gif_blocks(payload):
    return bytes([len(payload)]) + payload + b"\x00"


def riff_chunk(fourcc, payload):
    padded = payload + (b"\x00" if len(payload) % 2 else b"")
    return fourcc + len(payload).to_bytes(4, "little") + padded


class CampfireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize_database()

    def test_password_round_trip(self):
        encoded = security.password_hash("correct horse battery staple")
        self.assertTrue(encoded.startswith("pbkdf2_sha256$600000$"))
        self.assertTrue(security.password_matches("correct horse battery staple", encoded))
        self.assertFalse(security.password_matches("wrong password", encoded))

    def test_passkey_ceremonies_are_verified_one_time_and_inventory_is_private_metadata(self):
        username = f"passkey_service_{time.time_ns()}"
        with database.connect() as db:
            user_id = db.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (username, security.password_hash("passkey test password"),
                 database.utc_now())).lastrowid
            ceremony, options = registration_options(
                db, user_id, "passkey test password", "example.test")
            self.assertEqual(options["rp"]["id"], "example.test")
            verified = SimpleNamespace(
                credential_id=b"credential-id", credential_public_key=b"public-key", sign_count=3)
            with patch("campfire.services.passkeys.verify_registration_response",
                       return_value=verified):
                stored = register_passkey(
                    db, user_id, ceremony, {"id": "ignored"}, "Laptop",
                    "example.test", "https://example.test")
            self.assertEqual(stored["name"], "Laptop")
            self.assertEqual([entry["name"] for entry in list_passkeys(db, user_id)], ["Laptop"])
            with self.assertRaises(PasskeyError):
                register_passkey(db, user_id, ceremony, {"id": "ignored"}, "Again",
                                 "example.test", "https://example.test")

            login_ceremony, login_options = authentication_options(
                db, username, "example.test")
            self.assertEqual(len(login_options["allowCredentials"]), 1)
            authenticated = SimpleNamespace(new_sign_count=4)
            with patch("campfire.services.passkeys.base64url_to_bytes",
                       return_value=b"credential-id"), patch(
                           "campfire.services.passkeys.verify_authentication_response",
                           return_value=authenticated):
                user = authenticate_passkey(
                    db, login_ceremony, {"id": "credential"},
                    "example.test", "https://example.test")
            self.assertEqual(user["id"], user_id)
            self.assertTrue(remove_passkey(db, user_id, stored["id"], "passkey test password"))

    def test_unknown_passkey_account_gets_indistinguishable_fake_options(self):
        with database.connect() as db:
            ceremony, options = authentication_options(
                db, f"missing_{time.time_ns()}", "example.test")
            row = db.execute("SELECT user_id FROM webauthn_challenges WHERE token_hash=?",
                             (security.session_hash(ceremony),)).fetchone()
        self.assertIsNone(row["user_id"])
        self.assertEqual(len(options["allowCredentials"]), 1)

    def test_schema_and_relations(self):
        with database.connect() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "sessions", "communities", "memberships", "channels", "messages",
                         "invitations", "attachments", "channel_reads", "notification_preferences",
                         "channel_notifications", "community_bans", "schema_migrations",
                         "upload_reservations", "passkeys", "webauthn_challenges"} <= names)
        with database.connect() as db:
            message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            membership_columns = {row[1] for row in db.execute("PRAGMA table_info(memberships)")}
            session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        self.assertIn("attachment_id", message_columns)
        self.assertIn("role", membership_columns)
        self.assertIn("created_at", session_columns)
        self.assertIn("id", session_columns)
        with database.connect() as db:
            self.assertEqual(database.schema_version(db), 3)

    def test_existing_memberships_are_migrated_to_member_role(self):
        original_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as folder:
            legacy_path = Path(folder) / "legacy.db"
            legacy = sqlite3.connect(legacy_path)
            legacy.executescript("""
              CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE communities (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                owner_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
              CREATE TABLE memberships (community_id INTEGER NOT NULL REFERENCES communities(id),
                user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY (community_id,user_id));
              CREATE TABLE sessions (token TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),
                expires_at INTEGER NOT NULL);
              INSERT INTO users VALUES(1,'legacy_owner','unused','2026-01-01T00:00:00Z');
              INSERT INTO communities VALUES(1,'Legacy',1,'2026-01-01T00:00:00Z');
              INSERT INTO memberships VALUES(1,1);
              INSERT INTO sessions VALUES('legacy-token',1,2000000000);
            """)
            legacy.commit()
            legacy.close()
            try:
                database.DB_PATH = legacy_path
                database.initialize_database()
                with database.connect() as migrated:
                    role = migrated.execute("SELECT role FROM memberships WHERE user_id=1").fetchone()[0]
                    session = migrated.execute(
                        "SELECT id,token,created_at FROM sessions WHERE user_id=1").fetchone()
                    session_columns = {row[1] for row in
                                       migrated.execute("PRAGMA table_info(sessions)")}
            finally:
                database.DB_PATH = original_path
        self.assertEqual(role, "member")
        self.assertIsNotNone(session["created_at"])
        self.assertIn("id", session_columns, "an older sessions table gains a stable id")
        self.assertEqual(session["token"], "legacy-token",
                         "rebuilding the table must not sign everyone out")

    def test_password_change_and_session_revocation_keep_only_the_current_session(self):
        with database.connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 ("session_user", security.password_hash("old password long enough"),
                                  database.utc_now())).lastrowid
            current = security.session_hash("current-session")
            remote = security.session_hash("remote-session")
            other_user = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                    ("session_other", "unused", database.utc_now())).lastrowid
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (current, user_id, 2000, "2026-01-01T00:00:00Z"))
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (remote, user_id, 2000, "2026-01-02T00:00:00Z"))
            other_id = db.execute(
                "INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (security.session_hash("someone-elses-session"), other_user, 2000,
                 "2026-01-03T00:00:00Z")).lastrowid

            sessions = list_sessions(db, user_id, current, timestamp=1000)
            self.assertEqual(len(sessions), 2)
            self.assertTrue(sessions[0]["current"])
            self.assertIsNone(revoke_session(db, user_id, other_id, current, timestamp=1000))
            rotated = security.session_hash("rotated-session")
            self.assertIsNone(change_password(db, user_id, "wrong password",
                                              "new password long enough", current, rotated))
            revoked = change_password(db, user_id, "old password long enough",
                                      "new password long enough", current, rotated)
            self.assertEqual(revoked, 1)
            self.assertTrue(security.password_matches(
                "new password long enough",
                db.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()[0]))
            self.assertEqual([entry["id"] for entry in
                              list_sessions(db, user_id, rotated, timestamp=1000)],
                             [sessions[0]["id"]],
                             "the surviving session keeps its identity, not its token")
            self.assertIsNone(db.execute("SELECT 1 FROM sessions WHERE token=?", (current,)).fetchone(),
                              "the token that confirmed the change must not survive it")

    def account_fixture(self, db, label, password="a sufficiently long password"):
        """Build an account that owns one community and has written in another."""
        actors = {}
        for role in ("subject", "administrator", "member", "host"):
            actors[role] = db.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (f"{label}_{role}", security.password_hash(password) if role == "subject" else "unused",
                 database.utc_now())).lastrowid
        owned = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                           (f"{label} owned", actors["subject"], database.utc_now())).lastrowid
        guest = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                           (f"{label} guest", actors["host"], database.utc_now())).lastrowid
        for community_id, user_id, role in ((owned, actors["subject"], "member"),
                                            (owned, actors["member"], "member"),
                                            (owned, actors["administrator"], "administrator"),
                                            (guest, actors["host"], "member"),
                                            (guest, actors["subject"], "member")):
            db.execute("INSERT INTO memberships(community_id,user_id,role) VALUES(?,?,?)",
                       (community_id, user_id, role))
        channels = {}
        for name, community_id in (("owned", owned), ("guest", guest)):
            channels[name] = db.execute(
                "INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                (community_id, "general", database.utc_now())).lastrowid
        attachment_id = db.execute("""INSERT INTO attachments
          (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
          VALUES(?,?,?,?,?,?,?)""",
          (channels["guest"], actors["subject"], f"{label}_stored.png", "photo.png",
           "image/png", 12, database.utc_now())).lastrowid
        db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                   (channels["owned"], actors["subject"], "mine", database.utc_now()))
        db.execute("""INSERT INTO messages(channel_id,author_id,body,created_at,attachment_id)
                      VALUES(?,?,?,?,?)""",
                   (channels["guest"], actors["subject"], "shared", database.utc_now(), attachment_id))
        db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                   (channels["guest"], actors["host"], "theirs", database.utc_now()))
        return actors | {"owned": owned, "guest": guest} | {f"{k}_channel": v for k, v in channels.items()}

    def test_export_gathers_only_the_account_it_belongs_to(self):
        with database.connect() as db:
            actors = self.account_fixture(db, "export")
            export = export_account(db, actors["subject"], exported_at="2026-08-09T00:00:00Z")
        self.assertEqual(export["format"], "campfire.account-export.v1")
        self.assertEqual(export["account"]["username"], "export_subject")
        self.assertNotIn("password_hash", export["account"])
        self.assertEqual({entry["body"] for entry in export["messages"]}, {"mine", "shared"},
                         "an export must not carry other people's messages")
        self.assertEqual([entry["role"] for entry in export["communities"]
                          if entry["name"] == "export owned"], ["owner"])
        self.assertEqual([entry["attachment_name"] for entry in export["messages"]
                          if entry["body"] == "shared"], ["photo.png"])
        self.assertEqual(export["notification_preferences"]["default_mode"], "all")

    def test_deletion_hands_an_owned_community_to_its_senior_member(self):
        with database.connect() as db:
            actors = self.account_fixture(db, "succession")
            plan = deletion_plan(db, actors["subject"])
            self.assertEqual(plan["communities_dissolved"], [])
            self.assertEqual([entry["successor_username"] for entry in plan["communities_transferred"]],
                             ["succession_administrator"],
                             "the most privileged remaining member inherits, not the first row")
            self.assertEqual(plan["messages"], 2)
            self.assertEqual(plan["attachments"], 1)

            status, outcome = delete_account(db, actors["subject"], "a sufficiently long password")
            self.assertEqual(status, "ok")
            self.assertEqual(outcome["orphaned_files"], ["succession_stored.png"])
            self.assertEqual(db.execute("SELECT owner_id FROM communities WHERE id=?",
                                        (actors["owned"],)).fetchone()[0], actors["administrator"])
            self.assertIsNone(db.execute("SELECT 1 FROM users WHERE id=?",
                                         (actors["subject"],)).fetchone())
            self.assertEqual([row[0] for row in db.execute(
                "SELECT body FROM messages WHERE channel_id IN (?,?) ORDER BY id",
                (actors["owned_channel"], actors["guest_channel"]))],
                ["theirs"], "the account's messages go with it, others' stay")
            self.assertIsNone(db.execute("SELECT 1 FROM attachments WHERE storage_name=?",
                                         ("succession_stored.png",)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sessions WHERE user_id=?",
                                         (actors["subject"],)).fetchone())

    def test_deletion_dissolves_a_community_nobody_else_belongs_to(self):
        with database.connect() as db:
            actors = self.account_fixture(db, "dissolve")
            db.execute("DELETE FROM memberships WHERE community_id=? AND user_id<>?",
                       (actors["owned"], actors["subject"]))
            plan = deletion_plan(db, actors["subject"])
            self.assertEqual([entry["name"] for entry in plan["communities_dissolved"]],
                             ["dissolve owned"])
            self.assertEqual(plan["communities_transferred"], [])

            self.assertEqual(delete_account(db, actors["subject"], "a sufficiently long password")[0], "ok")
            self.assertIsNone(db.execute("SELECT 1 FROM communities WHERE id=?",
                                         (actors["owned"],)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM channels WHERE id=?",
                                         (actors["owned_channel"],)).fetchone())
            self.assertTrue(db.execute("SELECT 1 FROM communities WHERE id=?",
                                       (actors["guest"],)).fetchone(),
                            "a community the account merely joined must survive")

    def test_deletion_erases_messages_left_in_a_community_that_removed_the_account(self):
        """Being kicked leaves your words behind; deleting your account does not."""
        with database.connect() as db:
            actors = self.account_fixture(db, "kicked_then_deleted")
            db.execute("DELETE FROM memberships WHERE community_id=? AND user_id=?",
                       (actors["guest"], actors["subject"]))
            self.assertEqual(delete_account(db, actors["subject"], "a sufficiently long password")[0], "ok")
            self.assertEqual([row[0] for row in db.execute(
                "SELECT body FROM messages WHERE channel_id=?", (actors["guest_channel"],))], ["theirs"])

    def test_deletion_refuses_the_wrong_password_and_changes_nothing(self):
        with database.connect() as db:
            actors = self.account_fixture(db, "wrong_password")
            status, outcome = delete_account(db, actors["subject"], "not the right password")
            self.assertEqual(status, "invalid_password")
            self.assertIsNone(outcome)
            self.assertTrue(db.execute("SELECT 1 FROM users WHERE id=?", (actors["subject"],)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE author_id=?",
                                        (actors["subject"],)).fetchone()[0], 2)

    def test_deletion_keeps_bans_the_account_issued_without_naming_it(self):
        with database.connect() as db:
            actors = self.account_fixture(db, "issued_bans")
            db.execute("""INSERT INTO community_bans
              (community_id,user_id,banned_by,role_at_ban,created_at) VALUES(?,?,?,?,?)""",
              (actors["owned"], actors["member"], actors["subject"], "member", database.utc_now()))
            db.execute("DELETE FROM memberships WHERE community_id=? AND user_id=?",
                       (actors["owned"], actors["member"]))
            self.assertEqual(delete_account(db, actors["subject"], "a sufficiently long password")[0], "ok")
            ban = db.execute("SELECT user_id,banned_by FROM community_bans WHERE community_id=?",
                             (actors["owned"],)).fetchone()
        self.assertEqual(ban["user_id"], actors["member"], "the ban must outlive the moderator's account")
        self.assertIsNone(ban["banned_by"])

    def test_a_revoked_session_id_is_never_given_to_a_later_session(self):
        """A stale list must not be able to revoke a session it never showed."""
        with database.connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 ("recycled_ids", "unused", database.utc_now())).lastrowid
            first = security.session_hash("first-session")
            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (first, user_id, 2000, "2026-01-01T00:00:00Z"))
            highest = list_sessions(db, user_id, first, timestamp=1000)[0]["id"]
            self.assertIs(revoke_session(db, user_id, highest, first, timestamp=1000), True)

            db.execute("INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (security.session_hash("later-session"), user_id, 2000,
                        "2026-01-02T00:00:00Z"))
            replacement = list_sessions(db, user_id, "none", timestamp=1000)[0]
        self.assertNotEqual(replacement["id"], highest,
                            "reusing the id would point a stale Sign out at the wrong session")

    def test_removing_a_member_revokes_the_invites_they_created(self):
        """A ban that leaves working invite codes behind has not closed the door."""
        with database.connect() as db:
            owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                  ("revoke_owner", "unused", database.utc_now())).lastrowid
            admin_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                  ("revoke_admin", "unused", database.utc_now())).lastrowid
            communities = {}
            for name in ("here", "elsewhere"):
                communities[name] = db.execute(
                    "INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                    (f"revoke {name}", owner_id, database.utc_now())).lastrowid
                for user_id, role in ((owner_id, "member"), (admin_id, "administrator")):
                    db.execute("INSERT INTO memberships(community_id,user_id,role) VALUES(?,?,?)",
                               (communities[name], user_id, role))
            invites = {}
            for label, community_id, creator in (("theirs", communities["here"], admin_id),
                                                 ("owners", communities["here"], owner_id),
                                                 ("far", communities["elsewhere"], admin_id)):
                invites[label] = db.execute("""INSERT INTO invitations
                  (community_id,created_by,token_hash,expires_at,max_uses,created_at)
                  VALUES(?,?,?,?,?,?)""",
                  (community_id, creator, f"digest_{label}", 2000000000, 5,
                   database.utc_now())).lastrowid

            status, _ = remove_member(db, communities["here"], admin_id, owner_id, ban=True)
            self.assertEqual(status, "ok")
            surviving = {row[0] for row in db.execute("SELECT id FROM invitations")}
        self.assertNotIn(invites["theirs"], surviving,
                         "a removed member's codes must stop admitting people")
        self.assertIn(invites["owners"], surviving,
                      "only the removed member's own invites are revoked")
        self.assertIn(invites["far"], surviving,
                      "a community they still belong to is not this one's business")

    def channel_fixture(self, db, label):
        """An owner, an administrator, a moderator and a member around one channel."""
        actors = {}
        for role in ("owner", "administrator", "moderator", "member"):
            actors[role] = db.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (f"{label}_{role}", "unused", database.utc_now())).lastrowid
        community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                  (label, actors["owner"], database.utc_now())).lastrowid
        for role, user_id in actors.items():
            db.execute("INSERT INTO memberships(community_id,user_id,role) VALUES(?,?,?)",
                       (community_id, user_id, role if role != "owner" else "member"))
        channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                (community_id, "general", database.utc_now())).lastrowid
        return actors, community_id, channel_id

    def test_a_channel_can_restrict_posting_to_a_role(self):
        with database.connect() as db:
            actors, _, channel_id = self.channel_fixture(db, "posting_rules")
            self.assertIsNone(update_channel_settings(db, channel_id, actors["moderator"],
                                                      "moderator", 0, True),
                              "a moderator does not administer channels")
            self.assertIs(update_channel_settings(db, channel_id, actors["owner"],
                                                  "emperor", 0, True), False)
            self.assertIs(update_channel_settings(db, channel_id, actors["owner"],
                                                  "member", 9999, True), False)
            updated = update_channel_settings(db, channel_id, actors["administrator"],
                                              "moderator", 0, False)
            self.assertEqual(updated["post_min_role"], "moderator")

            for role, expected in (("member", "role"), ("moderator", "ok"),
                                   ("administrator", "ok"), ("owner", "ok")):
                context = channel_context(db, channel_id, actors[role])
                self.assertEqual(may_post(db, context, actors[role])[0], expected, role)
            # Reading is untouched by a posting rule.
            self.assertIsNotNone(channel_context(db, channel_id, actors["member"]))
            self.assertEqual(may_post(db, channel_context(db, channel_id, actors["owner"]),
                                      actors["owner"], uploading=True)[0], "uploads_disabled")

    def test_slow_mode_counts_from_the_last_message_and_spares_moderators(self):
        with database.connect() as db:
            actors, _, channel_id = self.channel_fixture(db, "slow_mode")
            update_channel_settings(db, channel_id, actors["owner"], "member", 30, True)
            for role in ("member", "moderator"):
                db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                           (channel_id, actors[role], "first", "2026-08-09T12:00:00Z"))

            member = channel_context(db, channel_id, actors["member"])
            self.assertEqual(slow_mode_remaining(db, member, actors["member"],
                                                 now="2026-08-09T12:00:10Z"), 20)
            self.assertEqual(may_post(db, member, actors["member"], now="2026-08-09T12:00:10Z"),
                             ("slow_mode", 20))
            self.assertEqual(may_post(db, member, actors["member"], now="2026-08-09T12:00:30Z"),
                             ("ok", None))

            moderator = channel_context(db, channel_id, actors["moderator"])
            self.assertEqual(may_post(db, moderator, actors["moderator"], now="2026-08-09T12:00:10Z"),
                             ("ok", None), "moderators answer floods, so slow mode spares them")

    def retention_fixture(self, db, label):
        """One community, one channel, and messages of assorted ages."""
        actors, community_id, channel_id = self.channel_fixture(db, label)
        ages = {}
        for name, stamp, attach in (("ancient", "2020-01-01T00:00:00Z", False),
                                    ("old_image", "2026-06-01T00:00:00Z", True),
                                    ("recent", "2026-08-08T00:00:00Z", False)):
            attachment_id = None
            if attach:
                attachment_id = db.execute("""INSERT INTO attachments
                  (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
                  VALUES(?,?,?,?,?,?,?)""",
                  (channel_id, actors["member"], f"{label}_{name}.png", "photo.png",
                   "image/png", 10, stamp)).lastrowid
            ages[name] = db.execute("""INSERT INTO messages
              (channel_id,author_id,body,created_at,attachment_id) VALUES(?,?,?,?,?)""",
              (channel_id, actors["member"], name, stamp, attachment_id)).lastrowid
        return actors, community_id, channel_id, ages

    def test_retention_removes_only_what_is_past_its_window(self):
        with database.connect() as db:
            actors, community_id, channel_id, ages = self.retention_fixture(db, "retention")
            self.assertIsNone(set_retention(db, community_id, actors["moderator"], 30, 30),
                              "a moderator does not set retention")
            self.assertIs(set_retention(db, community_id, actors["owner"], -1, 0), False)
            self.assertEqual(set_retention(db, community_id, actors["administrator"], 90, 30),
                             {"community_id": community_id, "message_days": 90,
                              "attachment_days": 30})

            removed, orphaned = purge_expired(db, now="2026-08-09T00:00:00Z")
            surviving = {row[0] for row in db.execute(
                "SELECT id FROM messages WHERE channel_id=?", (channel_id,))}
        self.assertEqual(removed, {(community_id, channel_id): 2})
        self.assertEqual(orphaned, ["retention_old_image.png"])
        self.assertEqual(surviving, {ages["recent"]},
                         "only history past a window it was given goes")

    def test_retention_defaults_to_keeping_everything(self):
        """A community that never chose a window is never quietly pruned."""
        with database.connect() as db:
            _, community_id, channel_id, ages = self.retention_fixture(db, "no_retention")
            removed, orphaned = purge_expired(db, now="2126-01-01T00:00:00Z")
            surviving = {row[0] for row in db.execute(
                "SELECT id FROM messages WHERE channel_id=?", (channel_id,))}
        self.assertEqual(removed, {})
        self.assertEqual(orphaned, [])
        self.assertEqual(surviving, set(ages.values()))

    def test_image_retention_can_be_shorter_than_message_retention(self):
        with database.connect() as db:
            actors, community_id, channel_id, ages = self.retention_fixture(db, "image_retention")
            set_retention(db, community_id, actors["owner"], 0, 30)
            removed, orphaned = purge_expired(db, now="2026-08-09T00:00:00Z")
            surviving = {row[0] for row in db.execute(
                "SELECT id FROM messages WHERE channel_id=?", (channel_id,))}
            attachments = db.execute("SELECT COUNT(*) FROM attachments WHERE channel_id=?",
                                     (channel_id,)).fetchone()[0]
        self.assertEqual(removed, {(community_id, channel_id): 1})
        self.assertEqual(orphaned, ["image_retention_old_image.png"])
        self.assertEqual(surviving, {ages["ancient"], ages["recent"]},
                         "words outlive the images when only images have a window")
        self.assertEqual(attachments, 0)

    def test_storage_usage_is_administrator_visible_and_broken_down(self):
        with database.connect() as db:
            actors, community_id, channel_id = self.channel_fixture(db, "storage")
            for index, size in enumerate((1000, 2500)):
                db.execute("""INSERT INTO attachments
                  (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
                  VALUES(?,?,?,?,?,?,?)""",
                  (channel_id, actors["member"], f"storage_{index}.png", "photo.png",
                   "image/png", size, database.utc_now()))
            total, files = tracked_bytes(db)
            self.assertGreaterEqual(total, 3500)

            self.assertIsNone(storage_usage(db, actors["member"], 0),
                              "disk usage is an operator's concern, not every member's")
            self.assertIsNone(storage_usage(db, actors["moderator"], 0))
            limit = total + 10_000
            report = storage_usage(db, actors["administrator"], limit, stored_bytes=4096)
            mine = [entry for entry in report["communities"] if entry["id"] == community_id]
        self.assertEqual(report["limit_bytes"], limit)
        self.assertEqual(report["available_bytes"], 10_000)
        self.assertEqual(report["stored_bytes"], 4096)
        self.assertEqual(report["files"], files)
        self.assertEqual([(entry["bytes"], entry["files"]) for entry in mine], [(3500, 2)])

    def test_an_upload_past_the_ceiling_is_refused_and_no_limit_means_no_ceiling(self):
        with database.connect() as db:
            actors, _, channel_id = self.channel_fixture(db, "quota")
            db.execute("""INSERT INTO attachments
              (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (channel_id, actors["member"], "quota_0.png", "photo.png", "image/png",
               1000, database.utc_now()))
            used = tracked_bytes(db)[0]
            self.assertFalse(exceeds_limit(db, 0, 10 ** 12),
                             "a limit of zero is no limit, which is the default")
            self.assertFalse(exceeds_limit(db, used + 100, 100))
            self.assertTrue(exceeds_limit(db, used + 100, 101))

    def test_concurrent_uploads_reserve_capacity_before_receiving_bytes(self):
        with database.connect() as db:
            used = tracked_bytes(db)[0]
        barrier = threading.Barrier(2)
        results = []

        def reserve(token):
            with database.connect() as connection:
                barrier.wait()
                results.append((token, reserve_upload(
                    connection, token, used + 100, 60, timestamp=100)))

        threads = [threading.Thread(target=reserve, args=(token,))
                   for token in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(accepted for _, accepted in results), 1)
        winner = next(token for token, accepted in results if accepted)
        with database.connect() as db:
            self.assertTrue(begin_reserved_upload_write(
                db, winner, used + 100, 50, timestamp=101))
        with database.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM upload_reservations WHERE token=?", (winner,)).fetchone())

    def test_capacity_warnings_cover_the_image_limit_and_shared_filesystems(self):
        with tempfile.TemporaryDirectory() as folder, database.connect() as db:
            disk = SimpleNamespace(total=100, used=95, free=5)
            with patch("campfire.services.storage.tracked_bytes", return_value=(95, 1)), \
                 patch("campfire.services.storage.shutil.disk_usage", return_value=disk):
                warnings = capacity_warnings(
                    db, 100, Path(folder) / "uploads", Path(folder) / "campfire.db", 90)
        self.assertEqual([warning["code"] for warning in warnings],
                         ["image_storage_limit", "filesystem_capacity"])
        self.assertEqual(warnings[-1]["percent"], 95)

    def test_readiness_location_check_accepts_a_writable_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(writable_location(Path(folder) / "new" / "uploads", directory=True))
            locked = Path(folder) / "locked"
            locked.mkdir(mode=0o500)
            self.assertFalse(writable_location(locked / "uploads", directory=True))

    def test_readiness_capacity_check_rejects_a_full_filesystem(self):
        with tempfile.TemporaryDirectory() as folder:
            full = SimpleNamespace(total=100, used=100, free=0)
            with patch("campfire.services.storage.shutil.disk_usage", return_value=full):
                self.assertFalse(filesystems_have_space(Path(folder) / "campfire.db",
                                                        Path(folder) / "uploads"))

    def test_image_signature_allowlist(self):
        self.assertEqual(uploads.detect_image_type(b"\x89PNG\r\n\x1a\nrest")[0], "image/png")
        self.assertEqual(uploads.detect_image_type(b"GIF89arest")[0], "image/gif")
        self.assertEqual(uploads.detect_image_type(b"\xff\xd8\xffrest")[0], "image/jpeg")
        self.assertEqual(uploads.detect_image_type(b"RIFFxxxxWEBPrest")[0], "image/webp")
        self.assertIsNone(uploads.detect_image_type(b"<svg><script>alert(1)</script></svg>"))

    def test_png_metadata_is_removed_and_pixels_survive(self):
        pixels = zlib.compress(b"\x00\xff\x00\x00")
        original = (uploads.PNG_SIGNATURE
                    + png_chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
                    + png_chunk(b"tEXt", b"Comment\x00taken at home")
                    + png_chunk(b"eXIf", b"GPS 51.5074 N 0.1278 W")
                    + png_chunk(b"tIME", b"\x07\xe6\x08\x02\x10\x00\x00")
                    + png_chunk(b"IDAT", pixels)
                    + png_chunk(b"IEND", b""))
        stripped = uploads.strip_png(original)
        self.assertIsNotNone(stripped)
        self.assertNotIn(b"GPS", stripped)
        self.assertNotIn(b"taken at home", stripped)
        self.assertEqual(png_chunk_types(stripped), [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(zlib.decompress(png_chunk_payload(stripped, b"IDAT")), b"\x00\xff\x00\x00")
        self.assertEqual(uploads.detect_image_type(stripped)[0], "image/png")

    def test_png_data_appended_after_the_end_is_discarded(self):
        original = (uploads.PNG_SIGNATURE
                    + png_chunk(b"IHDR", b"\x00" * 13)
                    + png_chunk(b"IEND", b"")
                    + b"a secret payload hidden past the end")
        self.assertNotIn(b"secret", uploads.strip_png(original))

    def test_jpeg_application_and_comment_segments_are_removed(self):
        original = (b"\xff\xd8"
                    + jpeg_segment(0xE1, b"Exif\x00\x00GPS 51.5074 N")
                    + jpeg_segment(0xE0, b"JFIF\x00\x01\x02\x00")
                    + jpeg_segment(0xFE, b"shot on a phone")
                    + jpeg_segment(0xEE, b"Adobe\x00d\x00\x00\x00\x00")
                    + jpeg_segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
                    + jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
                    + b"\x12\x34\xff\xd9")
        stripped = uploads.strip_jpeg(original)
        self.assertIsNotNone(stripped)
        self.assertNotIn(b"Exif", stripped)
        self.assertNotIn(b"GPS", stripped)
        self.assertNotIn(b"shot on a phone", stripped)
        self.assertNotIn(b"JFIF", stripped)
        self.assertIn(b"Adobe", stripped, "the colour-transform marker is not about the photographer")
        self.assertIn(b"\x12\x34", stripped, "the scan data must survive")
        self.assertTrue(stripped.startswith(b"\xff\xd8\xff"))
        self.assertTrue(stripped.endswith(b"\xff\xd9"))

    def test_jpeg_trailing_data_after_the_end_marker_is_discarded(self):
        original = (b"\xff\xd8"
                    + jpeg_segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
                    + jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
                    + b"\x12\x34\xff\xd9" + b"appended thumbnail with its own exif")
        self.assertNotIn(b"exif", uploads.strip_jpeg(original))

    def test_gif_comment_and_foreign_extensions_are_removed(self):
        original = (b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00"
                    + b"\x21\xfe" + gif_blocks(b"taken at home")
                    + b"\x21\xff\x0bXMP DataXMP" + gif_blocks(b"<x:xmpmeta>GPS</x:xmpmeta>")
                    + b"\x21\xff\x0bNETSCAPE2.0" + gif_blocks(b"\x01\x00\x00")
                    + b"\x21\xf9" + gif_blocks(b"\x04\x00\x00\x00")
                    + b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00" + b"\x02" + gif_blocks(b"\x4c\x01\x00")
                    + b"\x3b")
        stripped = uploads.strip_gif(original)
        self.assertIsNotNone(stripped)
        self.assertNotIn(b"taken at home", stripped)
        self.assertNotIn(b"GPS", stripped)
        self.assertNotIn(b"XMP Data", stripped)
        self.assertIn(b"NETSCAPE2.0", stripped, "the loop count keeps animations looping")
        self.assertIn(b"\x21\xf9", stripped, "frame timing must survive")
        self.assertIn(b"\x4c\x01\x00", stripped, "the frame data must survive")
        self.assertTrue(stripped.endswith(b"\x3b"))
        self.assertEqual(uploads.detect_image_type(stripped)[0], "image/gif")

    def test_webp_metadata_chunks_and_their_flags_are_removed(self):
        original = (riff_chunk(b"VP8X", b"\x2e" + b"\x00" * 9)
                    + riff_chunk(b"VP8 ", b"frame data")
                    + riff_chunk(b"EXIF", b"GPS 51.5074 N")
                    + riff_chunk(b"XMP ", b"<x:xmpmeta/>"))
        original = b"RIFF" + (len(original) + 4).to_bytes(4, "little") + b"WEBP" + original
        stripped = uploads.strip_webp(original)
        self.assertIsNotNone(stripped)
        self.assertNotIn(b"GPS", stripped)
        self.assertNotIn(b"xmpmeta", stripped)
        self.assertIn(b"frame data", stripped)
        self.assertEqual(int.from_bytes(stripped[4:8], "little"), len(stripped) - 8,
                         "the RIFF length must match the rebuilt file")
        flags = stripped[stripped.index(b"VP8X") + 8]
        self.assertEqual(flags & uploads.VP8X_METADATA_FLAGS, 0,
                         "flags must not advertise metadata that is gone")
        self.assertEqual(flags & 0x02, 0x02, "the animation flag must be left alone")
        self.assertEqual(uploads.detect_image_type(stripped)[0], "image/webp")

    def test_unparsable_images_are_rejected_rather_than_stored_unstripped(self):
        truncated_png = uploads.PNG_SIGNATURE + png_chunk(b"IHDR", b"\x00" * 13)[:-4]
        self.assertIsNone(uploads.strip_png(truncated_png))
        self.assertIsNone(uploads.strip_png(b"not a png"))
        self.assertIsNone(uploads.strip_jpeg(b"\xff\xd8\xff" + b"\x00" * 20))
        self.assertIsNone(uploads.strip_gif(b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00" + b"\x99"))
        self.assertIsNone(uploads.strip_webp(b"RIFF" + (400).to_bytes(4, "little") + b"WEBP"))
        self.assertIsNone(uploads.strip_metadata(b"anything", "image/svg+xml"))

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
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, member_id))
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
            admin_invite_id = db.execute("""INSERT INTO invitations
              (community_id,created_by,token_hash,expires_at,max_uses,uses,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (community_id, owner_id, security.invite_hash("admin-active"), 2000, 3, 0,
               database.utc_now())).lastrowid
            db.execute("UPDATE memberships SET role='administrator' WHERE community_id=? AND user_id=?",
                       (community_id, member_id))
            admin_visible = list_active_invites(db, community_id, member_id, timestamp=1000)
            admin_revoked = revoke_invite(db, admin_invite_id, member_id)
            owner_revoked = revoke_invite(db, active_id, owner_id)
            remaining = db.execute("SELECT 1 FROM invitations WHERE id=?", (active_id,)).fetchone()
        self.assertEqual([invite["id"] for invite in visible], [active_id])
        self.assertIsNone(forbidden)
        self.assertFalse(member_revoked)
        self.assertEqual([invite["id"] for invite in admin_visible], [admin_invite_id, active_id])
        self.assertTrue(admin_revoked)
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
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, member_id))
            visible = list_community_members(db, community_id, member_id)
            hidden = list_community_members(db, community_id, outsider_id)
        self.assertEqual([member["role"] for member in visible], ["owner", "member"])
        self.assertEqual(visible[0]["id"], owner_id)
        self.assertIsNone(hidden)

    def test_owner_assigns_roles_and_ownership_cannot_be_reassigned(self):
        with database.connect() as db:
            owner_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                  ("role_owner", "unused", database.utc_now())).lastrowid
            member_id = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                   ("role_member", "unused", database.utc_now())).lastrowid
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      ("Role Test", owner_id, database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, member_id))

            promoted = set_member_role(db, community_id, member_id, owner_id, "administrator")
            self.assertEqual(promoted["role"], "administrator")
            self.assertEqual(community_role(db, community_id, member_id), "administrator")
            self.assertTrue(has_role(db, community_id, member_id, "moderator"))
            self.assertEqual([entry["role"] for entry in
                              list_community_members(db, community_id, owner_id)],
                             ["owner", "administrator"])
            self.assertFalse(set_member_role(db, community_id, member_id, owner_id, "owner"))
            self.assertFalse(set_member_role(db, community_id, owner_id, owner_id, "member"))
            self.assertIsNone(set_member_role(db, community_id, owner_id, member_id, "member"))
            self.assertEqual(community_role(db, community_id, owner_id), "owner")

    def test_kick_and_ban_follow_role_hierarchy_and_clear_member_state(self):
        with database.connect() as db:
            actors = {}
            for role in ("owner", "administrator", "moderator", "member", "second_member"):
                actors[role] = db.execute(
                    "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                    (f"moderation_{role}", "unused", database.utc_now())).lastrowid
            community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                      ("Moderation", actors["owner"], database.utc_now())).lastrowid
            for role in actors:
                stored_role = role if role in {"administrator", "moderator"} else "member"
                db.execute("INSERT INTO memberships(community_id,user_id,role) VALUES(?,?,?)",
                           (community_id, actors[role], stored_role))
            channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                    (community_id, "general", database.utc_now())).lastrowid
            message_id = db.execute(
                "INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                (channel_id, actors["member"], "keep this history", database.utc_now())).lastrowid
            db.execute("INSERT INTO channel_reads VALUES(?,?,?)",
                       (actors["member"], channel_id, message_id))
            db.execute("INSERT INTO channel_notifications VALUES(?,?,?)",
                       (actors["member"], channel_id, "none"))

            denied, _ = remove_member(db, community_id, actors["administrator"],
                                      actors["moderator"])
            self.assertEqual(denied, "forbidden")
            kicked, _ = remove_member(db, community_id, actors["member"], actors["moderator"])
            self.assertEqual(kicked, "ok")
            self.assertIsNone(db.execute("SELECT 1 FROM memberships WHERE community_id=? AND user_id=?",
                                         (community_id, actors["member"])).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM channel_reads WHERE user_id=? AND channel_id=?",
                                         (actors["member"], channel_id)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM channel_notifications WHERE user_id=? AND channel_id=?",
                                         (actors["member"], channel_id)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone(),
                                 "moderation removes access, not conversation history")

            banned, _ = remove_member(db, community_id, actors["second_member"],
                                      actors["moderator"], ban=True)
            self.assertEqual(banned, "ok")
            self.assertTrue(is_banned(db, community_id, actors["second_member"]))
            self.assertEqual([entry["user_id"] for entry in
                              list_community_bans(db, community_id, actors["moderator"])],
                             [actors["second_member"]])
            self.assertEqual(unban_member(db, community_id, actors["second_member"],
                                          actors["moderator"]), "ok")

            owner_ban, _ = remove_member(db, community_id, actors["administrator"],
                                         actors["owner"], ban=True)
            self.assertEqual(owner_ban, "ok")
            self.assertEqual(unban_member(db, community_id, actors["administrator"],
                                          actors["moderator"]), "forbidden")
            self.assertEqual(unban_member(db, community_id, actors["administrator"],
                                          actors["owner"]), "ok")

    def message_fixture(self, db, label, attachment=False):
        """Build an owner, an author, a bystander, an outsider, and one message."""
        actors = {}
        for role in ("owner", "author", "bystander", "outsider"):
            actors[role] = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                      (f"{label}_{role}", "unused", database.utc_now())).lastrowid
        community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                  (label, actors["owner"], database.utc_now())).lastrowid
        for role in ("owner", "author", "bystander"):
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, actors[role]))
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

    def test_moderators_and_administrators_may_delete_but_members_may_not(self):
        with database.connect() as db:
            actors, message_id = self.message_fixture(db, "role_delete_rules")
            community_id = db.execute("""SELECT ch.community_id FROM messages m
                                          JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?""",
                                      (message_id,)).fetchone()[0]
            for role, expected in (("moderator", True), ("administrator", True), ("member", False)):
                db.execute("UPDATE memberships SET role=? WHERE community_id=? AND user_id=?",
                           (role, community_id, actors["bystander"]))
                self.assertEqual(may_delete(visible_message(db, message_id, actors["bystander"])), expected)

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
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (shared, actors["resident"]))
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (shared, actors["neighbour"]))
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (elsewhere, actors["stranger"]))

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
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, owner_id))
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, absent_id))
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

    def test_rate_limiter_has_hard_entry_and_key_size_bounds(self):
        limiter = security.RateLimiter(attempts=2, window=60, max_entries=3, max_key_bytes=16)
        self.assertTrue(limiter.allow("one", timestamp=100))
        self.assertTrue(limiter.allow("two", timestamp=100))
        self.assertTrue(limiter.allow("three", timestamp=100))
        self.assertFalse(limiter.allow("four", timestamp=100))
        self.assertFalse(limiter.allow("x" * 17, timestamp=100))
        self.assertEqual(len(limiter._entries), 3)
        self.assertTrue(limiter.allow("later", timestamp=200))
        self.assertEqual(set(limiter._entries), {"later"})

    def test_startup_refuses_usernames_that_differ_only_by_case(self):
        with closing(sqlite3.connect(":memory:")) as memory:
            memory.row_factory = sqlite3.Row
            memory.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)")
            memory.execute("INSERT INTO users(username) VALUES('Sam'),('sam')")
            with self.assertRaises(RuntimeError) as failure:
                database.enforce_username_case_uniqueness(memory)
        self.assertIn("Sam", str(failure.exception))

    def unread_fixture(self, db, label, messages=3):
        """An owner, a member, an outsider, one channel, and posts written by the owner."""
        actors = {}
        for role in ("owner", "member", "outsider"):
            actors[role] = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                      (f"{label}_{role}", "unused", database.utc_now())).lastrowid
        community_id = db.execute("INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                                  (label, actors["owner"], database.utc_now())).lastrowid
        for role in ("owner", "member"):
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, actors[role]))
        channel_id = db.execute("INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                                (community_id, "general", database.utc_now())).lastrowid
        message_ids = [
            db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                       (channel_id, actors["owner"], f"message {index}", database.utc_now())).lastrowid
            for index in range(messages)]
        return actors, community_id, channel_id, message_ids

    def test_unread_counts_ignore_your_own_messages_and_stop_at_the_marker(self):
        with database.connect() as db:
            actors, _, channel_id, message_ids = self.unread_fixture(db, "unread_counts")
            reader = channel_states(db, actors["member"])[channel_id]
            writer = channel_states(db, actors["owner"])[channel_id]
            self.assertEqual(reader["unread"], 3)
            self.assertEqual(reader["last_read_message_id"], 0)
            self.assertEqual(writer["unread"], 0, "sending a message is not a way of missing it")

            mark_read(db, channel_id, actors["member"], message_ids[1])
            self.assertEqual(channel_states(db, actors["member"])[channel_id]["unread"], 1)

    def test_a_read_marker_moves_forward_only_and_never_past_the_channel(self):
        with database.connect() as db:
            actors, _, channel_id, message_ids = self.unread_fixture(db, "unread_marker")
            mark_read(db, channel_id, actors["member"], message_ids[2])
            rewound = mark_read(db, channel_id, actors["member"], message_ids[0])
            self.assertEqual(rewound["last_read_message_id"], message_ids[2],
                             "a stale tab must not make read messages unread again")

            ahead = mark_read(db, channel_id, actors["member"], message_ids[2] + 10_000)
            self.assertEqual(ahead["last_read_message_id"], message_ids[2],
                             "a client must not silence messages it has never seen")

    def test_outsiders_cannot_read_or_change_channel_state(self):
        with database.connect() as db:
            actors, _, channel_id, message_ids = self.unread_fixture(db, "unread_outsider")
            self.assertIsNone(mark_read(db, channel_id, actors["outsider"], message_ids[0]))
            self.assertFalse(set_channel_mode(db, channel_id, actors["outsider"], "none"))
            self.assertNotIn(channel_id, channel_states(db, actors["outsider"]))

    def test_a_new_member_does_not_inherit_the_backlog(self):
        with database.connect() as db:
            actors, community_id, channel_id, message_ids = self.unread_fixture(db, "unread_arrival")
            arrival = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                                 ("unread_arrival_joiner", "unused", database.utc_now())).lastrowid
            db.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)", (community_id, arrival))
            mark_community_read(db, community_id, arrival)
            settled = channel_states(db, arrival)[channel_id]
            self.assertEqual(settled["unread"], 0)
            self.assertEqual(settled["last_read_message_id"], message_ids[-1])

            db.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",
                       (channel_id, actors["owner"], "after you arrived", database.utc_now()))
            self.assertEqual(channel_states(db, arrival)[channel_id]["unread"], 1,
                             "conversation after the arrival is theirs to read")
            mark_community_read(db, community_id, arrival)
            self.assertEqual(channel_states(db, arrival)[channel_id]["unread"], 1,
                             "settling a backlog once must not keep swallowing new messages")

    def test_notification_modes_layer_a_channel_choice_over_the_account_default(self):
        with database.connect() as db:
            actors, _, channel_id, _ = self.unread_fixture(db, "notify_modes")
            member = actors["member"]
            self.assertEqual(account_mode(db, member), "all")
            self.assertIsNone(channel_states(db, member)[channel_id]["notify"])

            set_account_mode(db, member, "none")
            self.assertEqual(account_mode(db, member), "none")

            self.assertTrue(set_channel_mode(db, channel_id, member, "all"))
            self.assertEqual(channel_states(db, member)[channel_id]["notify"], "all")

            self.assertTrue(set_channel_mode(db, channel_id, member, None))
            self.assertIsNone(channel_states(db, member)[channel_id]["notify"],
                              "clearing an override must fall back to the account default")

    def test_muting_a_channel_still_counts_its_unread_messages(self):
        """Muting is about not being interrupted, not about pretending nothing happened."""
        with database.connect() as db:
            actors, _, channel_id, _ = self.unread_fixture(db, "notify_muted")
            set_channel_mode(db, channel_id, actors["member"], "none")
            muted = channel_states(db, actors["member"])[channel_id]
        self.assertEqual(muted["notify"], "none")
        self.assertEqual(muted["unread"], 3)

    def test_read_markers_and_mutes_are_removed_with_their_channel(self):
        with database.connect() as db:
            actors, _, channel_id, message_ids = self.unread_fixture(db, "notify_cascade")
            mark_read(db, channel_id, actors["member"], message_ids[0])
            set_channel_mode(db, channel_id, actors["member"], "none")
            db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            self.assertIsNone(db.execute("SELECT 1 FROM channel_reads WHERE channel_id=?",
                                         (channel_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM channel_notifications WHERE channel_id=?",
                                         (channel_id,)).fetchone())

    def test_username_rules(self):
        self.assertTrue(security.USERNAME_RE.fullmatch("friendly_user"))
        self.assertFalse(security.USERNAME_RE.fullmatch("x"))
        self.assertFalse(security.USERNAME_RE.fullmatch("not friendly"))


if __name__ == "__main__":
    unittest.main()
