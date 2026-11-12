"""Best-effort SSH/Dropbear online presence from Linux process metadata.

The count is an observed daemon-process count, not a guaranteed device count.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import pwd
import sqlite3


def online_ssh_processes(database: Path | str, proc_root: Path | str = "/proc") -> list[dict[str, object]]:
    """Return active managed SSH/Dropbear accounts and observed process counts.

    Reads process ownership and comm from procfs. Must run as root for a complete
    system-wide scan. Matching is by local OS username and daemon comm name.
    """
    if os.geteuid() != 0:
        raise PermissionError("Online SSH monitoring requires root to inspect all process owners")

    today = dt.date.today().isoformat()
    conn = sqlite3.connect(str(database), timeout=10)
    try:
        records = conn.execute("""SELECT p.protocol, p.external_identity
            FROM protocol_accounts p
            JOIN users u ON u.id=p.user_id
            WHERE p.enabled=1 AND p.protocol IN ('openssh','dropbear')
              AND upper(coalesce(u.status,''))='ACTIVE'
              AND (u.expiry_date IS NULL OR substr(u.expiry_date,1,10) >= ?)
            ORDER BY p.external_identity, p.protocol""", (today,)).fetchall()
    finally:
        conn.close()

    configured: dict[tuple[str, str], int] = {(protocol, identity): 0 for protocol, identity in records}
    if not configured:
        return []

    proc_root = Path(proc_root)
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            if comm == "sshd":
                protocol = "openssh"
            elif comm == "dropbear":
                protocol = "dropbear"
            else:
                continue
            status = (entry / "status").read_text(encoding="ascii", errors="replace")
            uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), None)
            if uid_line is None:
                continue
            uid = int(uid_line.split()[1])  # effective UID
            if uid == 0:
                continue
            username = pwd.getpwuid(uid).pw_name
            key = (protocol, username)
            if key in configured:
                configured[key] += 1
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, KeyError, OSError):
            # Process may exit or change while procfs is being sampled.
            continue

    result: list[dict[str, object]] = []
    for (protocol, username), count in sorted(configured.items(), key=lambda item: (item[0][1], item[0][0])):
        result.append({"username": username, "protocol": protocol, "observed_processes": count})
    return result
