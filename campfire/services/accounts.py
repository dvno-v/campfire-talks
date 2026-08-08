"""Actor-authorized password and session lifecycle workflows."""

import time

from ..security import password_hash, password_matches


def list_sessions(database, actor_id, current_token, timestamp=None):
    """Return the actor's active sessions without collecting device metadata."""
    timestamp = int(time.time() if timestamp is None else timestamp)
    rows = database.execute("""
      SELECT rowid id,created_at,expires_at,token=? is_current
      FROM sessions WHERE user_id=? AND expires_at>?
      ORDER BY is_current DESC,created_at DESC,rowid DESC
    """, (current_token, actor_id, timestamp)).fetchall()
    return [{"id": row["id"], "created_at": row["created_at"],
             "expires_at": row["expires_at"], "current": bool(row["is_current"])}
            for row in rows]


def revoke_session(database, actor_id, session_id, current_token, timestamp=None):
    """Delete one active session owned by the actor.

    Returns ``None`` for a missing/expired session, otherwise whether the
    deleted session was the one making this request.
    """
    timestamp = int(time.time() if timestamp is None else timestamp)
    row = database.execute(
        "SELECT token FROM sessions WHERE rowid=? AND user_id=? AND expires_at>?",
        (session_id, actor_id, timestamp),
    ).fetchone()
    if not row:
        return None
    database.execute("DELETE FROM sessions WHERE rowid=? AND user_id=?", (session_id, actor_id))
    return row["token"] == current_token


def change_password(database, actor_id, current_password, new_password, current_token):
    """Change the password and revoke every session except the caller's."""
    user = database.execute("SELECT password_hash FROM users WHERE id=?", (actor_id,)).fetchone()
    if not user or not password_matches(current_password, user["password_hash"]):
        return None
    database.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (password_hash(new_password), actor_id))
    revoked = database.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                               (actor_id, current_token)).rowcount
    return revoked
