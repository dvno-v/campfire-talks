"""Schema version 4: voice channel types and bounded room leases."""

VERSION = 4
NAME = "voice_rooms"


def apply(database, _applied_at):
    # SQLite can add a checked, non-null column when its default satisfies the
    # constraint. Existing channels are text channels by definition.
    database.execute("""ALTER TABLE channels ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'
                        CHECK (kind IN ('text','voice'))""")
    database.execute("""CREATE TABLE voice_leases (
      channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL UNIQUE,
      key_fingerprint TEXT NOT NULL CHECK (
        length(key_fingerprint)=64 AND key_fingerprint NOT GLOB '*[^0-9a-f]*'),
      expires_at INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (channel_id,user_id)
    )""")
    database.execute("CREATE INDEX idx_voice_leases_expiry ON voice_leases(expires_at)")

