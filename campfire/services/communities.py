"""Community membership and owner-authorized management queries."""

import time


def list_community_members(database, community_id, actor_id, online_ids=frozenset()):
    """Return public member data, or None when the actor is not a member.

    Presence lives in memory, so the caller supplies the currently connected
    user IDs rather than this query reading them from storage.
    """
    allowed = database.execute(
        "SELECT 1 FROM memberships WHERE community_id=? AND user_id=?",
        (community_id, actor_id),
    ).fetchone()
    if not allowed:
        return None

    rows = database.execute("""
      SELECT u.id,u.username,
        CASE WHEN c.owner_id=u.id THEN 'owner' ELSE 'member' END role
      FROM memberships m
      JOIN users u ON u.id=m.user_id
      JOIN communities c ON c.id=m.community_id
      WHERE m.community_id=?
      ORDER BY CASE WHEN c.owner_id=u.id THEN 0 ELSE 1 END, u.username COLLATE NOCASE
    """, (community_id,)).fetchall()
    return [dict(row) | {"online": row["id"] in online_ids} for row in rows]


def shares_community(database, user_id, other_id):
    """True when two accounts have at least one community in common.

    Presence is only disclosed across an existing shared membership, so being
    connected is never observable by strangers.
    """
    return database.execute("""
      SELECT 1 FROM memberships mine
      JOIN memberships theirs ON theirs.community_id=mine.community_id
      WHERE mine.user_id=? AND theirs.user_id=? LIMIT 1
    """, (user_id, other_id)).fetchone() is not None


def list_active_invites(database, community_id, actor_id, timestamp=None):
    """Return active invite metadata to the owner, or None to other actors."""
    owns = database.execute(
        "SELECT 1 FROM communities WHERE id=? AND owner_id=?",
        (community_id, actor_id),
    ).fetchone()
    if not owns:
        return None

    timestamp = int(timestamp or time.time())
    rows = database.execute("""
      SELECT i.id,i.expires_at,i.max_uses,i.uses,i.created_at,
        u.id creator_id,u.username creator_username
      FROM invitations i
      JOIN users u ON u.id=i.created_by
      WHERE i.community_id=? AND i.expires_at>? AND i.uses<i.max_uses
      ORDER BY i.id DESC
    """, (community_id, timestamp)).fetchall()
    return [dict(row) for row in rows]


def revoke_invite(database, invite_id, actor_id):
    """Delete an invite only when the actor owns its community."""
    cursor = database.execute("""
      DELETE FROM invitations
      WHERE id=? AND community_id IN (
        SELECT id FROM communities WHERE owner_id=?
      )
    """, (invite_id, actor_id))
    return cursor.rowcount == 1
