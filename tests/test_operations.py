"""Operational guarantees for migrations, snapshots, verification, and restore."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from campfire import config, database
from campfire.instance_lock import InstanceBusy, operation_lock, server_lock
from campfire.migrations import MIGRATIONS
from campfire.operations import BackupError, create_backup, create_encrypted_backup
from campfire.operations import restore_backup, restore_encrypted_backup, verify_backup
from campfire.operations import verify_encrypted_backup


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "live" / "campfire.db"
        self.upload_dir = self.root / "live" / "uploads"
        self.original_database_path = database.DB_PATH
        database.DB_PATH = self.database_path
        database.initialize_database()
        self.upload_dir.mkdir(parents=True, mode=0o700)
        self.content = b"backup attachment bytes"
        with closing(database.connect()) as connection, connection:
            owner = connection.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                ("backup_owner", "unused", database.utc_now())).lastrowid
            community = connection.execute(
                "INSERT INTO communities(name,owner_id,created_at) VALUES(?,?,?)",
                ("Backup", owner, database.utc_now())).lastrowid
            connection.execute("INSERT INTO memberships(community_id,user_id) VALUES(?,?)",
                               (community, owner))
            channel = connection.execute(
                "INSERT INTO channels(community_id,name,created_at) VALUES(?,?,?)",
                (community, "general", database.utc_now())).lastrowid
            connection.execute("""INSERT INTO attachments
              (channel_id,uploader_id,storage_name,original_name,mime_type,byte_size,created_at)
              VALUES(?,?,?,?,?,?,?)""",
              (channel, owner, "stored.png", "photo.png", "image/png",
               len(self.content), database.utc_now()))
        (self.upload_dir / "stored.png").write_bytes(self.content)

    def tearDown(self):
        database.DB_PATH = self.original_database_path
        self.temporary.cleanup()

    def test_versioned_migrations_are_recorded_and_a_failed_one_rolls_back(self):
        with closing(database.connect()) as connection, connection:
            applied = connection.execute(
                "SELECT version,name FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual([row[0] for row in applied], [1, 2, 3])

            def fail_migration(db, _timestamp):
                db.execute("CREATE TABLE must_rollback(value TEXT)")
                raise RuntimeError("simulated migration failure")

            failing = SimpleNamespace(VERSION=4, NAME="failure", apply=fail_migration)
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                database.migrate_database(connection, MIGRATIONS + (failing,))
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='must_rollback'").fetchone())
            self.assertEqual(database.schema_version(connection), 3)

    def test_backup_is_self_verifying_and_restore_replaces_database_and_files(self):
        destination = self.root / "backups" / "before-change"
        (self.upload_dir / "untracked.tmp").write_bytes(b"not database content")
        manifest = create_backup(destination, self.database_path, self.upload_dir)
        self.assertEqual(manifest["schema_version"], 3)
        entries_before_verify = sorted(path.name for path in destination.iterdir())
        self.assertEqual(verify_backup(destination)["attachments"][0]["name"], "stored.png")
        self.assertEqual(sorted(path.name for path in destination.iterdir()), entries_before_verify)
        self.assertEqual(entries_before_verify, ["campfire.db", "manifest.json", "uploads"])
        self.assertFalse((destination / "uploads" / "untracked.tmp").exists())

        with closing(database.connect()) as connection, connection:
            connection.execute("DELETE FROM attachments")
            connection.execute("UPDATE users SET username='changed' WHERE username='backup_owner'")
        (self.upload_dir / "stored.png").unlink()

        restore_backup(destination, self.database_path, self.upload_dir)
        with closing(sqlite3.connect(self.database_path)) as restored:
            self.assertEqual(restored.execute(
                "SELECT username FROM users").fetchone()[0], "backup_owner")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 1)
        self.assertEqual((self.upload_dir / "stored.png").read_bytes(), self.content)

    def test_tampering_and_missing_live_files_fail_without_publishing_a_backup(self):
        destination = self.root / "backup"
        create_backup(destination, self.database_path, self.upload_dir)
        (destination / "uploads" / "stored.png").write_bytes(b"tampered")
        with self.assertRaisesRegex(BackupError, "hash or size"):
            verify_backup(destination)

        (self.upload_dir / "stored.png").unlink()
        incomplete = self.root / "missing-file-backup"
        with self.assertRaisesRegex(BackupError, "missing or unsafe"):
            create_backup(incomplete, self.database_path, self.upload_dir)
        self.assertFalse(incomplete.exists())

    def test_encrypted_backup_authenticates_verifies_and_restores(self):
        key_file = self.root / "backup.key"
        key_file.write_bytes(os.urandom(32))
        key_file.chmod(0o600)
        destination = self.root / "backups" / "snapshot.campfire-backup"

        manifest = create_encrypted_backup(
            destination, key_file, self.database_path, self.upload_dir)
        encrypted = destination.read_bytes()
        self.assertEqual(manifest["schema_version"], 3)
        self.assertNotIn(self.content, encrypted)
        self.assertNotIn(b"backup_owner", encrypted)
        self.assertEqual(
            verify_encrypted_backup(destination, key_file, self.database_path.parent)["format"],
            "campfire.backup.v1")

        with closing(database.connect()) as connection, connection:
            connection.execute("DELETE FROM attachments")
            connection.execute("UPDATE users SET username='changed' WHERE username='backup_owner'")
        (self.upload_dir / "stored.png").unlink()
        restore_encrypted_backup(
            destination, key_file, self.database_path, self.upload_dir)
        with closing(sqlite3.connect(self.database_path)) as restored:
            self.assertEqual(restored.execute(
                "SELECT username FROM users").fetchone()[0], "backup_owner")
        self.assertEqual((self.upload_dir / "stored.png").read_bytes(), self.content)
        self.assertFalse(list(self.database_path.parent.glob(".encrypted-*-staging-*")))

    def test_encrypted_backup_rejects_tampering_wrong_keys_and_bad_key_sizes(self):
        key_file = self.root / "backup.key"
        key_file.write_bytes(os.urandom(32))
        destination = self.root / "snapshot.campfire-backup"
        create_encrypted_backup(destination, key_file, self.database_path, self.upload_dir)

        tampered = bytearray(destination.read_bytes())
        tampered[len(tampered) // 2] ^= 1
        destination.write_bytes(tampered)
        with self.assertRaisesRegex(BackupError, "authentication failed"):
            verify_encrypted_backup(destination, key_file, self.database_path.parent)

        clean = self.root / "clean.campfire-backup"
        create_encrypted_backup(clean, key_file, self.database_path, self.upload_dir)
        wrong_key = self.root / "wrong.key"
        wrong_key.write_bytes(os.urandom(32))
        with self.assertRaisesRegex(BackupError, "authentication failed"):
            verify_encrypted_backup(clean, wrong_key, self.database_path.parent)
        short_key = self.root / "short.key"
        short_key.write_bytes(b"not enough entropy")
        with self.assertRaisesRegex(BackupError, "exactly 32"):
            verify_encrypted_backup(clean, short_key, self.database_path.parent)
        linked_key = self.root / "linked.key"
        linked_key.symlink_to(key_file)
        with self.assertRaisesRegex(BackupError, "symbolic link"):
            verify_encrypted_backup(clean, linked_key, self.database_path.parent)
        linked_backup = self.root / "linked.campfire-backup"
        linked_backup.symlink_to(clean)
        with self.assertRaisesRegex(BackupError, "symbolic link"):
            verify_encrypted_backup(linked_backup, key_file, self.database_path.parent)

    def test_restore_refuses_while_the_server_lock_is_shared(self):
        destination = self.root / "backup"
        create_backup(destination, self.database_path, self.upload_dir)
        with operation_lock(self.database_path, exclusive=False):
            with self.assertRaises(InstanceBusy):
                restore_backup(destination, self.database_path, self.upload_dir)

    def test_restore_runtime_failure_rolls_live_paths_back(self):
        destination = self.root / "backup"
        create_backup(destination, self.database_path, self.upload_dir)
        with closing(database.connect()) as connection, connection:
            connection.execute("UPDATE users SET username='keep_me' WHERE username='backup_owner'")
        (self.upload_dir / "stored.png").write_bytes(b"keep this live file")

        from campfire import operations
        real_replace = operations.os.replace
        failed = False

        def fail_install(source, target):
            nonlocal failed
            source = Path(source)
            target = Path(target)
            if (not failed and target == self.database_path
                    and ".restore-" in source.name):
                failed = True
                raise OSError("simulated install failure")
            return real_replace(source, target)

        with patch("campfire.operations.os.replace", side_effect=fail_install):
            with self.assertRaisesRegex(OSError, "simulated"):
                restore_backup(destination, self.database_path, self.upload_dir)
        with closing(sqlite3.connect(self.database_path)) as live:
            self.assertEqual(live.execute("SELECT username FROM users").fetchone()[0], "keep_me")
        self.assertEqual((self.upload_dir / "stored.png").read_bytes(), b"keep this live file")

    def test_backup_destination_cannot_be_nested_in_live_uploads(self):
        with self.assertRaisesRegex(BackupError, "inside the live upload"):
            create_backup(self.upload_dir / "backup", self.database_path, self.upload_dir)

    def test_a_second_server_process_is_refused(self):
        with server_lock(self.database_path):
            with self.assertRaises(InstanceBusy):
                with server_lock(self.database_path):
                    self.fail("a second process must not acquire the server lock")


class ConfigurationTests(unittest.TestCase):
    def test_private_defaults_are_safe(self):
        with patch.multiple(config, HOST="127.0.0.1", PUBLIC_ORIGIN="",
                            SECURE_COOKIES=False, TRUSTED_PROXIES=(),
                            MAX_STORAGE_BYTES=0, MAX_EVENT_STREAMS=32,
                            MAX_EVENT_STREAMS_PER_USER=4):
            self.assertTrue(config.validate_configuration())

    def test_network_listener_fails_closed_without_every_public_safeguard(self):
        with patch.multiple(config, HOST="0.0.0.0", PUBLIC_ORIGIN="",
                            SECURE_COOKIES=False, TRUSTED_PROXIES=(),
                            MAX_STORAGE_BYTES=0, MAX_EVENT_STREAMS=32,
                            MAX_EVENT_STREAMS_PER_USER=4):
            with self.assertRaises(config.ConfigError) as failure:
                config.validate_configuration()
        message = str(failure.exception)
        self.assertIn("CAMPFIRE_ORIGIN", message)
        self.assertIn("CAMPFIRE_TRUSTED_PROXIES", message)
        self.assertIn("CAMPFIRE_MAX_STORAGE_BYTES", message)

    def test_complete_https_proxy_configuration_is_accepted(self):
        trusted = config._networks("172.31.0.2")
        with patch.multiple(config, HOST="0.0.0.0",
                            PUBLIC_ORIGIN="https://chat.example.net",
                            SECURE_COOKIES=True, TRUSTED_PROXIES=trusted,
                            MAX_STORAGE_BYTES=10_000, MAX_EVENT_STREAMS=32,
                            MAX_EVENT_STREAMS_PER_USER=4):
            self.assertTrue(config.validate_configuration())

    def test_public_http_and_ambiguous_origins_are_rejected(self):
        trusted = config._networks("172.31.0.2")
        for origin in ("http://chat.example.net", "https://chat.example.net/path",
                       "https://chat.example.net/"):
            with self.subTest(origin=origin), patch.multiple(
                    config, HOST="0.0.0.0", PUBLIC_ORIGIN=origin,
                    SECURE_COOKIES=True, TRUSTED_PROXIES=trusted,
                    MAX_STORAGE_BYTES=10_000, MAX_EVENT_STREAMS=32,
                    MAX_EVENT_STREAMS_PER_USER=4):
                with self.assertRaises(config.ConfigError):
                    config.validate_configuration()

    def test_database_and_upload_paths_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as folder, patch.multiple(
                config, HOST="127.0.0.1", PUBLIC_ORIGIN="", SECURE_COOKIES=False,
                TRUSTED_PROXIES=(), MAX_STORAGE_BYTES=0, MAX_EVENT_STREAMS=32,
                MAX_EVENT_STREAMS_PER_USER=4, DB_PATH=Path(folder) / "data",
                UPLOAD_DIR=Path(folder) / "data" / "uploads"):
            with self.assertRaisesRegex(config.ConfigError, "non-nested"):
                config.validate_configuration()


if __name__ == "__main__":
    unittest.main()
