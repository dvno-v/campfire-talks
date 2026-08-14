"""Consistent, verifiable backup and offline restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import __version__
from .database import schema_version, utc_now
from .instance_lock import operation_lock
from .migrations import LATEST_SCHEMA_VERSION

BACKUP_FORMAT = "campfire.backup.v1"
DATABASE_NAME = "campfire.db"
UPLOADS_NAME = "uploads"
MANIFEST_NAME = "manifest.json"
ENCRYPTED_BACKUP_MAGIC = b"CAMPFIRE-ENCRYPTED-BACKUP-v1\n"
BACKUP_KEY_BYTES = 32
GCM_NONCE_BYTES = 12
GCM_TAG_BYTES = 16


class BackupError(RuntimeError):
    """A backup or restore could not be completed safely."""


def _resolve_without_symlink(value, label):
    path = Path(value)
    if path.is_symlink():
        raise BackupError(f"{label} cannot be a symbolic link")
    return path.resolve()


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


def _read_backup_key(key_file):
    key_file = _resolve_without_symlink(key_file, "Backup key")
    if not key_file.is_file():
        raise BackupError("Backup key must be a regular file")
    try:
        if key_file.stat().st_size != BACKUP_KEY_BYTES:
            raise BackupError("Backup key must contain exactly 32 random bytes")
        key = key_file.read_bytes()
    except OSError as failure:
        raise BackupError("Backup key cannot be read") from failure
    if len(key) != BACKUP_KEY_BYTES:
        raise BackupError("Backup key must contain exactly 32 random bytes")
    return key


class _EncryptingWriter:
    """Minimal streaming file interface consumed by tarfile's pipe mode."""

    def __init__(self, destination, encryptor):
        self.destination = destination
        self.encryptor = encryptor

    def write(self, content):
        encrypted = self.encryptor.update(content)
        if encrypted:
            self.destination.write(encrypted)
        return len(content)

    def flush(self):
        self.destination.flush()


def _private_tar_metadata(member):
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = 0
    member.mode = 0o700 if member.isdir() else 0o600
    return member


