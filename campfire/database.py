"""SQLite connection, schema, and small serialization helpers."""

import contextlib
import os
import sqlite3
import time
from datetime import datetime, timezone

from .config import DB_PATH
from .migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
from .migrations.v002_legacy_compatibility import enforce_username_case_uniqueness
from .migrations.v002_legacy_compatibility import rebuild_sessions_with_stable_ids


class Connection(sqlite3.Connection):
    """Commit/rollback and close when used as a context manager.

    sqlite3's default context manager manages only the transaction. Request
    handlers open short-lived connections, so leaving close to garbage
    collection needlessly retains descriptors and emits ResourceWarning on
    current Python releases.
    """

    def __exit__(self, exception_type, exception, traceback):
        try:
            return super().__exit__(exception_type, exception, traceback)
        finally:
            self.close()


def connect():
    """Open the database, keeping it readable only by the service account.

    Messages, password hashes and session digests all live in this one file, so
    the default umask is not good enough: another local account must not be able
    to read it just because the directory was created loosely.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not DB_PATH.exists()
    database = sqlite3.connect(DB_PATH, timeout=10, factory=Connection)
    if fresh:
        restrict_permissions()
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    # Readers and the writer would otherwise lock each other out, which matters
    # because every live stream holds a connection.
    database.execute("PRAGMA journal_mode = WAL")
    return database


def restrict_permissions():
    """Narrow the data directory and database files to the owner alone."""
    with contextlib.suppress(OSError):
        os.chmod(DB_PATH.parent, 0o700)
    for path in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-wal"), DB_PATH.with_name(DB_PATH.name + "-shm")):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def schema_version(database):
    """Return the latest recorded migration, or zero for an unversioned database."""
    table = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not table:
        return 0
    row = database.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()
    return int(row[0])


def migrate_database(database, migrations=MIGRATIONS):
    """Apply each pending migration in its own all-or-nothing transaction."""
    versions = [migration.VERSION for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("Database migrations must be consecutive and start at version 1")

    database.commit()
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
        )""")
        database.commit()
    except Exception:
        database.rollback()
        raise

    applied = {row[0] for row in database.execute(
        "SELECT version FROM schema_migrations ORDER BY version")}
    unknown = applied - set(versions)
    if unknown:
        raise RuntimeError(
            f"Database schema is newer than this Campfire release (version {max(unknown)})")
    expected_applied = set(range(1, max(applied, default=0) + 1))
    if applied != expected_applied:
        raise RuntimeError("Database migration history is incomplete")

    for migration in migrations:
        if migration.VERSION in applied:
            continue
        applied_at = utc_now()
        database.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(database, applied_at)
            database.execute(
                "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (migration.VERSION, migration.NAME, applied_at))
            database.execute(f"PRAGMA user_version = {migration.VERSION}")
            database.commit()
        except Exception:
            database.rollback()
            raise
    return schema_version(database)


def initialize_database():
    database = connect()
    try:
        version = migrate_database(database)
        database.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
        database.execute("DELETE FROM invitations WHERE expires_at<=?", (int(time.time()),))
        database.execute("DELETE FROM upload_reservations WHERE expires_at<=?", (int(time.time()),))
        database.execute("DELETE FROM webauthn_challenges WHERE expires_at<=?", (int(time.time()),))
        database.commit()
    finally:
        database.close()
    # WAL's side files appear only once something has been written.
    restrict_permissions()
    return version


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def message_from_row(row):
    columns = row.keys()
    message = {
        "id": row["id"], "channel_id": row["channel_id"], "body": row["body"],
        "created_at": row["created_at"], "author_id": row["author_id"], "username": row["username"],
        "edited_at": row["edited_at"] if "edited_at" in columns else None,
        "attachment": None,
    }
    if "attachment_id" in columns and row["attachment_id"] is not None:
        message["attachment"] = {
            "id": row["attachment_id"], "name": row["original_name"],
            "mime_type": row["mime_type"], "byte_size": row["byte_size"],
        }
    return message
