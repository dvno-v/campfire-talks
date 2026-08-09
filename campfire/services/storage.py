"""Shared-image disk accounting and the instance storage ceiling.

A self-hosted instance runs on somebody's actual disk, and the first sign of
that disk filling should not be the database failing to write. The quota is
counted from the attachment rows rather than by walking the directory: those
rows are what authorization and deletion already work from, so the number the
limit is enforced against is the same number the interface reports.

The bytes actually on disk are reported alongside it. They should match, and
saying both is how an operator finds out when they do not.
"""


def tracked_bytes(database):
    """Total size and count of the images Campfire knows it is storing."""
    row = database.execute(
        "SELECT COALESCE(SUM(byte_size),0) bytes,COUNT(*) files FROM attachments").fetchone()
    return row["bytes"], row["files"]


def exceeds_limit(database, limit_bytes, incoming_bytes):
    """True when accepting this upload would put the instance over its ceiling.

    A limit of zero means no ceiling, which is the default: imposing one on an
    instance that never asked for it would start refusing uploads that used to
    work.
    """
    if not limit_bytes:
        return False
    return tracked_bytes(database)[0] + incoming_bytes > limit_bytes


def usage(database, actor_id, limit_bytes, stored_bytes=None):
    """Storage totals, plus a breakdown of the communities the actor administers.

    Returns ``None`` for an account that administers nothing. Disk usage is an
    operator's concern, and an ordinary member learning how much everyone has
    shared is not something the feature needs in order to work.
    """
    communities = database.execute("""
      SELECT c.id,c.name,COALESCE(SUM(a.byte_size),0) bytes,COUNT(a.id) files
      FROM communities c
      JOIN memberships m ON m.community_id=c.id AND m.user_id=?
      LEFT JOIN channels ch ON ch.community_id=c.id
      LEFT JOIN attachments a ON a.channel_id=ch.id
      WHERE c.owner_id=? OR m.role='administrator'
      GROUP BY c.id ORDER BY bytes DESC,c.id
    """, (actor_id, actor_id)).fetchall()
    if not communities:
        return None
    used, files = tracked_bytes(database)
    return {
        "used_bytes": used,
        "files": files,
        "limit_bytes": limit_bytes,
        "available_bytes": max(0, limit_bytes - used) if limit_bytes else None,
        "stored_bytes": stored_bytes,
        "communities": [dict(row) for row in communities],
    }


def directory_bytes(upload_dir):
    """Bytes actually occupied by the upload directory, or None if unreadable.

    A directory that does not exist yet holds nothing, which is a real answer;
    None is reserved for a directory that exists and could not be read, because
    that is the case an operator needs to notice.
    """
    if not upload_dir.exists():
        return 0
    try:
        return sum(path.stat().st_size for path in upload_dir.iterdir() if path.is_file())
    except OSError:
        return None
