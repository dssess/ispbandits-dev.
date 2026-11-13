#!/usr/bin/env python3
"""Atomic Xray + SSH traffic collector for the Nginx Bandit-compatible schema.

Copyright (c) 2026 ISPbandits Dev. All rights reserved.

Xray source:
  `xray api statsquery --server=127.0.0.1:10085`
  Requires Xray StatsService, user stats policies, and a unique Xray user email/tag
  mapped in protocol_accounts.external_identity.

SSH source:
  The optional experimental OpenSSH producer atomically writes a root-readable
  cumulative JSON snapshot to SSH_COUNTER_FILE. Stock OpenSSH/Dropbear do not
  expose per-account network-byte totals through a simple API. The producer uses
  a post-auth socket mapping and nftables counters; it is not live-validated and
  does NOT substitute /proc/<pid>/io for network bytes.

Expected SSH snapshot (cumulative counters, not per-poll deltas):
{
  "version": 1,
  "sampled_at": "2026-11-12T15:00:00Z",
  "counters": [
    {
      "protocol": "openssh",
      "external_identity": "alice",
      "generation": "meter-epoch-uuid",
      "upload_bytes": 123456,
      "download_bytes": 654321
    }
  ]
}

The producer must aggregate counters by protocol account, publish atomically
(temp file + os.replace), keep generation stable while its cumulative counters
remain continuous, and change generation only when its counters reset. The
account-create CLI seeds new managed identity checkpoints before disclosing
credentials. Manually linked identities still need an explicit baseline before
client traffic is accepted.

All SQLite schema creation, legacy usage migration, counter checkpoint updates,
traffic total increments, and the compatibility users.data_usage refresh are
performed in one BEGIN IMMEDIATE transaction. Unknown identities or counter
regressions abort the whole transaction.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

LOG = logging.getLogger("traffic-collector")

DB_PATH = Path(os.environ.get("ISPB_DB", "/var/lib/ispbandits-dev/database.db"))
SSH_COUNTER_FILE = Path(
    os.environ.get("SSH_COUNTER_FILE", "/run/vpn-ssh-meter/counters.json")
)
XRAY_BIN = os.environ.get("XRAY_BIN") or shutil.which("xray") or "/usr/local/bin/xray"
XRAY_SERVER = os.environ.get("XRAY_API_SERVER", "127.0.0.1:10085")
XRAY_SERVICE = os.environ.get("XRAY_SERVICE", "ispbandits-xray.service")
SSH_MAX_AGE_SECONDS = int(os.environ.get("SSH_COUNTER_MAX_AGE", "180"))
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("COMMAND_TIMEOUT", "12"))

SSH_PROTOCOLS = {"openssh", "dropbear"}
XRAY_PROTOCOLS = {"vless", "trojan", "vmess"}
ALL_PROTOCOLS = SSH_PROTOCOLS | XRAY_PROTOCOLS
MIGRATION_VERSION = "traffic-v1-import-legacy-data-usage"


class CollectorError(RuntimeError):
    """Raised when an input is incomplete or unsafe to apply."""


class CounterSample:
    def __init__(
        self,
        source_kind: str,
        identity: str,
        generation: str,
        upload_bytes: int,
        download_bytes: int,
        protocol: Optional[str] = None,
    ) -> None:
        self.source_kind = source_kind
        self.identity = identity
        self.generation = generation
        self.upload_bytes = upload_bytes
        self.download_bytes = download_bytes
        self.protocol = protocol


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: Optional[dt.datetime] = None) -> str:
    value = value or utc_now()
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise CollectorError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectorError(f"Invalid {label} timestamp") from exc
    if parsed.tzinfo is None:
        raise CollectorError(f"{label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise CollectorError(f"{label} must be a non-negative integer")
    try:
        number = int(value)  # Xray JSON represents values as decimal strings.
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"{label} must be a non-negative integer") from exc
    if number < 0 or str(number) != str(value).strip():
        raise CollectorError(f"{label} must be a non-negative integer")
    return number


def run_checked(command: List[str], label: str) -> str:
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError(f"Could not run {label}") from exc
    if proc.returncode != 0:
        # Avoid logging command output: it may contain tags or host-specific data.
        raise CollectorError(f"{label} failed with exit code {proc.returncode}")
    return proc.stdout.strip()


def xray_generation() -> str:
    """Return a generation that changes when the Xray process restarts."""
    pid_text = ""
    try:
        pid_text = run_checked(
            ["systemctl", "show", XRAY_SERVICE, "--property=MainPID", "--value"],
            "systemd Xray PID lookup",
        )
    except CollectorError:
        pass

    if not pid_text or not pid_text.isdigit() or int(pid_text) <= 0:
        pgrep = shutil.which("pgrep")
        if not pgrep:
            raise CollectorError("Cannot identify the running Xray process")
        pid_text = run_checked([pgrep, "-xo", "xray"], "Xray process lookup")

    pid = int(pid_text)
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close_paren = stat_text.rfind(")")
        fields_after_comm = stat_text[close_paren + 2 :].split()
        # /proc/PID/stat field 22 (starttime) is index 19 after field 2 (comm).
        start_ticks = fields_after_comm[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IndexError) as exc:
        raise CollectorError("Cannot read Xray process start identity") from exc
    return f"{boot_id}:{pid}:{start_ticks}"


def read_xray_samples() -> List[CounterSample]:
    if not Path(XRAY_BIN).is_file() and shutil.which(XRAY_BIN) is None:
        raise CollectorError("Xray binary not found; set XRAY_BIN")

    output = run_checked(
        [XRAY_BIN, "api", "statsquery", f"--server={XRAY_SERVER}"],
        "Xray StatsService query",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CollectorError("Xray statsquery did not return JSON") from exc

    stats = payload.get("stat")
    if not isinstance(stats, list):
        raise CollectorError("Xray response has no 'stat' array")

    by_identity: Dict[str, Dict[str, int]] = {}
    for item in stats:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        parts = name.split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue
        direction = parts[3]
        if direction not in ("uplink", "downlink"):
            continue
        identity = parts[1]
        if not identity:
            raise CollectorError("Xray returned an empty user identity")
        values = by_identity.setdefault(identity, {})
        if direction in values:
            raise CollectorError("Xray returned a duplicate user traffic counter")
        values[direction] = nonnegative_int(item.get("value"), "Xray counter")

    generation = xray_generation()
    samples: List[CounterSample] = []
    for identity, values in by_identity.items():
        # Xray calls these uplink/downlink. The UI treats them as client
        # upload/download; validate direction once with known-byte test transfers.
        samples.append(
            CounterSample(
                source_kind="xray",
                identity=identity,
                generation=generation,
                upload_bytes=values.get("uplink", 0),
                download_bytes=values.get("downlink", 0),
            )
        )
    return samples


def read_ssh_samples() -> List[CounterSample]:
    try:
        file_stat = SSH_COUNTER_FILE.stat()
    except OSError as exc:
        raise CollectorError(
            "SSH counter snapshot is missing; install/configure the SSH network-meter producer"
        ) from exc

    if file_stat.st_mode & 0o077:
        raise CollectorError("SSH counter snapshot must not be group/world accessible")
    try:
        payload = json.loads(SSH_COUNTER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError("SSH counter snapshot is unreadable or invalid JSON") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise CollectorError("Unsupported SSH counter snapshot version")
    sampled_at = parse_timestamp(payload.get("sampled_at"), "SSH snapshot")
    age = (utc_now() - sampled_at).total_seconds()
    if age < -60 or age > SSH_MAX_AGE_SECONDS:
        raise CollectorError("SSH counter snapshot is stale or has a future timestamp")

    entries = payload.get("counters")
    if not isinstance(entries, list):
        raise CollectorError("SSH snapshot must contain a 'counters' array")

    samples: List[CounterSample] = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CollectorError("Invalid SSH counter entry")
        protocol = entry.get("protocol")
        identity = entry.get("external_identity")
        generation = entry.get("generation")
        if protocol not in SSH_PROTOCOLS:
            raise CollectorError("SSH snapshot protocol must be openssh or dropbear")
        if not isinstance(identity, str) or not identity.strip():
            raise CollectorError("SSH snapshot has an empty account identity")
        if not isinstance(generation, str) or not generation.strip():
            raise CollectorError("SSH snapshot entry has no counter generation")
        key = (protocol, identity)
        if key in seen:
            raise CollectorError("Duplicate SSH identity in counter snapshot")
        seen.add(key)
        samples.append(
            CounterSample(
                source_kind="ssh",
                protocol=protocol,
                identity=identity,
                generation=generation,
                upload_bytes=nonnegative_int(entry.get("upload_bytes"), "SSH upload_bytes"),
                download_bytes=nonnegative_int(
                    entry.get("download_bytes"), "SSH download_bytes"
                ),
            )
        )
    return samples


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS protocol_accounts (
               id INTEGER PRIMARY KEY,
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               protocol TEXT NOT NULL CHECK (
                   protocol IN ('openssh','dropbear','vless','trojan','vmess')
               ),
               external_identity TEXT NOT NULL,
               credential_ref TEXT,
               enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               UNIQUE (protocol, external_identity)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS traffic_totals (
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               protocol TEXT NOT NULL CHECK (
                   protocol IN ('openssh','dropbear','vless','trojan','vmess','legacy')
               ),
               upload_bytes INTEGER NOT NULL DEFAULT 0 CHECK (upload_bytes >= 0),
               download_bytes INTEGER NOT NULL DEFAULT 0 CHECK (download_bytes >= 0),
               legacy_bytes INTEGER NOT NULL DEFAULT 0 CHECK (legacy_bytes >= 0),
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (user_id, protocol)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS traffic_checkpoints (
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               protocol TEXT NOT NULL,
               source_key TEXT NOT NULL,
               direction TEXT NOT NULL CHECK (direction IN ('upload','download')),
               generation TEXT NOT NULL,
               last_counter_bytes INTEGER NOT NULL CHECK (last_counter_bytes >= 0),
               sampled_at TEXT NOT NULL,
               PRIMARY KEY (user_id, source_key, direction)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_protocol_accounts_user ON protocol_accounts(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_traffic_totals_user ON traffic_totals(user_id)"
    )

    user_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not user_table:
        raise CollectorError("Expected existing Nginx Bandit 'users' table was not found")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    required = {"id", "username", "data_usage"}
    if not required.issubset(columns):
        raise CollectorError("Existing users table lacks id, username, or data_usage")


def import_legacy_usage_once(conn: sqlite3.Connection) -> None:
    already = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    ).fetchone()
    if already:
        return
    conn.execute(
        """INSERT OR IGNORE INTO traffic_totals
               (user_id, protocol, upload_bytes, download_bytes, legacy_bytes, updated_at)
           SELECT id, 'legacy', 0, 0, MAX(COALESCE(data_usage, 0), 0), ?
           FROM users
           WHERE COALESCE(data_usage, 0) > 0""",
        (utc_iso(),),
    )
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (MIGRATION_VERSION, utc_iso()),
    )


def load_protocol_maps(conn: sqlite3.Connection) -> Tuple[Dict[Tuple[str, str], int], Dict[str, Tuple[int, str]]]:
    ssh_map: Dict[Tuple[str, str], int] = {}
    xray_map: Dict[str, Tuple[int, str]] = {}
    rows = conn.execute(
        "SELECT user_id, protocol, external_identity FROM protocol_accounts"
    ).fetchall()
    for user_id, protocol, identity in rows:
        if protocol not in ALL_PROTOCOLS:
            continue
        if protocol in SSH_PROTOCOLS:
            ssh_map[(protocol, identity)] = int(user_id)
        elif protocol in XRAY_PROTOCOLS:
            # To separate VLESS/Trojan totals, give each Xray protocol identity
            # a unique stats email/tag in Xray and map it to exactly one row here.
            if identity in xray_map and xray_map[identity] != (int(user_id), protocol):
                raise CollectorError("Xray stats identity maps to multiple protocol accounts")
            xray_map[identity] = (int(user_id), protocol)
    return ssh_map, xray_map


def seed_account_checkpoints(
    database_path: Path | str,
    username: str,
    xray_samples: List[CounterSample],
    ssh_samples: List[CounterSample],
    xray_generation_id: Optional[str] = None,
) -> int:
    """Seed a just-created account's counters before its credentials are shown.

    Existing checkpoints are never overwritten. Xray stats entries may not
    exist until the first byte is transferred; in that case, a zero checkpoint
    at the current Xray generation prevents the first real sample from being
    lost as a baseline.
    """
    path = Path(database_path)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        create_schema(conn)
        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if user is None:
            raise CollectorError("Cannot seed counters for an unknown account")
        user_id = int(user[0])
        protocols = conn.execute("""SELECT protocol,external_identity FROM protocol_accounts
            WHERE user_id=? AND enabled=1""", (user_id,)).fetchall()
        xray_by_identity = {sample.identity: sample for sample in xray_samples}
        ssh_by_identity = {(sample.protocol, sample.identity): sample for sample in ssh_samples}
        for protocol, identity in protocols:
            protocol, identity = str(protocol), str(identity)
            if protocol in XRAY_PROTOCOLS:
                sample = xray_by_identity.get(identity)
                generation = sample.generation if sample else xray_generation_id
                if not generation:
                    raise CollectorError("Cannot seed an Xray account without a current process generation")
                upload = sample.upload_bytes if sample else 0
                download = sample.download_bytes if sample else 0
                source_key = f"xray:{identity}"
            elif protocol in SSH_PROTOCOLS:
                sample = ssh_by_identity.get((protocol, identity))
                if sample is None:
                    continue
                generation = sample.generation
                upload = sample.upload_bytes
                download = sample.download_bytes
                source_key = f"ssh:{protocol}:{identity}"
            else:
                continue
            for direction, current in (("upload", upload), ("download", download)):
                cursor = conn.execute("""INSERT OR IGNORE INTO traffic_checkpoints
                    (user_id,protocol,source_key,direction,generation,last_counter_bytes,sampled_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (user_id, protocol, source_key, direction, generation, int(current), utc_iso()))
                inserted += cursor.rowcount
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_one_counter(
    conn: sqlite3.Connection,
    user_id: int,
    protocol: str,
    source_key: str,
    generation: str,
    direction: str,
    current: int,
    sampled_at: str,
) -> Tuple[int, bool]:
    old = conn.execute(
        """SELECT protocol, generation, last_counter_bytes
           FROM traffic_checkpoints
           WHERE user_id = ? AND source_key = ? AND direction = ?""",
        (user_id, source_key, direction),
    ).fetchone()

    if old is None:
        # First observation is a baseline. Run once before allowing traffic.
        delta = 0
        reset = False
    else:
        old_protocol, old_generation, old_value = old
        if old_protocol != protocol:
            raise CollectorError("Counter checkpoint protocol changed unexpectedly")
        if old_generation == generation:
            if current < int(old_value):
                raise CollectorError(
                    "Counter decreased without a generation change; refusing to corrupt totals"
                )
            delta = current - int(old_value)
            reset = False
        else:
            # New generation means the source counter restarted/reset.
            delta = current
            reset = True

    conn.execute(
        """INSERT INTO traffic_checkpoints
               (user_id, protocol, source_key, direction, generation,
                last_counter_bytes, sampled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, source_key, direction) DO UPDATE SET
               protocol = excluded.protocol,
               generation = excluded.generation,
               last_counter_bytes = excluded.last_counter_bytes,
               sampled_at = excluded.sampled_at""",
        (user_id, protocol, source_key, direction, generation, current, sampled_at),
    )

    if delta:
        up = delta if direction == "upload" else 0
        down = delta if direction == "download" else 0
        conn.execute(
            """INSERT INTO traffic_totals
                   (user_id, protocol, upload_bytes, download_bytes, legacy_bytes, updated_at)
               VALUES (?, ?, ?, ?, 0, ?)
               ON CONFLICT(user_id, protocol) DO UPDATE SET
                   upload_bytes = traffic_totals.upload_bytes + excluded.upload_bytes,
                   download_bytes = traffic_totals.download_bytes + excluded.download_bytes,
                   updated_at = excluded.updated_at""",
            (user_id, protocol, up, down, sampled_at),
        )
    return delta, reset


