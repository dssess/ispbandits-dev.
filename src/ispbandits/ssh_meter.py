"""Experimental OpenSSH per-session network-byte producer.

Copyright (c) 2026 ISPbandits Dev. All rights reserved.

This module maps a post-authentication OpenSSH worker's effective UID to its
open TCP socket inode, then counts that exact socket tuple in a dedicated,
non-verdict nftables table. It does not use nft meta skuid. Dropbear is not
supported by this producer. The mapping must be validated against the target
OpenSSH build and traffic modes before enabling quota enforcement.
"""
from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import uuid
import datetime as dt
from typing import Any


TABLE = "ispbandits_meter"
TABLE_COMMENT = "Managed by ISPbandits Dev SSH meter"
DEFAULT_STATE = Path("/var/lib/ispbandits-dev/ssh-meter-state.json")
DEFAULT_SNAPSHOT = Path("/run/vpn-ssh-meter/counters.json")
DEFAULT_LOCK = Path("/run/lock/ispbandits-ssh-meter.lock")
STALE_FLOW_SECONDS = 30


class MeterError(RuntimeError):
    """Raised when the experimental meter cannot safely publish a snapshot."""


class MeterUnavailable(MeterError):
    """Raised when a required OS tool or system input is unavailable."""


def _decode_proc_address(value: str, family: int) -> str:
    """Decode the little-endian address representation used by procfs."""
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise MeterError("Malformed address in /proc/net/tcp") from exc
    if family == socket.AF_INET:
        if len(raw) != 4:
            raise MeterError("Malformed IPv4 address in /proc/net/tcp")
        raw = raw[::-1]
    else:
        if len(raw) != 16:
            raise MeterError("Malformed IPv6 address in /proc/net/tcp6")
        raw = b"".join(raw[index:index + 4][::-1] for index in range(0, 16, 4))
    address = ipaddress.ip_address(raw)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)


def _parse_proc_tcp(path: Path, family: int) -> dict[int, dict[str, Any]]:
    """Return established TCP sockets keyed by kernel socket inode."""
    try:
        lines = path.read_text(encoding="ascii").splitlines()[1:]
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MeterUnavailable(f"Cannot read {path}") from exc
    result: dict[int, dict[str, Any]] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "01":  # TCP_ESTABLISHED only
            continue
        try:
            local_hex, local_port_hex = fields[1].split(":", 1)
            remote_hex, remote_port_hex = fields[2].split(":", 1)
            inode = int(fields[9])
            local_port = int(local_port_hex, 16)
            remote_port = int(remote_port_hex, 16)
            if not (1 <= local_port <= 65535 and 1 <= remote_port <= 65535):
                continue
            result[inode] = {
                "family": "ip" if family == socket.AF_INET else "ip6",
                "local_ip": _decode_proc_address(local_hex, family),
                "local_port": local_port,
                "remote_ip": _decode_proc_address(remote_hex, family),
                "remote_port": remote_port,
            }
        except (ValueError, MeterError):
            continue
    return result


def read_proc_tcp(proc_root: Path = Path("/proc")) -> dict[int, dict[str, Any]]:
    """Read both IPv4 and IPv6 TCP tables from the host procfs."""
    sockets = _parse_proc_tcp(proc_root / "net/tcp", socket.AF_INET)
    sockets.update(_parse_proc_tcp(proc_root / "net/tcp6", socket.AF_INET6))
    return sockets


def _effective_uid(status_path: Path) -> int | None:
    try:
        for line in status_path.read_text(encoding="ascii").splitlines():
            if line.startswith("Uid:"):
                values = line.split()[1:]
                return int(values[1]) if len(values) > 1 else int(values[0])
    except (OSError, ValueError):
        return None
    return None


def _process_socket_inodes(pid_dir: Path) -> set[int]:
    result: set[int] = set()
    try:
        fd_dir = pid_dir / "fd"
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match:
                result.add(int(match.group(1)))
    except OSError:
        return result
    return result


