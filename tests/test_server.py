import os
import tempfile
import time
import unittest
from pathlib import Path

temporary = tempfile.TemporaryDirectory()
os.environ["CAMPFIRE_DB"] = str(Path(temporary.name) / "test.db")

from campfire import database, security, uploads
from campfire.services.communities import list_community_members


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

    def test_rate_limiter_window(self):
        limiter = security.RateLimiter(attempts=2, window=10)
        self.assertTrue(limiter.allow("client", timestamp=100))
        self.assertTrue(limiter.allow("client", timestamp=101))
        self.assertFalse(limiter.allow("client", timestamp=102))
        self.assertTrue(limiter.allow("client", timestamp=111))

    def test_username_rules(self):
        self.assertTrue(security.USERNAME_RE.fullmatch("friendly_user"))
        self.assertFalse(security.USERNAME_RE.fullmatch("x"))
        self.assertFalse(security.USERNAME_RE.fullmatch("not friendly"))


if __name__ == "__main__":
    unittest.main()
