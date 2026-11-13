"""SQLite persistence for ISPbandits Dev Version 1.

Copyright (c) 2026 ISPbandits Dev. All rights reserved.
Proprietary software; see LICENSE.proprietary.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Iterator
import uuid as uuidlib

DEFAULT_DB = Path(os.environ.get("ISPB_DB", "/var/lib/ispbandits-dev/database.db"))
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PROTOCOLS = {"openssh", "dropbear", "vless", "trojan", "vmess"}


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def initialize(path: Path | str = DEFAULT_DB) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        with transaction(conn):
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                uuid TEXT,
                protocols TEXT DEFAULT 'ssh,ws,socks',
                expiry_date TEXT NOT NULL,
                max_logins INTEGER DEFAULT 2,
                bandwidth_limit_mb INTEGER DEFAULT 0,
                bandwidth_used_mb INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ACTIVE',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                data_usage INTEGER DEFAULT 0,
                data_limit INTEGER DEFAULT 0
            )""")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            compatible_additions = {
                "uuid": "TEXT",
                "protocols": "TEXT DEFAULT 'ssh,ws,socks'",
                "expiry_date": "TEXT",
                "max_logins": "INTEGER DEFAULT 2",
                "bandwidth_limit_mb": "INTEGER DEFAULT 0",
                "bandwidth_used_mb": "INTEGER DEFAULT 0",
                "status": "TEXT DEFAULT 'ACTIVE'",
                "created_at": "TEXT",
                "data_usage": "INTEGER DEFAULT 0",
                "data_limit": "INTEGER DEFAULT 0",
            }
            for name, declaration in compatible_additions.items():
                if name not in cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS protocol_accounts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                protocol TEXT NOT NULL CHECK(protocol IN
                    ('openssh','dropbear','vless','trojan','vmess')),
                external_identity TEXT NOT NULL,
                credential_ref TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(protocol, external_identity),
                UNIQUE(user_id, protocol)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS protocol_secrets (
                user_id INTEGER NOT NULL,
                protocol TEXT NOT NULL CHECK(protocol IN ('vless','trojan')),
                secret TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, protocol),
                FOREIGN KEY(user_id, protocol)
                    REFERENCES protocol_accounts(user_id, protocol) ON DELETE CASCADE
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS traffic_totals (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                protocol TEXT NOT NULL CHECK(protocol IN
                    ('openssh','dropbear','vless','trojan','vmess','legacy')),
                upload_bytes INTEGER NOT NULL DEFAULT 0 CHECK(upload_bytes >= 0),
                download_bytes INTEGER NOT NULL DEFAULT 0 CHECK(download_bytes >= 0),
                legacy_bytes INTEGER NOT NULL DEFAULT 0 CHECK(legacy_bytes >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, protocol)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS traffic_checkpoints (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                protocol TEXT NOT NULL,
                source_key TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('upload','download')),
                generation TEXT NOT NULL,
                last_counter_bytes INTEGER NOT NULL CHECK(last_counter_bytes >= 0),
                sampled_at TEXT NOT NULL,
                PRIMARY KEY(user_id, source_key, direction)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_protocol_accounts_user ON protocol_accounts(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_totals_user ON traffic_totals(user_id)")
            # Preserve old readings without claiming they were network bytes.
            marker = "v1-import-legacy-data-usage"
            migrated = conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (marker,)).fetchone()
            if not migrated:
                conn.execute("""INSERT OR IGNORE INTO traffic_totals
                    (user_id, protocol, legacy_bytes)
                    SELECT id, 'legacy', MAX(COALESCE(data_usage,0),0)
                    FROM users WHERE COALESCE(data_usage,0)>0""")
                conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                             (marker, dt.datetime.now(dt.timezone.utc).isoformat()))
    finally:
        conn.close()
    os.chmod(path, 0o600)


def validate_username(username: str) -> str:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must match [a-z_][a-z0-9_-]{0,31}")
    return username


