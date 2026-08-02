"""Community membership queries and authorization."""


def list_community_members(database, community_id, actor_id):
    """Return public member data, or None when the actor is not a member."""
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
    return [dict(row) for row in rows]
