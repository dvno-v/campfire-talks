"""Schema version 2: bring every pre-migration Campfire database forward."""

VERSION = 2
NAME = "legacy_compatibility_and_invariants"


def columns(database, table):
    return {row[1] for row in database.execute(f"PRAGMA table_info({table})")}


def rebuild_sessions_with_stable_ids(database):
    database.execute("""CREATE TABLE sessions_rebuilt (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token TEXT NOT NULL UNIQUE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
    )""")
    database.execute("""INSERT INTO sessions_rebuilt(token,user_id,expires_at,created_at)
      SELECT token,user_id,expires_at,created_at FROM sessions ORDER BY rowid""")
    database.execute("DROP TABLE sessions")
    database.execute("ALTER TABLE sessions_rebuilt RENAME TO sessions")


def enforce_username_case_uniqueness(database):
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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase "
        "ON users(username COLLATE NOCASE)")


def apply(database, applied_at):
    message_columns = columns(database, "messages")
    if "attachment_id" not in message_columns:
        database.execute(
            "ALTER TABLE messages ADD COLUMN attachment_id INTEGER REFERENCES attachments(id)")
    if "edited_at" not in message_columns:
        database.execute("ALTER TABLE messages ADD COLUMN edited_at TEXT")

    membership_columns = columns(database, "memberships")
    if "role" not in membership_columns:
        database.execute("""ALTER TABLE memberships ADD COLUMN role TEXT NOT NULL
          DEFAULT 'member' CHECK (role IN ('administrator', 'moderator', 'member'))""")

    session_columns = columns(database, "sessions")
    if "created_at" not in session_columns:
        database.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")
        database.execute("UPDATE sessions SET created_at=? WHERE created_at IS NULL", (applied_at,))
    if "id" not in session_columns:
        rebuild_sessions_with_stable_ids(database)

    community_columns = columns(database, "communities")
    for column in ("message_retention_days", "attachment_retention_days"):
        if column not in community_columns:
            database.execute(
                f"ALTER TABLE communities ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0 "
                f"CHECK ({column} >= 0)")

    channel_columns = columns(database, "channels")
    if "post_min_role" not in channel_columns:
        database.execute("""ALTER TABLE channels ADD COLUMN post_min_role TEXT NOT NULL
          DEFAULT 'member' CHECK (post_min_role IN ('administrator', 'moderator', 'member'))""")
    if "slow_mode_seconds" not in channel_columns:
        database.execute("""ALTER TABLE channels ADD COLUMN slow_mode_seconds INTEGER NOT NULL
          DEFAULT 0 CHECK (slow_mode_seconds >= 0)""")
    if "uploads_allowed" not in channel_columns:
        database.execute("""ALTER TABLE channels ADD COLUMN uploads_allowed INTEGER NOT NULL
          DEFAULT 1 CHECK (uploads_allowed IN (0, 1))""")

    enforce_username_case_uniqueness(database)

