"""Online SQLite backup and rollback-safe restore helpers."""
# © 2026 ISPbandits Dev. All rights reserved.
from __future__ import annotations
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import tarfile
from datetime import datetime, timezone


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def create_backup(database: Path | str, backup_dir: Path | str) -> Path:
    database, backup_dir = Path(database), Path(backup_dir)
    if not database.is_file():
        raise FileNotFoundError(database)
    if backup_dir.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    existed = backup_dir.exists()
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(backup_dir, 0o700)
    elif stat.S_IMODE(backup_dir.stat().st_mode) & 0o077:
        raise PermissionError("Backup directory must be private (mode 0700); choose a dedicated directory")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = backup_dir / f"ispbandits-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(dir=backup_dir) as td:
        staged = Path(td) / "database.db"
        src = sqlite3.connect(str(database), timeout=15)
        dst = sqlite3.connect(str(staged))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(staged, 0o600)
        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_sha256": _sha256(staged),
        }
        manifest_path = Path(td) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        temp_archive = Path(td) / "archive.tar.gz"
        with tarfile.open(temp_archive, "w:gz") as tar:
            tar.add(staged, arcname="database.db", recursive=False)
            tar.add(manifest_path, arcname="manifest.json", recursive=False)
        os.chmod(temp_archive, 0o600)
        archive_fd = os.open(temp_archive, os.O_RDONLY)
        try:
            os.fsync(archive_fd)
        finally:
            os.close(archive_fd)
        # A hard-link publish is atomic and refuses to overwrite any existing archive.
        os.link(temp_archive, target)
        directory_fd = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    os.chmod(target, 0o600)
    return target


def _validate_sqlite(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("Backup SQLite integrity check failed")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            raise ValueError("Backup is missing the users table")
    finally:
        conn.close()


def restore_backup(database: Path | str, archive: Path | str, backup_dir: Path | str) -> Path | None:
    database, archive = Path(database), Path(archive)
    max_database_bytes = 16 * 1024 * 1024 * 1024
    max_manifest_bytes = 64 * 1024
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        allowed = {"database.db", "manifest.json"}
        if len(members) != 2 or {m.name for m in members} != allowed:
            raise ValueError("Unexpected backup archive contents")
        if any(not m.isfile() or m.issym() or m.islnk() or PurePosixPath(m.name).is_absolute()
               or ".." in PurePosixPath(m.name).parts for m in members):
            raise ValueError("Unsafe backup archive member")
        sizes = {m.name: m.size for m in members}
        if sizes["database.db"] < 100 or sizes["database.db"] > max_database_bytes:
            raise ValueError("Backup database size is outside the supported restore limit")
        if sizes["manifest.json"] > max_manifest_bytes:
            raise ValueError("Backup manifest is too large")
        db_member = tar.extractfile("database.db")
        manifest_member = tar.extractfile("manifest.json")
        if db_member is None or manifest_member is None:
            raise ValueError("Backup archive is incomplete")
        manifest = json.loads(manifest_member.read(max_manifest_bytes + 1).decode("utf-8"))
        if manifest.get("format") != 1:
            raise ValueError("Unsupported backup format")
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=database.parent) as td:
            staged = Path(td) / "database.db"
            with staged.open("wb") as f:
                copied = 0
                while True:
                    block = db_member.read(1024 * 1024)
                    if not block:
                        break
                    copied += len(block)
                    if copied > max_database_bytes:
                        raise ValueError("Backup database exceeds the supported restore limit")
                    f.write(block)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(staged, 0o600)
            if _sha256(staged) != manifest.get("database_sha256"):
                raise ValueError("Backup checksum does not match")
            _validate_sqlite(staged)
            rollback = None
            if database.exists():
                rollback = create_backup(database, backup_dir)
            # Replace atomically from a staged file on the same filesystem.
            fd, replacement_name = tempfile.mkstemp(prefix=f".{database.name}.restore.",
                                                     suffix=".tmp", dir=database.parent)
            replacement = Path(replacement_name)
            try:
                with staged.open("rb") as src, os.fdopen(fd, "wb") as dst:
                    while True:
                        block = src.read(1024 * 1024)
                        if not block:
                            break
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.chmod(replacement, 0o600)
                # Caller must quiesce SQLite writers before restore. Stale WAL
                # files belong to the old database and must not be replayed onto
                # the replacement file after its atomic rename.
                Path(str(database) + "-wal").unlink(missing_ok=True)
                Path(str(database) + "-shm").unlink(missing_ok=True)
                os.replace(replacement, database)
                dir_fd = os.open(database.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                replacement.unlink(missing_ok=True)
            return rollback
