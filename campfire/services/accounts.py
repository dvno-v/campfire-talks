"""Actor-authorized password, session, export, and deletion workflows."""

import time

from ..database import utc_now
from ..security import password_hash, password_matches

# Ownership cannot simply vanish: `communities.owner_id` is the only place the
# owner role is stored, so a departing owner hands the community to its most
# privileged remaining member. Ties go to the lowest account id, which is the
# oldest member, so the outcome is deterministic rather than whatever SQLite
# happened to return first.
SUCCESSOR_ORDER = "CASE m.role WHEN 'administrator' THEN 2 WHEN 'moderator' THEN 1 ELSE 0 END"


def list_sessions(database, actor_id, current_token, timestamp=None):
    """Return the actor's active sessions without collecting device metadata."""
    timestamp = int(time.time() if timestamp is None else timestamp)
    rows = database.execute("""
      SELECT id,created_at,expires_at,token=? is_current
      FROM sessions WHERE user_id=? AND expires_at>?
      ORDER BY is_current DESC,created_at DESC,id DESC
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
        "SELECT token FROM sessions WHERE id=? AND user_id=? AND expires_at>?",
        (session_id, actor_id, timestamp),
    ).fetchone()
    if not row:
        return None
    database.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, actor_id))
    return row["token"] == current_token


def change_password(database, actor_id, current_password, new_password, current_token,
                    replacement_token):
    """Change the password, revoke every other session, and reissue the caller's.

    The surviving session is given a new token rather than kept as it was. A
    password change is where someone acts on the suspicion that a credential
    leaked, and the cookie riding along with the old one is part of what may
    have leaked. The caller supplies the replacement because tokens belong to
    the layer that sets cookies.
    """
    user = database.execute("SELECT password_hash FROM users WHERE id=?", (actor_id,)).fetchone()
    if not user or not password_matches(current_password, user["password_hash"]):
        return None
    database.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (password_hash(new_password), actor_id))
    revoked = database.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                               (actor_id, current_token)).rowcount
    database.execute("UPDATE sessions SET token=? WHERE user_id=? AND token=?",
                     (replacement_token, actor_id, current_token))
    return revoked


def export_account(database, actor_id, exported_at=None):
    """Return everything stored about one account as JSON-ready data.

    The export is what the account can already see about itself, gathered in
    one place: no digests, no other members' messages, and no data that would
    let the file identify anyone the account does not already share a community
    with. It exists so leaving does not mean losing your own history.
    """
    account = database.execute(
        "SELECT id,username,created_at FROM users WHERE id=?", (actor_id,)).fetchone()
    if not account:
        return None

    def rows(query, parameters=(actor_id,)):
        return [dict(row) for row in database.execute(query, parameters).fetchall()]

    return {
        "format": "campfire.account-export.v1",
        "exported_at": exported_at or utc_now(),
        "account": dict(account),
        "sessions": rows("""SELECT created_at,expires_at FROM sessions
                            WHERE user_id=? ORDER BY created_at"""),
        "passkeys": rows("""SELECT name,created_at,last_used_at FROM passkeys
                            WHERE user_id=? ORDER BY created_at,id"""),
        "communities": rows("""
          SELECT c.id,c.name,c.created_at,
            CASE WHEN c.owner_id=m.user_id THEN 'owner' ELSE m.role END role
          FROM memberships m JOIN communities c ON c.id=m.community_id
          WHERE m.user_id=? ORDER BY c.id"""),
        "messages": rows("""
          SELECT m.id,m.body,m.created_at,m.edited_at,c.name community,ch.name channel,
            a.original_name attachment_name,a.mime_type attachment_type,a.byte_size attachment_bytes
          FROM messages m JOIN channels ch ON ch.id=m.channel_id
          JOIN communities c ON c.id=ch.community_id
          LEFT JOIN attachments a ON a.id=m.attachment_id
          WHERE m.author_id=? ORDER BY m.id"""),
        "attachments": rows("""
          SELECT a.id,a.original_name,a.mime_type,a.byte_size,a.created_at,ch.name channel
          FROM attachments a JOIN channels ch ON ch.id=a.channel_id
          WHERE a.uploader_id=? ORDER BY a.id"""),
        "read_markers": rows("""
          SELECT ch.name channel,c.name community,r.last_read_message_id
          FROM channel_reads r JOIN channels ch ON ch.id=r.channel_id
          JOIN communities c ON c.id=ch.community_id
          WHERE r.user_id=? ORDER BY r.channel_id"""),
        "active_voice_leases": rows("""
          SELECT c.name community,ch.name channel,v.key_fingerprint,v.created_at,v.expires_at
          FROM voice_leases v JOIN channels ch ON ch.id=v.channel_id
          JOIN communities c ON c.id=ch.community_id
          WHERE v.user_id=? ORDER BY v.channel_id"""),
        "notification_preferences": {
            "default_mode": (database.execute(
                "SELECT mode FROM notification_preferences WHERE user_id=?",
                (actor_id,)).fetchone() or {"mode": "all"})["mode"],
            "channels": rows("""
              SELECT ch.name channel,c.name community,n.mode
              FROM channel_notifications n JOIN channels ch ON ch.id=n.channel_id
              JOIN communities c ON c.id=ch.community_id
              WHERE n.user_id=? ORDER BY n.channel_id"""),
        },
        "invitations_created": rows("""
          SELECT i.id,c.name community,i.expires_at,i.max_uses,i.uses,i.created_at
          FROM invitations i JOIN communities c ON c.id=i.community_id
          WHERE i.created_by=? ORDER BY i.id"""),
        "bans_received": rows("""
          SELECT c.name community,b.role_at_ban,b.created_at
          FROM community_bans b JOIN communities c ON c.id=b.community_id
          WHERE b.user_id=? ORDER BY b.community_id"""),
    }


def deletion_plan(database, actor_id):
    """Describe what deleting this account would do, changing nothing.

    The client shows this before asking for confirmation: handing a community
    to someone else, or dissolving it outright, is not something to discover
    afterwards.
    """
    dissolved, transferred = [], []
    owned = database.execute(
        "SELECT id,name FROM communities WHERE owner_id=? ORDER BY id", (actor_id,)).fetchall()
    for community in owned:
        successor = _successor(database, community["id"], actor_id)
        if successor:
            transferred.append({"id": community["id"], "name": community["name"],
                                "successor_id": successor["id"],
                                "successor_username": successor["username"]})
        else:
            dissolved.append({"id": community["id"], "name": community["name"]})
    return {
        "communities_dissolved": dissolved,
        "communities_transferred": transferred,
        "messages": _count(database, "SELECT COUNT(*) FROM messages WHERE author_id=?", actor_id),
        "attachments": _count(database, "SELECT COUNT(*) FROM attachments WHERE uploader_id=?",
                              actor_id),
    }


def delete_account(database, actor_id, password):
    """Erase an account, its messages, and its images, or return why it cannot be.

    Returns ``(status, plan)``. On success the plan carries an extra
    ``orphaned_files`` list: the caller unlinks those once the transaction has
    committed, exactly as single-message deletion already does.

    Deletion is deliberately stronger here than a kick. A kick is a moderator
    acting on someone, so their words stay part of everyone else's
    conversation; this is the account itself withdrawing, and it takes the
    content it published with it.
    """
    # Take SQLite's writer lock before reading anything this depends on. A role
    # change landing between choosing a successor and handing over the community
    # would otherwise pick the wrong one, and a password change landing between
    # the check and the delete would be confirmed by the password it replaced.
    database.execute("UPDATE users SET username=username WHERE id=?", (actor_id,))
    user = database.execute("SELECT password_hash FROM users WHERE id=?", (actor_id,)).fetchone()
    if not user:
        return "account_not_found", None
    if not password_matches(password, user["password_hash"]):
        return "invalid_password", None

    plan = deletion_plan(database, actor_id)
    orphaned = []
    for community in plan["communities_transferred"]:
        database.execute("UPDATE communities SET owner_id=? WHERE id=?",
                         (community["successor_id"], community["id"]))
    for community in plan["communities_dissolved"]:
        orphaned += _dissolve_community(database, community["id"])
    orphaned += _erase_authored_content(database, actor_id)
    # Sessions, memberships, read markers, notification preferences, invites
    # created, and bans received all cascade from the account row; bans this
    # account issued keep their row with the moderator link set to NULL.
    database.execute("DELETE FROM users WHERE id=?", (actor_id,))
    return "ok", plan | {"orphaned_files": orphaned}


def _successor(database, community_id, actor_id):
    return database.execute(f"""
      SELECT u.id,u.username FROM memberships m JOIN users u ON u.id=m.user_id
      WHERE m.community_id=? AND m.user_id<>?
      ORDER BY {SUCCESSOR_ORDER} DESC,m.user_id LIMIT 1
    """, (community_id, actor_id)).fetchone()


def _dissolve_community(database, community_id):
    """Delete a community nobody else belongs to, returning its orphaned files.

    Messages and attachments are removed explicitly and in that order: both
    cascade from `channels`, and a cascade that reached the attachments first
    would break the `messages.attachment_id` reference still pointing at them.
    """
    channels = "SELECT id FROM channels WHERE community_id=?"
    orphaned = [row["storage_name"] for row in database.execute(
        f"SELECT storage_name FROM attachments WHERE channel_id IN ({channels})",
        (community_id,)).fetchall()]
    database.execute(f"DELETE FROM messages WHERE channel_id IN ({channels})", (community_id,))
    database.execute(f"DELETE FROM attachments WHERE channel_id IN ({channels})", (community_id,))
    database.execute("DELETE FROM communities WHERE id=?", (community_id,))
    return orphaned


def _erase_authored_content(database, actor_id):
    """Remove the account's messages and images from every community it reached.

    Messages the account left behind in a community that kicked it are erased
    too: the account is being deleted, not its membership.
    """
    attachments = database.execute("""
      SELECT id,storage_name FROM attachments WHERE uploader_id=?
      UNION
      SELECT a.id,a.storage_name FROM attachments a
      JOIN messages m ON m.attachment_id=a.id WHERE m.author_id=?
    """, (actor_id, actor_id)).fetchall()
    database.execute("DELETE FROM messages WHERE author_id=?", (actor_id,))
    if attachments:
        identifiers = [row["id"] for row in attachments]
        placeholders = ",".join("?" * len(identifiers))
        # Nothing should reference these once the author's messages are gone,
        # but clearing the link first keeps the delete from failing outright if
        # some future feature ever attaches one image to a second message.
        database.execute(
            f"UPDATE messages SET attachment_id=NULL WHERE attachment_id IN ({placeholders})",
            identifiers)
        database.execute(f"DELETE FROM attachments WHERE id IN ({placeholders})", identifiers)
    return [row["storage_name"] for row in attachments]


def _count(database, query, actor_id):
    return database.execute(query, (actor_id,)).fetchone()[0]
