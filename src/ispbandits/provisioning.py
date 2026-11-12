"""Linux account provisioning for SSH/Dropbear users.

Only customer accounts are created. This module never edits sshd_config,
Dropbear settings, firewall rules, admin accounts, or root authentication.
"""
from __future__ import annotations

import datetime as dt
import os
import pwd
import secrets
import shutil
import subprocess
from pathlib import Path

from . import database


class ProvisioningError(RuntimeError):
    """A safe account provisioning or rollback operation failed."""


def _run(command: list[str], *, input_text: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, input=input_text, text=True,
                                capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError(f"Could not run {Path(command[0]).name}") from exc
    if check and result.returncode != 0:
        # Never surface output from password tools or include a password in errors.
        raise ProvisioningError(f"{Path(command[0]).name} failed with exit code {result.returncode}")
    return result


def create_linux_ssh_account(username: str, expiry_date: str,
                             password: str | None = None) -> str:
    """Create a no-shell system account and set a generated or supplied password.

    The caller must be root. The returned password is shown only by the caller;
    it is never saved in SQLite or logged by this module.
    """
    database.validate_username(username)
    if os.geteuid() != 0:
        raise PermissionError("Creating Linux SSH accounts requires root")
    try:
        expiry = dt.date.fromisoformat(expiry_date)
    except ValueError as exc:
        raise ValueError("Expiry must be YYYY-MM-DD") from exc
    if expiry < dt.date.today():
        raise ValueError("Cannot create an account that is already expired")
    try:
        pwd.getpwnam(username)
    except KeyError:
        pass
    else:
        raise ValueError(f"Linux account already exists: {username}")

    useradd = shutil.which("useradd")
    userdel = shutil.which("userdel")
    chpasswd = shutil.which("chpasswd")
    if not all((useradd, userdel, chpasswd)):
        raise ProvisioningError("Required useradd/userdel/chpasswd utilities were not found")

    password = password or secrets.token_urlsafe(24)
    if not password or any(c in password for c in ":\r\n"):
        raise ValueError("Password must be non-empty and cannot contain colon or newline")

    # No home directory and no interactive shell. SSH port-forwarding-only
    # clients can still authenticate; the server admin's account is untouched.
    _run([useradd, "-M", "-s", "/bin/false", "-e", expiry.isoformat(), username])
    try:
        _run([chpasswd], input_text=f"{username}:{password}\n")
    except Exception:
        _run([userdel, "-f", username], check=False)
        raise
    return password


def delete_linux_ssh_account(username: str) -> None:
    """Terminate sessions and remove a managed no-home customer account."""
    database.validate_username(username)
    if os.geteuid() != 0:
        raise PermissionError("Removing Linux SSH accounts requires root")
    userdel = shutil.which("userdel")
    pkill = shutil.which("pkill")
    if not userdel:
        raise ProvisioningError("userdel was not found")
    if pkill:
        _run([pkill, "-KILL", "-u", username], check=False)
    _run([userdel, "-f", username], check=False)


def expire_linux_ssh_account(username: str) -> None:
    """Lock a managed user's password and terminate its active processes."""
    database.validate_username(username)
    if os.geteuid() != 0:
        raise PermissionError("Expiring Linux SSH accounts requires root")
    try:
        pwd.getpwnam(username)
    except KeyError:
        return
    usermod = shutil.which("usermod")
    if not usermod:
        raise ProvisioningError("usermod was not found")
    _run([usermod, "-L", username])
    pkill = shutil.which("pkill")
    if pkill:
        _run([pkill, "-KILL", "-u", username], check=False)


def create_managed_account(database_path: Path | str, username: str, expiry_date: str,
                           max_logins: int = 2,
                           protocols: tuple[str, ...] = ("openssh", "vless", "trojan")) -> dict[str, object]:
    """Create one account and selected protocol identities with rollback on error."""
    allowed = {"openssh", "dropbear", "vless", "trojan"}
    if not protocols or len(set(protocols)) != len(protocols) or set(protocols) - allowed:
        raise ValueError("Select one or more unique protocols from openssh, dropbear, vless, trojan")
    ssh_selected = bool(set(protocols) & {"openssh", "dropbear"})
    ssh_password = None
    db_created = False
    linux_created = False
    credentials: list[dict[str, str]] = []

    database.initialize(database_path)
    if ssh_selected:
        ssh_password = create_linux_ssh_account(username, expiry_date)
        linux_created = True
    try:
        ssh_protocols = tuple(p for p in protocols if p in {"openssh", "dropbear"})
        database.add_user(database_path, username, expiry_date, max_logins, 0, ssh_protocols)
        db_created = True
        for protocol in protocols:
            if protocol in {"vless", "trojan"}:
                credentials.append(database.create_xray_protocol(database_path, username, protocol))
        return {"username": username, "expiry_date": expiry_date,
                "ssh_password": ssh_password, "xray_credentials": credentials}
    except Exception:
        if db_created:
            database.delete_user_record(database_path, username)
        if linux_created:
            delete_linux_ssh_account(username)
        raise
