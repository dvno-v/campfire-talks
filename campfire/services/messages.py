"""Message ownership rules for editing and deletion.

Two distinct rights govern an existing message:

- Authorship allows editing. Nobody may rewrite words attributed to another
  person, so this right is never delegated, not even to a community owner.
- Authorship *or* community ownership allows deletion, because removing
  content is the moderation action an owner needs.

Both require current membership of the message's community. Callers who are not
members cannot distinguish a message they may not touch from one that does not
exist.
"""


def visible_message(database, message_id, actor_id):
    """Return the message plus the actor's rights over it, or None if unreachable."""
    return database.execute("""
      SELECT m.id,m.channel_id,m.body,m.created_at,m.edited_at,m.attachment_id,
             m.author_id,u.username,
             a.original_name,a.mime_type,a.byte_size,a.storage_name,
             m.author_id=? AS is_author,
             c.owner_id=? AS is_community_owner
      FROM messages m
      JOIN users u ON u.id=m.author_id
      JOIN channels ch ON ch.id=m.channel_id
      JOIN communities c ON c.id=ch.community_id
      JOIN memberships mem ON mem.community_id=c.id AND mem.user_id=?
      LEFT JOIN attachments a ON a.id=m.attachment_id
      WHERE m.id=?
    """, (actor_id, actor_id, actor_id, message_id)).fetchone()


def may_edit(message):
    return bool(message["is_author"])


def may_delete(message):
    return bool(message["is_author"] or message["is_community_owner"])


def apply_edit(database, message_id, body, edited_at):
    """Rewrite a message body, recording that it no longer reads as sent."""
    database.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?", (body, edited_at, message_id))


def remove_message(database, message):
    """Delete a message and any attachment it owns, returning the orphaned file name.

    The attachment row is removed with its message so deletion does not leave
    retrievable content behind; the caller unlinks the file once the
    transaction has committed.
    """
    database.execute("DELETE FROM messages WHERE id=?", (message["id"],))
    if message["attachment_id"] is not None:
        database.execute("DELETE FROM attachments WHERE id=?", (message["attachment_id"],))
        return message["storage_name"]
    return None
