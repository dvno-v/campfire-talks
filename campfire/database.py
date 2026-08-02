"""SQLite connection, schema, and small serialization helpers."""

import sqlite3
import time
from datetime import datetime, timezone

from .config import DB_PATH


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DB_PATH, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return database


def initialize_database():
    with connect() as database:
        database.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS communities (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memberships (
          community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          PRIMARY KEY (community_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS channels (
          id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
          name TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE (community_id, name)
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
          author_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL, created_at TEXT NOT NULL,
          attachment_id INTEGER REFERENCES attachments(id)
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
        CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
        CREATE INDEX IF NOT EXISTS idx_invites_token ON invitations(token_hash);
        """)
        message_columns = {row[1] for row in database.execute("PRAGMA table_info(messages)")}
        if "attachment_id" not in message_columns:
            database.execute("ALTER TABLE messages ADD COLUMN attachment_id INTEGER REFERENCES attachments(id)")
        database.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
        database.execute("DELETE FROM invitations WHERE expires_at<=?", (int(time.time()),))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def message_from_row(row):
    message = {
        "id": row["id"], "channel_id": row["channel_id"], "body": row["body"],
        "created_at": row["created_at"], "author_id": row["author_id"], "username": row["username"],
        "attachment": None,
    }
    if "attachment_id" in row.keys() and row["attachment_id"] is not None:
        message["attachment"] = {
            "id": row["attachment_id"], "name": row["original_name"],
            "mime_type": row["mime_type"], "byte_size": row["byte_size"],
        }
    return message
