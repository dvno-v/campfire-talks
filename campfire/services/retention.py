"""Per-community retention: how long messages and shared images are kept.

Retention is expressed in whole days, and zero means "keep indefinitely", which
stays the default. A community that has never chosen is never quietly pruned.

Images can be given a shorter life than words. They are almost all of the disk
a small instance uses, and a photo shared in a channel ages differently from
the sentence next to it. Removing a shared image removes the message carrying
it, because that message *is* the image: Campfire stores an upload as a message
with an empty body, so keeping the husk would leave a row that says nothing.
"""

from datetime import datetime, timedelta, timezone

from .communities import ROLE_RANK, community_role

MAX_RETENTION_DAYS = 3650


def set_retention(database, community_id, actor_id, message_days, attachment_days):
    """Store a community's retention windows for an administrator or owner.

    Returns ``None`` when the actor may not manage the community and ``False``
    for values outside the accepted range.
    """
    role = community_role(database, community_id, actor_id)
    if role is None or ROLE_RANK[role] < ROLE_RANK["administrator"]:
        return None
    if not all(isinstance(days, int) and 0 <= days <= MAX_RETENTION_DAYS
               for days in (message_days, attachment_days)):
        return False
    database.execute("""UPDATE communities
                        SET message_retention_days=?,attachment_retention_days=? WHERE id=?""",
                     (message_days, attachment_days, community_id))
    return {"community_id": community_id, "message_days": message_days,
            "attachment_days": attachment_days}


def purge_expired(database, now=None):
    """Delete everything past its community's retention window.

    Returns ``(removed_per_channel, orphaned_files)``. The caller unlinks the
    files once the transaction has committed, exactly as single-message
    deletion already does, and tells the affected channels that their history
    moved underneath them.
    """
    moment = now or datetime.now(timezone.utc)
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    removed, orphaned = {}, []
    communities = database.execute("""
      SELECT id,message_retention_days,attachment_retention_days FROM communities
      WHERE message_retention_days>0 OR attachment_retention_days>0
    """).fetchall()
    for community in communities:
        expired = _expired_messages(
            database, community["id"],
            _threshold(moment, community["message_retention_days"]),
            _threshold(moment, community["attachment_retention_days"]))
        if not expired:
            continue
        message_ids = [row["id"] for row in expired]
        attachment_ids = [row["attachment_id"] for row in expired if row["attachment_id"]]
        orphaned += [row["storage_name"] for row in expired if row["storage_name"]]
        _delete(database, "messages", message_ids)
        _delete(database, "attachments", attachment_ids)
        for row in expired:
            key = (community["id"], row["channel_id"])
            removed[key] = removed.get(key, 0) + 1
    return removed, orphaned


def _threshold(moment, days):
    """The stored timestamp before which rows of this age have expired."""
    if not days:
        return None
    return (moment - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _expired_messages(database, community_id, message_before, attachment_before):
    """Rows to remove, computed in Python rather than SQL date arithmetic.

    Timestamps are stored as ISO 8601 with a literal `T` and `Z`, which does
    not compare correctly against SQLite's own `datetime()` output; passing a
    threshold in the stored format keeps the comparison a plain string one.
    """
    clauses, parameters = [], [community_id]
    if message_before:
        clauses.append("m.created_at<?")
        parameters.append(message_before)
    if attachment_before:
        clauses.append("(m.attachment_id IS NOT NULL AND m.created_at<?)")
        parameters.append(attachment_before)
    if not clauses:
        return []
    return database.execute(f"""
      SELECT m.id,m.channel_id,m.attachment_id,a.storage_name
      FROM messages m JOIN channels ch ON ch.id=m.channel_id
      LEFT JOIN attachments a ON a.id=m.attachment_id
      WHERE ch.community_id=? AND ({' OR '.join(clauses)})
    """, parameters).fetchall()


def _delete(database, table, identifiers):
    """Messages go before the attachments they point at, so the reference from
    `messages.attachment_id` is never left dangling mid-transaction."""
    if identifiers:
        placeholders = ",".join("?" * len(identifiers))
        database.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", identifiers)
