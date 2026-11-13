"""Online SQLite backup and rollback-safe restore helpers."""
# © 2026 ISPbandits Dev. All rights reserved.
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile
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
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
        # Same-filesystem atomic publish; never leave a partial backup at target.
        os.replace(temp_archive, target)
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
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        allowed = {"database.db", "manifest.json"}
        if len(members) != 2 or {m.name for m in members} != allowed:
            raise ValueError("Unexpected backup archive contents")
        if any(not m.isfile() or PurePosixPath(m.name).is_absolute() or ".." in PurePosixPath(m.name).parts for m in members):
            raise ValueError("Unsafe backup archive member")
        db_member = tar.extractfile("database.db")
        manifest_member = tar.extractfile("manifest.json")
        if db_member is None or manifest_member is None:
            raise ValueError("Backup archive is incomplete")
        manifest = json.loads(manifest_member.read().decode("utf-8"))
        if manifest.get("format") != 1:
            raise ValueError("Unsupported backup format")
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=database.parent) as td:
            staged = Path(td) / "database.db"
            with staged.open("wb") as f:
                f.write(db_member.read())
            os.chmod(staged, 0o600)
            if _sha256(staged) != manifest.get("database_sha256"):
                raise ValueError("Backup checksum does not match")
            _validate_sqlite(staged)
            rollback = None
            if database.exists():
                rollback = create_backup(database, backup_dir)
            # Replace atomically from a staged file on the same filesystem.
            replacement = database.parent / f".{database.name}.restore.tmp"
            with staged.open("rb") as src, replacement.open("wb") as dst:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
            os.chmod(replacement, 0o600)
            os.replace(replacement, database)
            return rollback
