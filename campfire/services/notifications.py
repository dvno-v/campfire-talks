"""Per-account read markers and the notification choices layered on them.

Two related ideas are kept apart on purpose:

- A *read marker* is the highest message id an account has seen in a channel.
  That id is the only thing stored. No timestamp is recorded, because when
  somebody last opened a channel is activity history that an unread badge does
  not need in order to work.
- A *notification mode* says whether that account wants to be told about new
  messages. `all` announces them, `none` mutes them. One row per account sets
  the default; a per-channel row overrides it for a single channel.

Muting changes what is announced, never what is counted. A muted channel still
tracks its unread messages, so the marker stays truthful and the client can
show the channel as having activity without interrupting anyone.

Unread counts ignore the reader's own messages: sending something is not a way
of missing it.

Every function here authorizes the actor itself and returns None or False for a
channel the actor cannot reach, so a non-member cannot tell an unreadable
channel apart from one that does not exist. Choosing the status code that
expresses that is the HTTP layer's job.
"""

NOTIFICATION_MODES = frozenset({"all", "none"})
DEFAULT_NOTIFICATION_MODE = "all"

# One row per readable channel: the unread total, where the reader got to, and
# the per-channel override (NULL when the account default applies).
_STATE_QUERY = """
  SELECT ch.id channel_id, ch.community_id,
         COALESCE(r.last_read_message_id, 0) last_read_message_id,
         n.mode notify,
         COUNT(m.id) unread
  FROM channels ch
  JOIN memberships mem ON mem.community_id=ch.community_id AND mem.user_id=?
  LEFT JOIN channel_reads r ON r.channel_id=ch.id AND r.user_id=?
  LEFT JOIN channel_notifications n ON n.channel_id=ch.id AND n.user_id=?
  LEFT JOIN messages m ON m.channel_id=ch.id
       AND m.id>COALESCE(r.last_read_message_id, 0) AND m.author_id<>?
  WHERE (? IS NULL OR ch.id=?)
  GROUP BY ch.id
"""


def readable_channel(database, channel_id, user_id):
    """True when the channel belongs to a community the actor is currently in."""
    return database.execute("""
      SELECT 1 FROM channels ch
      JOIN memberships m ON m.community_id=ch.community_id AND m.user_id=?
      WHERE ch.id=?
    """, (user_id, channel_id)).fetchone() is not None


def channel_states(database, user_id, channel_id=None):
    """Return {channel_id: state} for every channel the actor may read.

    Passing a channel narrows the result to that one channel, which is what the
    write paths return so a caller never has to re-read everything.
    """
    rows = database.execute(
        _STATE_QUERY, (user_id, user_id, user_id, user_id, channel_id, channel_id)).fetchall()
    return {row["channel_id"]: dict(row) for row in rows}


def mark_read(database, channel_id, user_id, message_id):
    """Advance the read marker, never rewind it.

    Two tabs report their own positions independently, and a client may send a
    stale id after a reconnect; letting either move the marker backwards would
    make already-read messages unread again. The requested id is also clamped
    to the newest message that actually exists, so a client cannot claim to
    have read into the future and silence messages it has never seen.
    """
    if not readable_channel(database, channel_id, user_id):
        return None
    newest = database.execute(
        "SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()[0]
    marker = max(0, min(int(message_id), newest))
    database.execute("""
      INSERT INTO channel_reads(user_id,channel_id,last_read_message_id) VALUES(?,?,?)
      ON CONFLICT(user_id,channel_id) DO UPDATE
        SET last_read_message_id=MAX(last_read_message_id, excluded.last_read_message_id)
    """, (user_id, channel_id, marker))
    return channel_states(database, user_id, channel_id).get(channel_id)


def mark_community_read(database, community_id, user_id):
    """Start a new member at the end of the history they have just been given.

    Somebody who joins today did not miss the conversation that happened before
    they were invited, and a badge counting it would be noise rather than
    information. The history stays readable; only the badge is settled. An
    existing marker is left alone so rejoining cannot erase a real position.
    """
    database.execute("""
      INSERT INTO channel_reads(user_id,channel_id,last_read_message_id)
      SELECT ?, ch.id, COALESCE((SELECT MAX(m.id) FROM messages m WHERE m.channel_id=ch.id), 0)
      FROM channels ch WHERE ch.community_id=?
      ON CONFLICT(user_id,channel_id) DO NOTHING
    """, (user_id, community_id))


def account_mode(database, user_id):
    """The account-wide notification mode, defaulting to `all` when unset."""
    row = database.execute(
        "SELECT mode FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
    return row["mode"] if row else DEFAULT_NOTIFICATION_MODE


def set_account_mode(database, user_id, mode):
    """Store the account-wide default. The caller has already validated `mode`."""
    database.execute("""
      INSERT INTO notification_preferences(user_id,mode) VALUES(?,?)
      ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode
    """, (user_id, mode))


def set_channel_mode(database, channel_id, user_id, mode):
    """Override the default for one channel, or clear the override with None.

    Returns False only when the actor cannot reach the channel; `mode` is
    validated by the caller.
    """
    if not readable_channel(database, channel_id, user_id):
        return False
    if mode is None:
        database.execute("DELETE FROM channel_notifications WHERE user_id=? AND channel_id=?",
                         (user_id, channel_id))
    else:
        database.execute("""
          INSERT INTO channel_notifications(user_id,channel_id,mode) VALUES(?,?,?)
          ON CONFLICT(user_id,channel_id) DO UPDATE SET mode=excluded.mode
        """, (user_id, channel_id, mode))
    return True
