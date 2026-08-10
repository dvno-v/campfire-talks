"""Schema version 1: the complete persistent model."""

VERSION = 1
NAME = "initial_schema"


STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
      password_hash TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token TEXT NOT NULL UNIQUE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS communities (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL,
      owner_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
      message_retention_days INTEGER NOT NULL DEFAULT 0
        CHECK (message_retention_days >= 0),
      attachment_retention_days INTEGER NOT NULL DEFAULT 0
        CHECK (attachment_retention_days >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS memberships (
      community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('administrator', 'moderator', 'member')),
      PRIMARY KEY (community_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS community_bans (
      community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      banned_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
      role_at_ban TEXT NOT NULL
        CHECK (role_at_ban IN ('administrator', 'moderator', 'member')),
      created_at TEXT NOT NULL,
      PRIMARY KEY (community_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS channels (
      id INTEGER PRIMARY KEY,
      community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
      name TEXT NOT NULL, created_at TEXT NOT NULL,
      post_min_role TEXT NOT NULL DEFAULT 'member'
        CHECK (post_min_role IN ('administrator', 'moderator', 'member')),
      slow_mode_seconds INTEGER NOT NULL DEFAULT 0 CHECK (slow_mode_seconds >= 0),
      uploads_allowed INTEGER NOT NULL DEFAULT 1 CHECK (uploads_allowed IN (0, 1)),
      UNIQUE (community_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS attachments (
      id INTEGER PRIMARY KEY,
      channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
      uploader_id INTEGER NOT NULL REFERENCES users(id),
      storage_name TEXT UNIQUE NOT NULL,
      original_name TEXT NOT NULL, mime_type TEXT NOT NULL,
      byte_size INTEGER NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY,
      channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
      author_id INTEGER NOT NULL REFERENCES users(id),
      body TEXT NOT NULL, created_at TEXT NOT NULL,
      attachment_id INTEGER REFERENCES attachments(id), edited_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS invitations (
      id INTEGER PRIMARY KEY,
      community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
      created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT UNIQUE NOT NULL, expires_at INTEGER NOT NULL,
      max_uses INTEGER NOT NULL DEFAULT 10, uses INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS channel_reads (
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
      last_read_message_id INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (user_id, channel_id)
    )""",
    """CREATE TABLE IF NOT EXISTS notification_preferences (
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      mode TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS channel_notifications (
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
      mode TEXT NOT NULL,
      PRIMARY KEY (user_id, channel_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_invites_token ON invitations(token_hash)",
)


def apply(database, _applied_at):
    for statement in STATEMENTS:
        database.execute(statement)

