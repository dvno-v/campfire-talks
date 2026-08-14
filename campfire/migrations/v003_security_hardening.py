"""Schema version 3: atomic upload reservations and WebAuthn credentials."""

VERSION = 3
NAME = "security_hardening"


def apply(database, _applied_at):
    database.execute("""CREATE TABLE upload_reservations (
      token TEXT PRIMARY KEY,
      byte_size INTEGER NOT NULL CHECK (byte_size > 0),
      expires_at INTEGER NOT NULL
    )""")
    database.execute("""CREATE TABLE passkeys (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      credential_id BLOB NOT NULL UNIQUE,
      public_key BLOB NOT NULL,
      sign_count INTEGER NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
      name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
      created_at TEXT NOT NULL,
      last_used_at TEXT
    )""")
    database.execute("CREATE INDEX idx_passkeys_user ON passkeys(user_id,id)")
    database.execute("""CREATE TABLE webauthn_challenges (
      token_hash TEXT PRIMARY KEY,
      user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
      purpose TEXT NOT NULL CHECK (purpose IN ('register','authenticate')),
      challenge BLOB NOT NULL,
      expires_at INTEGER NOT NULL,
      created_at TEXT NOT NULL
    )""")

