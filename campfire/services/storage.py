"""Shared-image disk accounting and the instance storage ceiling.

A self-hosted instance runs on somebody's actual disk, and the first sign of
that disk filling should not be the database failing to write. The quota is
counted from the attachment rows rather than by walking the directory: those
rows are what authorization and deletion already work from, so the number the
limit is enforced against is the same number the interface reports.

The bytes actually on disk are reported alongside it. They should match, and
saying both is how an operator finds out when they do not.
"""

import os
import shutil


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


def capacity_warnings(database, limit_bytes, upload_dir, database_path,
                      warning_percent=90):
    """Return operator-facing warnings before configured or physical space runs out.

    Only warning codes and percentages are returned. Filesystem sizes can
    describe unrelated data on a shared host, so Campfire does not disclose
    them through an HTTP endpoint.
    """
    warnings = []
    used = tracked_bytes(database)[0]
    if limit_bytes and used * 100 >= limit_bytes * warning_percent:
        percent = min(100, (used * 100) // limit_bytes)
        warnings.append({
            "code": "image_storage_limit",
            "percent": percent,
            "message": f"Campfire's image storage limit is {percent}% used.",
        })

    checked_devices = set()
    filesystem_warned = False
    for path in (database_path, upload_dir):
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            device = existing.stat().st_dev
            if device in checked_devices:
                continue
            checked_devices.add(device)
            disk = shutil.disk_usage(existing)
        except OSError:
            continue
        if (not filesystem_warned and disk.total
                and disk.used * 100 >= disk.total * warning_percent):
            percent = min(100, (disk.used * 100) // disk.total)
            warnings.append({
                "code": "filesystem_capacity",
                "percent": percent,
                "message": f"A filesystem used by Campfire is {percent}% full.",
            })
            filesystem_warned = True
    return warnings


def filesystems_have_space(*paths):
    """Whether every distinct filesystem behind these paths has usable space."""
    checked_devices = set()
    for path in paths:
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            device = existing.stat().st_dev
            if device in checked_devices:
                continue
            checked_devices.add(device)
            if shutil.disk_usage(existing).free <= 0:
                return False
        except OSError:
            return False
    return True


def writable_location(path, directory=False):
    """Whether an existing path, or the closest parent of a new one, is writable."""
    if path.exists():
        if directory and not path.is_dir():
            return False
        if not directory and not path.is_file():
            return False
        candidate = path
    else:
        candidate = path.parent
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        if not candidate.is_dir():
            return False
    try:
        mode = candidate.stat().st_mode
    except OSError:
        return False
    # Checking permission bits keeps this useful when the process is tested as
    # root, where access(2) can otherwise mask a bad read-only configuration.
    required = os.W_OK | (os.X_OK if candidate.is_dir() else 0)
    return bool(mode & 0o222) and os.access(candidate, required)
