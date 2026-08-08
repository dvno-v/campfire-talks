"""Community membership, roles, and actor-authorized management queries."""

import time


ASSIGNABLE_ROLES = frozenset({"administrator", "moderator", "member"})
ROLE_RANK = {"member": 0, "moderator": 1, "administrator": 2, "owner": 3}


def is_member(database, community_id, actor_id):
    """True when the actor currently belongs to the community."""
    return database.execute(
        "SELECT 1 FROM memberships WHERE community_id=? AND user_id=?",
        (community_id, actor_id),
    ).fetchone() is not None


def community_role(database, community_id, actor_id):
    """Return the actor's effective role, deriving ownership from the community."""
    row = database.execute("""
      SELECT CASE WHEN c.owner_id=m.user_id THEN 'owner' ELSE m.role END role
      FROM memberships m JOIN communities c ON c.id=m.community_id
      WHERE m.community_id=? AND m.user_id=?
    """, (community_id, actor_id)).fetchone()
    return row["role"] if row else None


def has_role(database, community_id, actor_id, minimum):
    """True when the actor is a member at or above the requested privilege level."""
    role = community_role(database, community_id, actor_id)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[minimum]


def list_community_members(database, community_id, actor_id, online_ids=frozenset()):
    """Return public member data, or None when the actor is not a member.

    Presence lives in memory, so the caller supplies the currently connected
    user IDs rather than this query reading them from storage.
    """
    if not is_member(database, community_id, actor_id):
        return None

    rows = database.execute("""
      SELECT u.id,u.username,
        CASE WHEN c.owner_id=u.id THEN 'owner' ELSE m.role END role
      FROM memberships m
      JOIN users u ON u.id=m.user_id
      JOIN communities c ON c.id=m.community_id
      WHERE m.community_id=?
      ORDER BY CASE WHEN c.owner_id=u.id THEN 0
                    WHEN m.role='administrator' THEN 1
                    WHEN m.role='moderator' THEN 2 ELSE 3 END,
               u.username COLLATE NOCASE
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


def set_member_role(database, community_id, member_id, actor_id, role):
    """Assign a non-owner member's role when requested by the community owner.

    The effective owner role is derived from ``communities.owner_id`` and
    cannot be changed through this operation.
    """
    if role not in ASSIGNABLE_ROLES:
        return False
    if community_role(database, community_id, actor_id) != "owner":
        return None
    owner = database.execute("SELECT owner_id FROM communities WHERE id=?", (community_id,)).fetchone()
    if not owner or owner["owner_id"] == member_id:
        return False
    updated = database.execute(
        "UPDATE memberships SET role=? WHERE community_id=? AND user_id=?",
        (role, community_id, member_id),
    )
    if updated.rowcount != 1:
        return False
    member = database.execute("SELECT id,username FROM users WHERE id=?", (member_id,)).fetchone()
    return dict(member) | {"role": role}


def list_active_invites(database, community_id, actor_id, timestamp=None):
    """Return active invite metadata to an administrator, or None otherwise."""
    if not has_role(database, community_id, actor_id, "administrator"):
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
    """Delete an invite only when the actor administers its community."""
    cursor = database.execute("""
      DELETE FROM invitations
      WHERE id=? AND community_id IN
        (SELECT community_id FROM memberships WHERE user_id=?
         AND (role='administrator' OR community_id IN
           (SELECT id FROM communities WHERE owner_id=?)))
    """, (invite_id, actor_id, actor_id))
    return cursor.rowcount == 1
