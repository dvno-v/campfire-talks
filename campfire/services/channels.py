"""Per-channel posting rules: who may write, how often, and with what.

These are community controls rather than privacy controls. Every member of a
community can still read every one of its channels; what a channel can restrict
is contributing to it. Read restrictions would have to be enforced in message
listing, live delivery, unread counts and notification state at once, and a rule
enforced in three places out of four is worse than no rule at all, so it is
deliberately left to a later milestone.
"""

from datetime import datetime

from ..database import utc_now
from .communities import ROLE_RANK

POSTING_ROLES = frozenset({"administrator", "moderator", "member"})
# Five minutes is the longest wait worth imposing on a friend group; past that
# the control stops damping a flood and starts ending the conversation.
MAX_SLOW_MODE_SECONDS = 300
# Slow mode damps a flood from the ordinary membership. Moderators are the
# people expected to answer one, so it does not apply to them.
SLOW_MODE_EXEMPT = "moderator"


def channel_context(database, channel_id, actor_id):
    """Return a channel's posting rules alongside the actor's role, or None.

    ``None`` covers a channel that does not exist and one in a community the
    actor does not belong to, which the HTTP layer answers identically.
    """
    return database.execute("""
      SELECT ch.id,ch.name,ch.community_id,ch.post_min_role,ch.slow_mode_seconds,
             ch.uploads_allowed,
             CASE WHEN c.owner_id=m.user_id THEN 'owner' ELSE m.role END role
      FROM channels ch
      JOIN communities c ON c.id=ch.community_id
      JOIN memberships m ON m.community_id=ch.community_id AND m.user_id=?
      WHERE ch.id=?
    """, (actor_id, channel_id)).fetchone()


def settings_payload(row):
    """The posting rules a client needs to render one channel's composer."""
    return {"post_min_role": row["post_min_role"],
            "slow_mode_seconds": row["slow_mode_seconds"],
            "uploads_allowed": bool(row["uploads_allowed"])}


def may_post(database, context, actor_id, uploading=False, now=None):
    """Decide whether the actor may contribute to this channel right now.

    Returns ``(status, detail)``. ``detail`` carries the required role for
    ``"role"`` and the whole seconds still to wait for ``"slow_mode"``, so the
    HTTP layer can say what is wrong rather than only that something is.
    """
    if ROLE_RANK[context["role"]] < ROLE_RANK[context["post_min_role"]]:
        return "role", context["post_min_role"]
    if uploading and not context["uploads_allowed"]:
        return "uploads_disabled", None
    remaining = slow_mode_remaining(database, context, actor_id, now)
    if remaining:
        return "slow_mode", remaining
    return "ok", None


def slow_mode_remaining(database, context, actor_id, now=None):
    """Whole seconds before the actor may post again, or 0 when they may now."""
    seconds = context["slow_mode_seconds"]
    if not seconds or ROLE_RANK[context["role"]] >= ROLE_RANK[SLOW_MODE_EXEMPT]:
        return 0
    moment = _moment(now or utc_now())
    latest = database.execute(
        "SELECT MAX(created_at) last FROM messages WHERE channel_id=? AND author_id=?",
        (context["id"], actor_id)).fetchone()["last"]
    if not latest:
        return 0
    return max(0, seconds - int((moment - _moment(latest)).total_seconds()))


def _moment(stamp):
    """Timestamps are stored as ISO 8601 with a literal Z, which fromisoformat
    accepts only as an explicit offset before Python 3.11."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def update_settings(database, channel_id, actor_id, post_min_role, slow_mode_seconds,
                    uploads_allowed):
    """Rewrite a channel's posting rules for a community administrator.

    Returns ``None`` when the actor may not manage the channel and ``False``
    for values the schema would refuse, so the caller can tell "not allowed"
    from "not valid".
    """
    context = channel_context(database, channel_id, actor_id)
    if not context:
        return None
    if ROLE_RANK[context["role"]] < ROLE_RANK["administrator"]:
        return None
    if post_min_role not in POSTING_ROLES:
        return False
    if not 0 <= slow_mode_seconds <= MAX_SLOW_MODE_SECONDS:
        return False
    database.execute("""UPDATE channels
                        SET post_min_role=?,slow_mode_seconds=?,uploads_allowed=? WHERE id=?""",
                     (post_min_role, slow_mode_seconds, int(bool(uploads_allowed)), channel_id))
    return {"id": channel_id, "community_id": context["community_id"], "name": context["name"],
            "post_min_role": post_min_role, "slow_mode_seconds": slow_mode_seconds,
            "uploads_allowed": bool(uploads_allowed)}