def add_user(path: Path | str, username: str, expiry_date: str,
             max_logins: int = 2, data_limit: int = 0,
             ssh_protocols: tuple[str, ...] = ()) -> int:
    validate_username(username)
    try:
        expiry = dt.date.fromisoformat(expiry_date)
    except ValueError as exc:
        raise ValueError("Expiry must be an ISO date: YYYY-MM-DD") from exc
    if max_logins < 1 or data_limit < 0:
        raise ValueError("max_logins must be positive and data_limit must be non-negative bytes")
    if len(set(ssh_protocols)) != len(ssh_protocols) or set(ssh_protocols) - {"openssh", "dropbear"}:
        raise ValueError("ssh_protocols may contain openssh and/or dropbear only")
    conn = connect(path)
    try:
        with transaction(conn):
            cur = conn.execute("""INSERT INTO users
                (username, uuid, expiry_date, max_logins, status, data_limit, protocols)
                VALUES(?,?,?,?,'ACTIVE',?,?)""",
                (username, str(uuidlib.uuid4()), expiry.isoformat(), max_logins, data_limit,
                 ",".join(ssh_protocols)))
            user_id = int(cur.lastrowid)
            for protocol in ssh_protocols:
                conn.execute("""INSERT INTO protocol_accounts
                    (user_id,protocol,external_identity,credential_ref)
                    VALUES(?,?,?,NULL)""", (user_id, protocol, username))
            conn.execute("INSERT INTO audit_events(event_type,user_id,detail) VALUES('user-created',?,?)",
                         (user_id, username))
            return user_id
    finally:
        conn.close()


def link_protocol(path: Path | str, username: str, protocol: str,
                  identity: str, credential_ref: str | None = None) -> None:
    if protocol not in PROTOCOLS:
        raise ValueError(f"Unsupported protocol: {protocol}")
    if not identity or len(identity) > 255 or any(ord(c) < 32 for c in identity):
        raise ValueError("Identity must be 1-255 printable characters")
    conn = connect(path)
    try:
        with transaction(conn):
            user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if not user:
                raise ValueError(f"Unknown account: {username}")
            conn.execute("""INSERT INTO protocol_accounts
                (user_id,protocol,external_identity,credential_ref)
                VALUES(?,?,?,?)""", (user["id"], protocol, identity, credential_ref))
            conn.execute("INSERT INTO audit_events(event_type,user_id,detail) VALUES('protocol-linked',?,?)",
                         (user["id"], protocol))
    finally:
        conn.close()


def list_usage(path: Path | str) -> list[sqlite3.Row]:
    conn = connect(path)
    try:
        return conn.execute("""SELECT u.id,u.username,u.expiry_date,u.status,
            COALESCE(SUM(t.upload_bytes),0) upload_bytes,
            COALESCE(SUM(t.download_bytes),0) download_bytes,
            COALESCE(SUM(t.legacy_bytes),0) legacy_bytes,
            COALESCE(SUM(t.upload_bytes+t.download_bytes),0) measured_bytes,
            COALESCE(MAX(u.data_limit),0) data_limit,
            COALESCE(SUM(t.upload_bytes+t.download_bytes+t.legacy_bytes),0) total_bytes
            FROM users u LEFT JOIN traffic_totals t ON t.user_id=u.id
            GROUP BY u.id ORDER BY u.username""").fetchall()
    finally:
        conn.close()


