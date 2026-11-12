"""Generate a root-protected Xray VLESS/Trojan TLS configuration.

Configuration is assembled from managed database identities. It is never printed
because it contains Trojan passwords and VLESS UUIDs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import stat
import subprocess
import tempfile
from typing import Any


class XrayConfigError(RuntimeError):
    """Raised when inputs cannot produce a safe, valid Xray configuration."""


def _accounts(database: Path | str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    conn = sqlite3.connect(str(database), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""SELECT u.username,u.status,u.expiry_date,
                p.protocol,p.external_identity,p.enabled,s.secret
            FROM protocol_accounts p
            JOIN users u ON u.id=p.user_id
            LEFT JOIN protocol_secrets s ON s.user_id=p.user_id AND s.protocol=p.protocol
            WHERE p.protocol IN ('vless','trojan')
            ORDER BY u.username,p.protocol""").fetchall()
    except sqlite3.Error as exc:
        raise XrayConfigError("Database schema is not initialized") from exc
    finally:
        conn.close()

    today = dt.date.today()
    vless: list[dict[str, str]] = []
    trojan: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not row["enabled"] or str(row["status"]).upper() != "ACTIVE":
            continue
        try:
            expiry = dt.date.fromisoformat(str(row["expiry_date"])[:10])
        except ValueError as exc:
            raise XrayConfigError(f"Invalid expiry date for {row['username']}") from exc
        if expiry < today:
            continue
        identity = str(row["external_identity"])
        if not identity or identity in seen:
            raise XrayConfigError("Xray stats identities must be non-empty and unique")
        seen.add(identity)
        secret = row["secret"]
        if not isinstance(secret, str) or not secret:
            raise XrayConfigError(f"Missing stored credential for {row['username']} {row['protocol']}")
        if row["protocol"] == "vless":
            vless.append({"id": secret, "email": identity, "level": 0})
        else:
            trojan.append({"password": secret, "email": identity, "level": 0})
    return vless, trojan


def _validate_tls(cert_file: Path | str, key_file: Path | str) -> tuple[str, str]:
    # Preserve symlink paths (notably /tls/current/...) so atomic certificate
    # renewal updates are picked up on the next service restart.
    cert = Path(os.path.abspath(os.fspath(cert_file)))
    key = Path(os.path.abspath(os.fspath(key_file)))
    if not cert.is_file() or not key.is_file():
        raise XrayConfigError("TLS certificate and private key must be regular files")
    key_stat = key.stat()
    if key_stat.st_mode & (stat.S_IROTH | stat.S_IWOTH):
        raise XrayConfigError("TLS private key must not be world-readable or world-writable")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
    except (OSError, ssl.SSLError) as exc:
        raise XrayConfigError("TLS certificate/key pair is unreadable or mismatched") from exc
    return str(cert), str(key)


def build_config(database: Path | str, cert_file: Path | str, key_file: Path | str,
                 vless_port: int = 443, trojan_port: int = 8443,
                 api_port: int = 10085) -> dict[str, Any]:
    cert, key = _validate_tls(cert_file, key_file)
    for label, port in (("VLESS", vless_port), ("Trojan", trojan_port), ("API", api_port)):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise XrayConfigError(f"{label} port must be an integer from 1 to 65535")
    if len({vless_port, trojan_port, api_port}) != 3:
        raise XrayConfigError("VLESS, Trojan, and API ports must be distinct")

    vless_users, trojan_users = _accounts(database)
    tls = {"network": "tcp", "security": "tls", "tlsSettings": {
        "certificates": [{"certificateFile": cert, "keyFile": key}]
    }}
    return {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "listen": f"127.0.0.1:{api_port}",
                "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {"levels": {"0": {
            "statsUserUplink": True,
            "statsUserDownlink": True,
            "statsUserOnline": True,
        }}, "system": {
            "statsInboundUplink": True,
            "statsInboundDownlink": True,
        }},
        "inbounds": [
            {"listen": "0.0.0.0", "port": vless_port, "protocol": "vless",
             "tag": "vless-tls", "settings": {"users": vless_users, "decryption": "none"},
             "streamSettings": {**tls, "tlsSettings": {"certificates": [
                 {"certificateFile": cert, "keyFile": key}
             ]}}},
            {"listen": "0.0.0.0", "port": trojan_port, "protocol": "trojan",
             "tag": "trojan-tls", "settings": {"users": trojan_users},
             "streamSettings": {**tls, "tlsSettings": {"certificates": [
                 {"certificateFile": cert, "keyFile": key}
             ]}}},
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }


def validate_with_xray(config: dict[str, Any], xray_bin: str) -> None:
    executable = shutil.which(xray_bin) if os.sep not in xray_bin else xray_bin
    if not executable or not Path(executable).is_file():
        raise XrayConfigError("Xray executable was not found; install Xray or pass --xray-bin")
    with tempfile.TemporaryDirectory(prefix="ispbandits-xray-check-") as directory:
        config_path = Path(directory) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(config_path, 0o600)
        try:
            result = subprocess.run([executable, "run", "-test", "-config", str(config_path)],
                                    check=False, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise XrayConfigError("Could not run Xray configuration test") from exc
        if result.returncode != 0:
            raise XrayConfigError("Xray rejected the generated configuration; see local Xray logs")


def write_config(config: dict[str, Any], output: Path | str) -> Path:
    """Atomically write config without exposing credentials to other users."""
    path = Path(output)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        import grp
        group_id = grp.getgrnam("xray").gr_gid
        os.chown(path.parent, 0, group_id)
        os.chmod(path.parent, 0o750)
    except KeyError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o640)
        try:
            import grp
            group_id = grp.getgrnam("xray").gr_gid
            os.chown(path, 0, group_id)
        except KeyError:
            # Keep current owner/group if the official Xray service account is
            # not installed yet. Install the service before starting Xray.
            pass
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return path


def generate(database: Path | str, cert_file: Path | str, key_file: Path | str,
             output: Path | str, vless_port: int = 443, trojan_port: int = 8443,
             api_port: int = 10085, xray_bin: str | None = None) -> Path:
    config = build_config(database, cert_file, key_file, vless_port, trojan_port, api_port)
    if xray_bin:
        validate_with_xray(config, xray_bin)
    return write_config(config, output)


def apply_config(config: dict[str, Any], output: Path | str, xray_bin: str,
                 service: str = "ispbandits-xray.service") -> Path:
    """Validate, atomically install, and restart Xray; restore config on failure."""
    if os.geteuid() != 0:
        raise PermissionError("Applying Xray config requires root")
    validate_with_xray(config, xray_bin)
    path = Path(output)
    old_content = path.read_bytes() if path.is_file() else None
    old_stat = path.stat() if path.is_file() else None
    old_mode = stat.S_IMODE(old_stat.st_mode) if old_stat else 0o640
    write_config(config, path)
    try:
        result = subprocess.run(["systemctl", "restart", service], check=False,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = None
        reason = "systemd restart command failed"
    else:
        reason = f"systemd restart failed with exit code {result.returncode}"
    if result is not None and result.returncode == 0:
        return path

    try:
        if old_content is None:
            path.unlink(missing_ok=True)
        else:
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=path.parent)
            temp_path = Path(temporary)
            try:
                os.fchmod(fd, old_mode)
                if old_stat is not None:
                    os.fchown(fd, old_stat.st_uid, old_stat.st_gid)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(old_content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "restart", service], check=False,
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise XrayConfigError("Xray restart failed and automatic config rollback also failed")
    raise XrayConfigError(f"Xray restart failed; previous config was restored ({reason})")