def _encrypt_backup_directory(source, destination, key):
    nonce = secrets.token_bytes(GCM_NONCE_BYTES)
    temporary = destination.parent / f".{destination.name}.incomplete-{secrets.token_hex(8)}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(ENCRYPTED_BACKUP_MAGIC)
            output.write(nonce)
            encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(ENCRYPTED_BACKUP_MAGIC)
            encrypted_output = _EncryptingWriter(output, encryptor)
            with tarfile.open(fileobj=encrypted_output, mode="w|",
                              format=tarfile.PAX_FORMAT) as archive:
                for name in (MANIFEST_NAME, DATABASE_NAME, UPLOADS_NAME):
                    archive.add(source / name, arcname=name, recursive=True,
                                filter=_private_tar_metadata)
            final = encryptor.finalize()
            if final:
                output.write(final)
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def create_encrypted_backup(destination, key_file, database_path, upload_dir):
    """Create an atomic, authenticated backup without publishing plaintext."""
    destination = _resolve_without_symlink(destination, "Backup destination")
    database_path = _resolve_without_symlink(database_path, "Database")
    upload_dir = _resolve_without_symlink(upload_dir, "Upload directory")
    if destination.exists():
        raise BackupError(f"Backup destination already exists: {destination}")
    if destination == upload_dir or upload_dir in destination.parents:
        raise BackupError("Backup destination cannot be inside the live upload directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = _read_backup_key(key_file)

    # The only plaintext staging remains beside the already-plaintext live
    # database. The backup volume receives only an authenticated ciphertext.
    staging_root = Path(tempfile.mkdtemp(
        prefix=".encrypted-backup-staging-", dir=database_path.parent))
    snapshot = staging_root / "snapshot"
    try:
        manifest = create_backup(snapshot, database_path, upload_dir)
        _encrypt_backup_directory(snapshot, destination, key)
        return manifest
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _decrypt_to_tar(source, destination, key):
    minimum = len(ENCRYPTED_BACKUP_MAGIC) + GCM_NONCE_BYTES + GCM_TAG_BYTES
    if not source.is_file() or source.is_symlink():
        raise BackupError("Encrypted backup must be a regular file")
    try:
        size = source.stat().st_size
        if size <= minimum:
            raise BackupError("Encrypted backup is truncated")
        with source.open("rb") as incoming, destination.open("xb") as plaintext:
            if incoming.read(len(ENCRYPTED_BACKUP_MAGIC)) != ENCRYPTED_BACKUP_MAGIC:
                raise BackupError("Unsupported encrypted backup format")
            nonce = incoming.read(GCM_NONCE_BYTES)
            incoming.seek(-GCM_TAG_BYTES, os.SEEK_END)
            tag = incoming.read(GCM_TAG_BYTES)
            remaining = size - minimum
            incoming.seek(len(ENCRYPTED_BACKUP_MAGIC) + GCM_NONCE_BYTES)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(ENCRYPTED_BACKUP_MAGIC)
            while remaining:
                chunk = incoming.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise BackupError("Encrypted backup is truncated")
                remaining -= len(chunk)
                plaintext.write(decryptor.update(chunk))
            plaintext.write(decryptor.finalize())
            plaintext.flush()
            os.fsync(plaintext.fileno())
        os.chmod(destination, 0o600)
    except InvalidTag as failure:
        destination.unlink(missing_ok=True)
        raise BackupError("Encrypted backup authentication failed") from failure
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _extract_authenticated_tar(archive_path, destination):
    destination.mkdir(mode=0o700)
    seen = set()
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                if name in seen:
                    raise BackupError("Encrypted backup archive contains duplicate entries")
                seen.add(name)
                if member.isdir():
                    if name != UPLOADS_NAME:
                        raise BackupError("Encrypted backup archive contains an unsafe directory")
                    (destination / UPLOADS_NAME).mkdir(mode=0o700, exist_ok=True)
                    continue
                path = Path(name)
                valid_file = (member.isreg() and (
                    name in {MANIFEST_NAME, DATABASE_NAME}
                    or (len(path.parts) == 2 and path.parts[0] == UPLOADS_NAME
                        and _safe_storage_name(path.parts[1]))))
                if not valid_file:
                    raise BackupError("Encrypted backup archive contains an unsafe entry")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("Encrypted backup archive contains an unreadable entry")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(mode=0o700, exist_ok=True)
                with extracted, target.open("xb") as output:
                    shutil.copyfileobj(extracted, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(target, 0o600)
    except (OSError, tarfile.TarError) as failure:
        raise BackupError("Encrypted backup archive is invalid") from failure
    if not {MANIFEST_NAME, DATABASE_NAME, UPLOADS_NAME} <= seen:
        raise BackupError("Encrypted backup archive is incomplete")


@contextmanager
def decrypted_backup(source, key_file, working_directory):
    """Yield an authenticated, verified plaintext snapshot, then remove it."""
    source = _resolve_without_symlink(source, "Encrypted backup")
    working_directory = _resolve_without_symlink(working_directory, "Backup working directory")
    working_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        required_space = source.stat().st_size * 2 + 16 * 1024 * 1024
        if shutil.disk_usage(working_directory).free < required_space:
            raise BackupError("Not enough working space to verify the encrypted backup safely")
    except OSError as failure:
        raise BackupError("Encrypted backup or working directory cannot be inspected") from failure
    key = _read_backup_key(key_file)
    temporary = Path(tempfile.mkdtemp(
        prefix=".encrypted-restore-staging-", dir=working_directory))
    archive_path = temporary / "snapshot.tar"
    snapshot = temporary / "snapshot"
    try:
        _decrypt_to_tar(source, archive_path, key)
        _extract_authenticated_tar(archive_path, snapshot)
        archive_path.unlink()
        manifest = verify_backup(snapshot)
        yield snapshot, manifest
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def verify_encrypted_backup(source, key_file, working_directory):
    with decrypted_backup(source, key_file, working_directory) as (_, manifest):
        return manifest


def restore_encrypted_backup(source, key_file, database_path, upload_dir):
    database_path = _resolve_without_symlink(database_path, "Database")
    with decrypted_backup(source, key_file, database_path.parent) as (snapshot, _):
        return restore_backup(snapshot, database_path, upload_dir)


def _open_source(database_path):
    if not database_path.is_file() or database_path.is_symlink():
        raise BackupError(f"Database does not exist as a regular file: {database_path}")
    database = sqlite3.connect(database_path, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return database


def create_backup(destination, database_path, upload_dir):
    """Create an atomic directory snapshot of SQLite and every referenced file."""
    destination = _resolve_without_symlink(destination, "Backup destination")
    database_path = _resolve_without_symlink(database_path, "Database")
    upload_dir = _resolve_without_symlink(upload_dir, "Upload directory")
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
    source = _resolve_without_symlink(source, "Backup source")
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
    source = _resolve_without_symlink(source, "Backup source")
    database_path = _resolve_without_symlink(database_path, "Database")
    upload_dir = _resolve_without_symlink(upload_dir, "Upload directory")
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