def enforce_data_quotas(path: Path | str, today: dt.date | None = None) -> list[str]:
    """Mark active accounts over their shared lifetime quota using measured bytes only.

    Legacy imported usage is intentionally excluded: its measurement source is
    unknown and it must not cause a new account lockout by itself.
    """
    today = today or dt.date.today()
    conn = connect(path)
    try:
        with transaction(conn):
            rows = conn.execute("""SELECT u.id,u.username,
                    COALESCE(SUM(t.upload_bytes+t.download_bytes),0) AS measured
                FROM users u LEFT JOIN traffic_totals t ON t.user_id=u.id
                WHERE UPPER(COALESCE(u.status,''))='ACTIVE'
                  AND COALESCE(u.data_limit,0)>0
                  AND substr(COALESCE(u.expiry_date,''),1,10)>=?
                GROUP BY u.id
                HAVING COALESCE(SUM(t.upload_bytes+t.download_bytes),0)>=u.data_limit
                ORDER BY u.username""", (today.isoformat(),)).fetchall()
            usernames = []
            for row in rows:
                conn.execute("UPDATE users SET status='QUOTA_EXCEEDED' WHERE id=?", (row["id"],))
                conn.execute("""INSERT INTO audit_events(event_type,user_id,detail)
                    VALUES('quota-exceeded',?,?)""",
                    (row["id"], f"measured lifetime usage reached {row['measured']} bytes"))
                usernames.append(str(row["username"]))
            return usernames
    finally:
        conn.close()


def set_data_limit(path: Path | str, username: str, limit_bytes: int,
                   today: dt.date | None = None) -> dict[str, str | int]:
    """Set a lifetime account quota and report whether account status changed."""
    validate_username(username)
    if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int) or limit_bytes < 0:
        raise ValueError("Quota must be a non-negative integer number of bytes")
    today = today or dt.date.today()
    conn = connect(path)
    try:
        with transaction(conn):
            row = conn.execute("""SELECT id,status,expiry_date FROM users WHERE username=?""",
                               (username,)).fetchone()
            if not row:
                raise ValueError(f"Unknown account: {username}")
            measured = int(conn.execute("""SELECT COALESCE(SUM(upload_bytes+download_bytes),0)
                FROM traffic_totals WHERE user_id=?""", (row["id"],)).fetchone()[0])
            old_status = str(row["status"]).upper()
            new_status = old_status
            not_expired = str(row["expiry_date"] or "")[:10] >= today.isoformat()
            if old_status == "QUOTA_EXCEEDED" and not_expired and (limit_bytes == 0 or measured < limit_bytes):
                new_status = "ACTIVE"
            elif old_status == "ACTIVE" and limit_bytes > 0 and measured >= limit_bytes:
                new_status = "QUOTA_EXCEEDED"
            conn.execute("UPDATE users SET data_limit=?,status=? WHERE id=?",
                         (limit_bytes, new_status, row["id"]))
            conn.execute("""INSERT INTO audit_events(event_type,user_id,detail)
                VALUES('quota-updated',?,?)""",
                (row["id"], f"lifetime quota set to {limit_bytes} bytes; status {old_status}->{new_status}"))
            return {"username": username, "measured_bytes": measured,
                    "old_status": old_status, "status": new_status,
                    "expiry_date": str(row["expiry_date"])}
    finally:
        conn.close()


def list_quota_exceeded_users(path: Path | str) -> list[str]:
    """Return all quota-locked accounts for idempotent external revocation retries."""
    conn = connect(path)
    try:
        rows = conn.execute("""SELECT username FROM users
            WHERE UPPER(COALESCE(status,''))='QUOTA_EXCEEDED'
            ORDER BY username""").fetchall()
        return [str(row["username"]) for row in rows]
    finally:
        conn.close()


def list_enabled_protocols(path: Path | str, username: str) -> list[tuple[str, str]]:
    """Return enabled (protocol, external identity) pairs for one account."""
    validate_username(username)
    conn = connect(path)
    try:
        rows = conn.execute("""SELECT p.protocol,p.external_identity
            FROM protocol_accounts p JOIN users u ON u.id=p.user_id
            WHERE u.username=? AND p.enabled=1 ORDER BY p.protocol""", (username,)).fetchall()
        return [(str(row["protocol"]), str(row["external_identity"])) for row in rows]
    finally:
        conn.close()