def _ssh_ports(sshd_bin: str = "sshd") -> set[int]:
    """Ask OpenSSH for its effective listening ports; never guess port 22."""
    executable = shutil.which(sshd_bin) if os.sep not in sshd_bin else sshd_bin
    if not executable or not Path(executable).is_file():
        raise MeterUnavailable("OpenSSH sshd was not found; SSH metering is unavailable")
    try:
        result = subprocess.run([executable, "-T"], check=False, capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MeterUnavailable("Could not read effective OpenSSH configuration") from exc
    if result.returncode:
        raise MeterUnavailable("OpenSSH rejected its effective configuration query")
    ports = set()
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "port":
            try:
                port = int(value.strip())
            except ValueError:
                continue
            if 1 <= port <= 65535:
                ports.add(port)
    if not ports:
        raise MeterUnavailable("No effective OpenSSH listening port was found")
    return ports


def load_managed_openssh_users(database_path: Path | str) -> dict[int, str]:
    """Resolve enabled managed OpenSSH identities to local UIDs."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(database_path), timeout=10)
        try:
            rows = conn.execute("""SELECT DISTINCT p.protocol,p.external_identity,u.status
                FROM protocol_accounts p JOIN users u ON u.id=p.user_id
                WHERE p.protocol IN ('openssh','dropbear') AND p.enabled=1
                ORDER BY p.protocol,p.external_identity""").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        raise MeterUnavailable("Could not read managed OpenSSH identities from SQLite") from exc
    identities: dict[int, str] = {}
    for row in rows:
        protocol, identity, status = str(row[0]), str(row[1]), str(row[2]).upper()
        if protocol == "dropbear":
            if status == "ACTIVE":
                raise MeterUnavailable("Enabled Dropbear accounts are not supported by the OpenSSH-only meter; shared quota collection is blocked")
            continue
        username = identity
        try:
            uid = pwd.getpwnam(username).pw_uid
        except KeyError:
            if status == "ACTIVE":
                raise MeterUnavailable(f"Active managed OpenSSH account {username!r} has no local system UID")
            continue
        if uid in identities and identities[uid] != username:
            raise MeterError("Two managed usernames resolve to the same system UID")
        identities[uid] = username
    return identities


def discover_flows(proc_root: Path = Path("/proc"),
                   uid_to_username: dict[int, str] | None = None,
                   ssh_ports: set[int] | None = None) -> dict[str, dict[str, Any]]:
    """Map authenticated OpenSSH workers to their established SSH socket tuples.

    The identity comes from the worker's effective UID. The tuple is obtained by
    matching a socket inode in that worker's /proc/<pid>/fd directory to the
    host's /proc/net/tcp tables. This is a prototype mapping, not a cross-build
    guarantee; ambiguous mappings fail closed.
    """
    if uid_to_username is None:
        uid_to_username = {}
    if ssh_ports is None:
        ssh_ports = set()
    sockets = read_proc_tcp(proc_root)
    flows: dict[str, dict[str, Any]] = {}
    by_tuple: dict[tuple[str, str, int, str, int], str] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise MeterUnavailable("Cannot enumerate host processes in procfs") from exc
    for pid_dir in entries:
        if not pid_dir.name.isdigit():
            continue
        try:
            comm = (pid_dir / "comm").read_text(encoding="ascii").strip()
        except OSError:
            continue
        # Only sshd/sshd-session workers are considered; shells are not.
        if comm not in {"sshd", "sshd-session"}:
            continue
        uid = _effective_uid(pid_dir / "status")
        username = uid_to_username.get(uid) if uid is not None else None
        if not username:
            continue
        for inode in _process_socket_inodes(pid_dir):
            info = sockets.get(inode)
            if not info or info["local_port"] not in ssh_ports:
                continue
            key_tuple = (info["family"], info["local_ip"], info["local_port"],
                         info["remote_ip"], info["remote_port"])
            previous = by_tuple.get(key_tuple)
            if previous and previous != username:
                raise MeterError("An SSH socket tuple was mapped to multiple managed users")
            by_tuple[key_tuple] = username
            digest = hashlib.sha256((username + "\0" + "\0".join(map(str, key_tuple))).encode()).hexdigest()[:24]
            flows[digest] = {
                "protocol": "openssh", "identity": username, "family": info["family"],
                "local_ip": info["local_ip"], "local_port": info["local_port"],
                "remote_ip": info["remote_ip"], "remote_port": info["remote_port"],
            }
    return flows


def _nft_run(args: list[str], *, input_text: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
    nft = shutil.which("nft")
    if not nft:
        raise MeterUnavailable("nftables command 'nft' is required for the experimental SSH meter")
    try:
        result = subprocess.run([nft, *args], input=input_text, check=False,
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MeterUnavailable("Could not query/update nftables counters") from exc
    if check and result.returncode:
        raise MeterError("nftables rejected an SSH meter operation")
    return result


def _table_json() -> dict[str, Any] | None:
    result = _nft_run(["-j", "-a", "list", "table", "inet", TABLE], check=False)
    if result.returncode:
        return None
    try:
        doc = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MeterError("nftables returned malformed JSON") from exc
    table_obj = next((item.get("table") for item in doc.get("nftables", []) if item.get("table")), None)
    if not table_obj or table_obj.get("comment") != TABLE_COMMENT:
        raise MeterError(f"The nftables table {TABLE!r} exists but is not owned by ISPbandits")
    chains = {item["chain"].get("name") for item in doc.get("nftables", []) if item.get("chain")}
    if not {"input", "output"}.issubset(chains):
        raise MeterError("ISPbandits nftables meter table is missing a required chain")
    return doc


def ensure_nft_table() -> None:
    if _table_json() is not None:
        return
    script = f'''table inet {TABLE} {{
 comment "{TABLE_COMMENT}"
 chain input {{ type filter hook input priority 10; policy accept; }}
 chain output {{ type filter hook output priority 10; policy accept; }}
}}
'''
    result = _nft_run(["-f", "-"], input_text=script, check=False)
    if result.returncode:
        # A concurrent service may have created the table after our first check.
        if _table_json() is None:
            raise MeterError("Could not create ISPbandits' isolated nftables meter table")


def _rule_comment(flow_id: str, direction: str) -> str:
    return f"ispbd-v1-{flow_id}-{direction}"


def _rules_from_json(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in doc.get("nftables", []):
        rule = item.get("rule")
        if not rule or rule.get("table") != TABLE or rule.get("chain") not in {"input", "output"}:
            continue
        comment = None
        counter = None
        for expr in rule.get("expr", []):
            if isinstance(expr, dict):
                if isinstance(expr.get("comment"), str):
                    comment = expr["comment"]
                if isinstance(expr.get("counter"), dict):
                    counter = expr["counter"]
        if comment and counter:
            result[comment] = {"chain": rule["chain"], "handle": int(rule["handle"]),
                               "packets": int(counter.get("packets", 0)),
                               "bytes": int(counter.get("bytes", 0))}
    return result


def _flow_rule(flow_id: str, flow: dict[str, Any], direction: str) -> str:
    local = ipaddress.ip_address(flow["local_ip"])
    remote = ipaddress.ip_address(flow["remote_ip"])
    family = "ip" if local.version == 4 else "ip6"
    if local.version != remote.version:
        raise MeterError("Local and remote addresses in one SSH socket have different families")
    if direction == "in":
        chain = "input"
        source, sport, destination, dport = remote, flow["remote_port"], local, flow["local_port"]
    else:
        chain = "output"
        source, sport, destination, dport = local, flow["local_port"], remote, flow["remote_port"]
    comment = _rule_comment(flow_id, direction)
    return (f'add rule inet {TABLE} {chain} {family} saddr {source} tcp sport {int(sport)} '
            f'{family} daddr {destination} tcp dport {int(dport)} counter comment "{comment}"')


def _read_state(path: Path, table_exists: bool) -> dict[str, Any]:
    if not path.exists():
        marker = path.with_name(path.name + ".initialized")
        if table_exists or marker.exists():
            raise MeterError("Meter state is missing after initialization; refusing an unsafe baseline")
        return {"version": 1, "generation": str(uuid.uuid4()), "totals": {}, "flows": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeterError("SSH meter state is unreadable; refusing to reset counters") from exc
    if value.get("version") != 1 or not isinstance(value.get("generation"), str):
        raise MeterError("SSH meter state has an unsupported version")
    if not isinstance(value.get("totals"), dict) or not isinstance(value.get("flows"), dict):
        raise MeterError("SSH meter state has an invalid structure")
    return value


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def _publish_snapshot(path: Path, state: dict[str, Any], sampled_at: float,
                      managed_identities: set[str]) -> None:
    counters = []
    for key, value in sorted(state["totals"].items()):
        protocol, identity = key.split("\t", 1)
        if protocol != "openssh" or identity not in managed_identities:
            continue
        counters.append({
            "protocol": protocol,
            "external_identity": identity,
            "generation": state["generation"],
            "upload_bytes": int(value["upload_bytes"]),
            "download_bytes": int(value["download_bytes"]),
        })
    iso_sampled_at = dt.datetime.fromtimestamp(sampled_at, dt.timezone.utc).isoformat(timespec="seconds")
    _atomic_json(path, {"version": 1, "sampled_at": iso_sampled_at,
                        "counters": counters}, mode=0o600)


def collect_snapshot(database_path: Path | str, *, state_path: Path = DEFAULT_STATE,
                     snapshot_path: Path = DEFAULT_SNAPSHOT, proc_root: Path = Path("/proc"),
                     sshd_bin: str = "sshd") -> dict[str, int | str]:
    """Sample OpenSSH flows, update durable totals, and atomically publish JSON."""
    if os.geteuid() != 0:
        raise PermissionError("The experimental SSH meter must run as root")
    DEFAULT_LOCK.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    lock_fd = os.open(DEFAULT_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        table_before = _table_json()
        ensure_nft_table()
        table_exists = table_before is not None
        state = _read_state(state_path, table_exists)
        users = load_managed_openssh_users(database_path)
        for username in users.values():
            state["totals"].setdefault(f"openssh\t{username}",
                                       {"upload_bytes": 0, "download_bytes": 0})
        ports = _ssh_ports(sshd_bin)
        active = discover_flows(proc_root, users, ports)
        table = _table_json()
        rules = _rules_from_json(table or {})

        add_commands = []
        for flow_id, flow in active.items():
            state["flows"].setdefault(flow_id, {
                "protocol": "openssh", "identity": flow["identity"],
                "last_in": 0, "last_out": 0, "last_seen": time.time(),
            })
            for direction in ("in", "out"):
                comment = _rule_comment(flow_id, direction)
                if comment not in rules:
                    add_commands.append(_flow_rule(flow_id, flow, direction))
        if add_commands:
            _nft_run(["-f", "-"], input_text="\n".join(add_commands) + "\n")
            table = _table_json()
            rules = _rules_from_json(table or {})

        now = time.time()
        for flow_id, flow_state in list(state["flows"].items()):
            in_rule = rules.get(_rule_comment(flow_id, "in"))
            out_rule = rules.get(_rule_comment(flow_id, "out"))
            if in_rule and out_rule:
                last_in = int(flow_state.get("last_in", 0))
                last_out = int(flow_state.get("last_out", 0))
                current_in = int(in_rule["bytes"])
                current_out = int(out_rule["bytes"])
                # A reset never subtracts stored bytes; the unobserved interval
                # across a reset remains an explicit accuracy gap in the docs.
                delta_in = current_in - last_in if current_in >= last_in else current_in
                delta_out = current_out - last_out if current_out >= last_out else current_out
                total_key = f"{flow_state['protocol']}\t{flow_state['identity']}"
                totals = state["totals"].setdefault(total_key, {"upload_bytes": 0, "download_bytes": 0})
                totals["upload_bytes"] = int(totals["upload_bytes"]) + delta_in
                totals["download_bytes"] = int(totals["download_bytes"]) + delta_out
                flow_state["last_in"] = current_in
                flow_state["last_out"] = current_out
                if flow_id in active:
                    flow_state["last_seen"] = now
            elif flow_id in active:
                # Missing rules for a live authenticated socket indicate a
                # partial table reset. Reinstalling them will start from zero.
                flow_state["last_in"] = 0
                flow_state["last_out"] = 0
                flow_state["last_seen"] = now

        state["sampled_at"] = now
        marker_path = state_path.with_name(state_path.name + ".initialized")
        if not marker_path.exists():
            _atomic_json(marker_path, {"version": 1, "generation": state["generation"]})
        _atomic_json(state_path, state)
        _publish_snapshot(snapshot_path, state, now, set(users.values()))

        stale_ids = [flow_id for flow_id, value in state["flows"].items()
                     if flow_id not in active and now - float(value.get("last_seen", now)) > STALE_FLOW_SECONDS]
        deletes = []
        for flow_id in stale_ids:
            for direction in ("in", "out"):
                rule = rules.get(_rule_comment(flow_id, direction))
                if rule:
                    deletes.append(f"delete rule inet {TABLE} {rule['chain']} handle {rule['handle']}")
        if deletes:
            _nft_run(["-f", "-"], input_text="\n".join(deletes) + "\n")
        for flow_id in stale_ids:
            state["flows"].pop(flow_id, None)
        if stale_ids:
            _atomic_json(state_path, state)

        return {"generation": state["generation"], "active_flows": len(active),
                "tracked_users": len(state["totals"]), "sampled_at": now}
    finally:
        os.close(lock_fd)
