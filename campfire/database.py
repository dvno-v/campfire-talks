"""SQLite connection, schema, and small serialization helpers."""

import contextlib
import os
import sqlite3
import time
from datetime import datetime, timezone

from .config import DB_PATH


def connect():
    """Open the database, keeping it readable only by the service account.

    Messages, password hashes and session digests all live in this one file, so
    the default umask is not good enough: another local account must not be able
    to read it just because the directory was created loosely.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not DB_PATH.exists()
    database = sqlite3.connect(DB_PATH, timeout=10)
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


def initialize_database():
    with connect() as database:
        database.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        -- AUTOINCREMENT, so a revoked session's id is never handed to a later
        -- one. A plain rowid is reused as soon as the highest row is deleted,
        -- which would let a stale session list revoke the wrong session.
        CREATE TABLE IF NOT EXISTS sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          token TEXT NOT NULL UNIQUE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS communities (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL,
          -- Zero means keep indefinitely, which stays the default: a community
          -- that has never chosen is never quietly pruned.
          message_retention_days INTEGER NOT NULL DEFAULT 0
            CHECK (message_retention_days >= 0),
          attachment_retention_days INTEGER NOT NULL DEFAULT 0
            CHECK (attachment_retention_days >= 0)
        );
        CREATE TABLE IF NOT EXISTS memberships (
          community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('administrator', 'moderator', 'member')),
          PRIMARY KEY (community_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS community_bans (
          community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          banned_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          role_at_ban TEXT NOT NULL CHECK (role_at_ban IN ('administrator', 'moderator', 'member')),
          created_at TEXT NOT NULL,
          PRIMARY KEY (community_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS channels (
          id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
          name TEXT NOT NULL, created_at TEXT NOT NULL,
          post_min_role TEXT NOT NULL DEFAULT 'member'
            CHECK (post_min_role IN ('administrator', 'moderator', 'member')),
          slow_mode_seconds INTEGER NOT NULL DEFAULT 0 CHECK (slow_mode_seconds >= 0),
          uploads_allowed INTEGER NOT NULL DEFAULT 1 CHECK (uploads_allowed IN (0, 1)),
          UNIQUE (community_id, name)
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
          author_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL, created_at TEXT NOT NULL,
          attachment_id INTEGER REFERENCES attachments(id), edited_at TEXT
        );
        CREATE TABLE IF NOT EXISTS invitations (
          id INTEGER PRIMARY KEY,
          community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
          created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          token_hash TEXT UNIQUE NOT NULL,
          expires_at INTEGER NOT NULL,
          max_uses INTEGER NOT NULL DEFAULT 10,
          uses INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attachments (
          id INTEGER PRIMARY KEY,
          channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
          uploader_id INTEGER NOT NULL REFERENCES users(id),
          storage_name TEXT UNIQUE NOT NULL,
          original_name TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          byte_size INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channel_reads (
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
          last_read_message_id INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (user_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS notification_preferences (
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channel_notifications (
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
          mode TEXT NOT NULL,
          PRIMARY KEY (user_id, channel_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
        CREATE INDEX IF NOT EXISTS idx_invites_token ON invitations(token_hash);
        """)
        message_columns = {row[1] for row in database.execute("PRAGMA table_info(messages)")}
        if "attachment_id" not in message_columns:
            database.execute("ALTER TABLE messages ADD COLUMN attachment_id INTEGER REFERENCES attachments(id)")
        if "edited_at" not in message_columns:
            database.execute("ALTER TABLE messages ADD COLUMN edited_at TEXT")
        membership_columns = {row[1] for row in database.execute("PRAGMA table_info(memberships)")}
        if "role" not in membership_columns:
            database.execute("""ALTER TABLE memberships ADD COLUMN role TEXT NOT NULL DEFAULT 'member'
                                CHECK (role IN ('administrator', 'moderator', 'member'))""")
        session_columns = {row[1] for row in database.execute("PRAGMA table_info(sessions)")}
        if "created_at" not in session_columns:
            database.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")
            database.execute("UPDATE sessions SET created_at=? WHERE created_at IS NULL", (utc_now(),))
        if "id" not in session_columns:
            rebuild_sessions_with_stable_ids(database)
        community_columns = {row[1] for row in database.execute("PRAGMA table_info(communities)")}
        for column in ("message_retention_days", "attachment_retention_days"):
            if column not in community_columns:
                database.execute(f"ALTER TABLE communities ADD COLUMN {column} "
                                 "INTEGER NOT NULL DEFAULT 0 CHECK (" + column + " >= 0)")
        channel_columns = {row[1] for row in database.execute("PRAGMA table_info(channels)")}
        if "post_min_role" not in channel_columns:
            database.execute("ALTER TABLE channels ADD COLUMN post_min_role TEXT NOT NULL "
                             "DEFAULT 'member' "
                             "CHECK (post_min_role IN ('administrator', 'moderator', 'member'))")
        if "slow_mode_seconds" not in channel_columns:
            database.execute("ALTER TABLE channels ADD COLUMN slow_mode_seconds "
                             "INTEGER NOT NULL DEFAULT 0 CHECK (slow_mode_seconds >= 0)")
        if "uploads_allowed" not in channel_columns:
            database.execute("ALTER TABLE channels ADD COLUMN uploads_allowed "
                             "INTEGER NOT NULL DEFAULT 1 CHECK (uploads_allowed IN (0, 1))")
        enforce_username_case_uniqueness(database)
        database.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
        database.execute("DELETE FROM invitations WHERE expires_at<=?", (int(time.time()),))
    # WAL's side files appear only once something has been written.
    restrict_permissions()


def rebuild_sessions_with_stable_ids(database):
    """Give an older `sessions` table a surrogate key that is never reused.

    A column cannot be promoted to AUTOINCREMENT in place, so the table is
    rebuilt and its rows copied across. Everyone stays signed in: the tokens
    are what authenticate, and they are carried over untouched.
    """
    database.executescript("""
      CREATE TABLE sessions_rebuilt (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
      );
      INSERT INTO sessions_rebuilt(token,user_id,expires_at,created_at)
        SELECT token,user_id,expires_at,created_at FROM sessions ORDER BY rowid;
      DROP TABLE sessions;
      ALTER TABLE sessions_rebuilt RENAME TO sessions;
    """)


def enforce_username_case_uniqueness(database):
    """Guarantee that usernames differing only by case cannot coexist.

    Sign-in resolves usernames case-insensitively, so `Sam` and `sam` as
    separate accounts would let the older row answer for both and lock the
    newer account out of its own name. Databases created before this rule may
    already hold such pairs; refuse to start rather than silently choosing a
    winner. Usernames are restricted to ASCII, which SQLite's NOCASE collation
    folds completely.
    """
    collisions = database.execute("""
      SELECT group_concat(username, ', ') AS names FROM users
      GROUP BY username COLLATE NOCASE HAVING COUNT(*) > 1
    """).fetchall()
    if collisions:
        conflicting = "; ".join(row["names"] for row in collisions)
        raise RuntimeError(
            "Campfire cannot start: these accounts differ only by capitalization "
            f"and must be renamed or removed first: {conflicting}")
    database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE)")


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