def collect(dry_run: bool = False, xray_only: bool = False) -> Tuple[int, int, int]:
    # Read both upstreams before acquiring the SQLite write lock. If either
    # source fails, there is no partial accounting update.
    xray_samples = read_xray_samples()
    ssh_samples = [] if xray_only else read_ssh_samples()
    sampled_at = utc_iso()

    conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        create_schema(conn)
        import_legacy_usage_once(conn)
        ssh_map, xray_map = load_protocol_maps(conn)

        total_samples = 0
        reset_count = 0
        delta_bytes = 0

        for sample in xray_samples:
            mapped = xray_map.get(sample.identity)
            if mapped is None:
                raise CollectorError("Xray returned an unmapped user stats identity")
            user_id, protocol = mapped
            source_key = f"xray:{sample.identity}"
            for direction, value in (
                ("upload", sample.upload_bytes),
                ("download", sample.download_bytes),
            ):
                delta, reset = apply_one_counter(
                    conn, user_id, protocol, source_key, sample.generation,
                    direction, value, sampled_at
                )
                total_samples += 1
                delta_bytes += delta
                reset_count += int(reset)

        for sample in ssh_samples:
            assert sample.protocol is not None
            user_id = ssh_map.get((sample.protocol, sample.identity))
            if user_id is None:
                raise CollectorError("SSH meter returned an unmapped protocol account")
            source_key = f"ssh:{sample.protocol}:{sample.identity}"
            for direction, value in (
                ("upload", sample.upload_bytes),
                ("download", sample.download_bytes),
            ):
                delta, reset = apply_one_counter(
                    conn, user_id, sample.protocol, source_key, sample.generation,
                    direction, value, sampled_at
                )
                total_samples += 1
                delta_bytes += delta
                reset_count += int(reset)

        # Keep the old Nginx Bandit menu field populated as a compatibility total.
        conn.execute(
            """UPDATE users
               SET data_usage = COALESCE((
                   SELECT SUM(upload_bytes + download_bytes + legacy_bytes)
                   FROM traffic_totals
                   WHERE traffic_totals.user_id = users.id
               ), 0)"""
        )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return total_samples, delta_bytes, reset_count
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomically collect Xray and optional SSH traffic totals")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read sources and compute updates, then roll back without changing SQLite",
    )
    parser.add_argument("--xray-only", action="store_true",
                        help="collect Xray VLESS/Trojan only until an SSH network-meter producer is installed")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if os.geteuid() != 0:
        LOG.error("Run as root so the collector can access local service/account metadata")
        return 2
    if not DB_PATH.is_file():
        LOG.error("SQLite database not found: %s", DB_PATH)
        return 2

    try:
        samples, delta_bytes, resets = collect(dry_run=args.dry_run, xray_only=args.xray_only)
    except CollectorError as exc:
        LOG.error("Collection aborted: %s", exc)
        return 1
    except sqlite3.Error as exc:
        LOG.error("SQLite transaction aborted: %s", exc)
        return 1

    action = "dry-run; rolled back" if args.dry_run else "committed"
    LOG.info(
        "Collection %s: counter_directions=%d, delta_bytes=%d, generation_changes=%d",
        action,
        samples,
        delta_bytes,
        resets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