def create_xray_protocol(path: Path | str, username: str, protocol: str,
                         rotate: bool = False) -> dict[str, str]:
    """Create a unique Xray stats identity and credential for VLESS or Trojan.

    Secrets are stored in the root-only SQLite database and are required in
    Xray's server config. The caller must protect both the DB and generated
    Xray config as credentials.
    """
    if protocol not in {"vless", "trojan"}:
        raise ValueError("Xray protocol must be vless or trojan")
    today = dt.date.today()
    conn = connect(path)
    try:
        with transaction(conn):
            user = conn.execute("""SELECT id, uuid, status, expiry_date FROM users
                WHERE username=?""", (username,)).fetchone()
            if not user:
                raise ValueError(f"Unknown account: {username}")
            if str(user["status"]).upper() != "ACTIVE":
                raise ValueError("Cannot provision an inactive account")
            try:
                expiry = dt.date.fromisoformat(str(user["expiry_date"])[:10])
            except ValueError as exc:
                raise ValueError("Account has an invalid expiry date") from exc
            if expiry < today:
                raise ValueError("Cannot provision an expired account")

            existing = conn.execute("""SELECT id FROM protocol_accounts
                WHERE user_id=? AND protocol=?""", (user["id"], protocol)).fetchone()
            if existing and not rotate:
                raise ValueError(f"{protocol} is already provisioned; use rotate to replace its credential")

            identity = f"{username}-{protocol}@xray.ispbandits.invalid"
            if protocol == "vless":
                credential = str(uuidlib.uuid4())
                conn.execute("UPDATE users SET uuid=? WHERE id=?", (credential, user["id"]))
            else:
                credential = secrets.token_urlsafe(32)

            if existing:
                conn.execute("UPDATE protocol_accounts SET external_identity=?,enabled=1 WHERE user_id=? AND protocol=?",
                             (identity, user["id"], protocol))
                conn.execute("""INSERT INTO protocol_secrets(user_id,protocol,secret)
                    VALUES(?,?,?) ON CONFLICT(user_id,protocol) DO UPDATE SET
                    secret=excluded.secret,updated_at=CURRENT_TIMESTAMP""",
                    (user["id"], protocol, credential))
                event = "protocol-credential-rotated"
            else:
                conn.execute("""INSERT INTO protocol_accounts
                    (user_id,protocol,external_identity,credential_ref)
                    VALUES(?,?,?,'protocol_secrets')""",
                    (user["id"], protocol, identity))
                conn.execute("INSERT INTO protocol_secrets(user_id,protocol,secret) VALUES(?,?,?)",
                             (user["id"], protocol, credential))
                event = "protocol-provisioned"
            conn.execute("INSERT INTO audit_events(event_type,user_id,detail) VALUES(?,?,?)",
                         (event, user["id"], protocol))
            return {"username": username, "protocol": protocol,
                    "identity": identity, "credential": credential}
    finally:
        conn.close()


def delete_user_record(path: Path | str, username: str) -> None:
    """Delete a user record for failed creation rollback; related rows cascade."""
    conn = connect(path)
    try:
        with transaction(conn):
            cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
            if cur.rowcount != 1:
                raise ValueError(f"Unknown account: {username}")
    finally:
        conn.close()


def expire_users(path: Path | str, today: dt.date | None = None) -> list[str]:
    """Mark due accounts expired and return their usernames for OS revocation."""
    today = today or dt.date.today()
    conn = connect(path)
    try:
        with transaction(conn):
            rows = conn.execute("""SELECT id,username FROM users
                WHERE UPPER(COALESCE(status,''))='ACTIVE'
                  AND substr(COALESCE(expiry_date,''),1,10) < ?""", (today.isoformat(),)).fetchall()
            for row in rows:
                conn.execute("UPDATE users SET status='EXPIRED' WHERE id=?", (row["id"],))
                conn.execute("""INSERT INTO audit_events(event_type,user_id,detail)
                    VALUES('user-expired',?,'expiry-date reached')""", (row["id"],))
            return [str(row["username"]) for row in rows]
    finally:
        conn.close()
