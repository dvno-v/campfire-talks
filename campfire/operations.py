"""Consistent, verifiable backup and offline restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import __version__
from .database import schema_version, utc_now
from .instance_lock import operation_lock
from .migrations import LATEST_SCHEMA_VERSION

BACKUP_FORMAT = "campfire.backup.v1"
DATABASE_NAME = "campfire.db"
UPLOADS_NAME = "uploads"
MANIFEST_NAME = "manifest.json"


class BackupError(RuntimeError):
    """A backup or restore could not be completed safely."""


def _safe_storage_name(name):
    return (isinstance(name, str) and name not in {"", ".", ".."}
            and Path(name).name == name and len(name.encode()) <= 255)


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_private(source, destination):
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    os.chmod(destination, 0o600)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_source(database_path):
    if not database_path.is_file() or database_path.is_symlink():
        raise BackupError(f"Database does not exist as a regular file: {database_path}")
    database = sqlite3.connect(database_path, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return database


def create_backup(destination, database_path, upload_dir):
    """Create an atomic directory snapshot of SQLite and every referenced file."""
    destination = Path(destination).resolve()
    database_path = Path(database_path).resolve()
    upload_dir = Path(upload_dir).resolve()
    if destination.exists():
        raise BackupError(f"Backup destination already exists: {destination}")
    if destination == upload_dir or upload_dir in destination.parents:
        raise BackupError("Backup destination cannot be inside the live upload directory")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with operation_lock(database_path, exclusive=False):
        temporary = Path(tempfile.mkdtemp(
            prefix=f".{destination.name}.incomplete-", dir=destination.parent))
        source = None
        try:
            uploads_target = temporary / UPLOADS_NAME
            uploads_target.mkdir(mode=0o700)
            source = _open_source(database_path)
            source.execute("BEGIN IMMEDIATE")
            version = schema_version(source)
            if not 1 <= version <= LATEST_SCHEMA_VERSION:
                raise BackupError("Database must be migrated before it can be backed up")
            attachments = source.execute(
                "SELECT storage_name,byte_size FROM attachments ORDER BY storage_name").fetchall()

            # A separate reader sees the same latest committed state while the
            # IMMEDIATE transaction prevents any writer from moving it forward.
            snapshot_source = sqlite3.connect(database_path, timeout=10)
            snapshot_target = sqlite3.connect(temporary / DATABASE_NAME)
            try:
                snapshot_source.backup(snapshot_target)
                # A backup is one standalone database, never a WAL triplet.
                snapshot_target.execute("PRAGMA journal_mode = DELETE")
            finally:
                snapshot_target.close()
                snapshot_source.close()
            os.chmod(temporary / DATABASE_NAME, 0o600)

            files = []
            for attachment in attachments:
                name = attachment["storage_name"]
                if not _safe_storage_name(name):
                    raise BackupError("Database contains an unsafe attachment storage name")
                stored = upload_dir / name
                if not stored.is_file() or stored.is_symlink():
                    raise BackupError(f"Referenced upload is missing or unsafe: {name}")
                target = uploads_target / name
                _copy_private(stored, target)
                actual_size = target.stat().st_size
                if actual_size != attachment["byte_size"]:
                    raise BackupError(f"Referenced upload has the wrong size: {name}")
                files.append({"name": name, "size": actual_size,
                              "sha256": _hash_file(target)})
            source.commit()
            source.close()
            source = None

            database_copy = temporary / DATABASE_NAME
            manifest = {
                "format": BACKUP_FORMAT,
                "created_at": utc_now(),
                "campfire_version": __version__,
                "schema_version": version,
                "database": {"size": database_copy.stat().st_size,
                             "sha256": _hash_file(database_copy)},
                "attachments": files,
            }
            manifest_path = temporary / MANIFEST_NAME
            with manifest_path.open("x", encoding="utf-8") as output:
                json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(manifest_path, 0o600)
            _fsync_directory(uploads_target)
            _fsync_directory(temporary)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
            return manifest
        except Exception:
            if source is not None:
                source.rollback()
                source.close()
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def verify_backup(source):
    """Validate manifest, hashes, SQLite integrity, schema, and attachment set."""
    source = Path(source).resolve()
    manifest_path = source / MANIFEST_NAME
    try:
        if manifest_path.stat().st_size > 16 * 1024 * 1024:
            raise BackupError("Backup manifest is unreasonably large")
        with manifest_path.open(encoding="utf-8") as input_file:
            manifest = json.load(input_file)
    except (OSError, ValueError, TypeError) as failure:
        raise BackupError("Backup manifest is missing or invalid") from failure
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise BackupError("Unsupported backup format")
    try:
        if {entry.name for entry in source.iterdir()} != {
                MANIFEST_NAME, DATABASE_NAME, UPLOADS_NAME}:
            raise BackupError("Backup directory contains unexpected entries")
    except OSError as failure:
        raise BackupError("Backup directory is unreadable") from failure
    version = manifest.get("schema_version")
    if not isinstance(version, int) or not 1 <= version <= LATEST_SCHEMA_VERSION:
        raise BackupError("Backup schema is unsupported by this Campfire release")

    database_path = source / DATABASE_NAME
    database_record = manifest.get("database")
    if (not isinstance(database_record, dict) or not database_path.is_file()
            or database_path.is_symlink()
            or database_record.get("size") != database_path.stat().st_size
            or database_record.get("sha256") != _hash_file(database_path)):
        raise BackupError("Backup database hash or size does not match its manifest")

    records = manifest.get("attachments")
    if not isinstance(records, list):
        raise BackupError("Backup attachment manifest is invalid")
    expected = {}
    for record in records:
        if (not isinstance(record, dict) or not _safe_storage_name(record.get("name"))
                or not isinstance(record.get("size"), int) or record["size"] < 0
                or not isinstance(record.get("sha256"), str)
                or len(record["sha256"]) != 64 or record["name"] in expected):
            raise BackupError("Backup attachment manifest is invalid")
        expected[record["name"]] = record

    uploads = source / UPLOADS_NAME
    try:
        actual_names = {path.name for path in uploads.iterdir()
                        if path.is_file() and not path.is_symlink()}
        all_entries = {path.name for path in uploads.iterdir()}
    except OSError as failure:
        raise BackupError("Backup upload directory is missing or unreadable") from failure
    if actual_names != set(expected) or all_entries != actual_names:
        raise BackupError("Backup upload set does not match its manifest")
    for name, record in expected.items():
        stored = uploads / name
        if stored.stat().st_size != record["size"] or _hash_file(stored) != record["sha256"]:
            raise BackupError(f"Backup upload hash or size does not match: {name}")

    database = sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    database.row_factory = sqlite3.Row
    try:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("Backup database failed SQLite integrity_check")
        if schema_version(database) != version:
            raise BackupError("Backup schema version does not match its manifest")
        rows = database.execute(
            "SELECT storage_name,byte_size FROM attachments ORDER BY storage_name").fetchall()
    except sqlite3.Error as failure:
        raise BackupError("Backup database cannot be read") from failure
    finally:
        database.close()
    database_files = {row["storage_name"]: row["byte_size"] for row in rows}
    manifest_files = {name: record["size"] for name, record in expected.items()}
    if database_files != manifest_files:
        raise BackupError("Backup uploads do not match the database attachment rows")
    return manifest


def restore_backup(source, database_path, upload_dir):
    """Validate and install a backup offline, rolling back any runtime failure."""
    source = Path(source).resolve()
    database_path = Path(database_path).resolve()
    upload_dir = Path(upload_dir).resolve()
    with operation_lock(database_path, exclusive=True, blocking=False):
        manifest = verify_backup(source)
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        upload_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_hex(8)
        staged_database = database_path.parent / f".{database_path.name}.restore-{token}"
        staged_uploads = upload_dir.parent / f".{upload_dir.name}.restore-{token}"
        old_database = database_path.parent / f".{database_path.name}.previous-{token}"
        old_uploads = upload_dir.parent / f".{upload_dir.name}.previous-{token}"
        sidecars = [database_path.with_name(database_path.name + suffix)
                    for suffix in ("-wal", "-shm")]
        old_sidecars = [path.with_name(f".{path.name}.previous-{token}") for path in sidecars]
        installed_database = installed_uploads = False
        moved_database = moved_uploads = False
        moved_sidecars = []
        try:
            _copy_private(source / DATABASE_NAME, staged_database)
            staged_uploads.mkdir(mode=0o700)
            for record in manifest["attachments"]:
                _copy_private(source / UPLOADS_NAME / record["name"],
                              staged_uploads / record["name"])
            _fsync_directory(staged_uploads)

            if upload_dir.exists():
                if not upload_dir.is_dir() or upload_dir.is_symlink():
                    raise BackupError("Configured upload path is not a safe directory")
                os.replace(upload_dir, old_uploads)
                moved_uploads = True
            if database_path.exists():
                if not database_path.is_file() or database_path.is_symlink():
                    raise BackupError("Configured database path is not a safe file")
                os.replace(database_path, old_database)
                moved_database = True
            for current, previous in zip(sidecars, old_sidecars):
                if current.exists():
                    os.replace(current, previous)
                    moved_sidecars.append((current, previous))

            os.replace(staged_uploads, upload_dir)
            installed_uploads = True
            os.replace(staged_database, database_path)
            installed_database = True
            verify_database = sqlite3.connect(database_path)
            try:
                if verify_database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("Restored database failed its final integrity check")
            finally:
                verify_database.close()
            _fsync_directory(database_path.parent)
            if upload_dir.parent != database_path.parent:
                _fsync_directory(upload_dir.parent)
        except Exception:
            if installed_database:
                database_path.unlink(missing_ok=True)
            if installed_uploads:
                shutil.rmtree(upload_dir, ignore_errors=True)
            if moved_database:
                os.replace(old_database, database_path)
            if moved_uploads:
                os.replace(old_uploads, upload_dir)
            for current, previous in moved_sidecars:
                os.replace(previous, current)
            staged_database.unlink(missing_ok=True)
            shutil.rmtree(staged_uploads, ignore_errors=True)
            raise
        else:
            old_database.unlink(missing_ok=True)
            shutil.rmtree(old_uploads, ignore_errors=True)
            for _, previous in moved_sidecars:
                previous.unlink(missing_ok=True)
            return manifest
