"""ISPbandits Dev administration CLI."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import sqlite3
import sys
import shutil
import subprocess
import tarfile
from . import database, backup
from . import collector
from . import sessions
from . import xray_config
from . import provisioning
from . import ssh_meter

NOTICE = "© 2026 ISPbandits Dev. All rights reserved. Proprietary software."


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ispbandits", description="ISPbandits Dev VPS management")
    p.add_argument("--db", default=str(database.DEFAULT_DB), help="SQLite path (default: %(default)s)")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("menu", help="open the interactive administration menu")
    sub.add_parser("about", help="show publisher and copyright information")
    sub.add_parser("init", help="initialize or migrate the SQLite database")
    add = sub.add_parser("user-add", help="add a management account record (does not provision a Linux/Xray user yet)")
    add.add_argument("username")
    add.add_argument("--expires", required=True, help="expiry date YYYY-MM-DD")
    add.add_argument("--max-logins", type=int, default=2)
    add.add_argument("--limit-bytes", type=int, default=0)
    create = sub.add_parser("account-create", help="create a Linux SSH account and/or Xray protocol identities")
    create.add_argument("username")
    create.add_argument("--expires", required=True, help="expiry date YYYY-MM-DD")
    create.add_argument("--limit-gb", type=int, default=0,
                        help="shared lifetime quota in decimal GB (0 = unlimited); requires the active experimental meter and a fresh snapshot")
    create.add_argument("--protocols", nargs="+", choices=("openssh", "vless", "trojan"),
                        default=("openssh", "vless", "trojan"))
    sub.add_parser("account-list", help="list accounts and provisioned protocol state without secrets")
    show = sub.add_parser("account-show", help="show one account, protocol state, and traffic breakdown without secrets")
    show.add_argument("username")
    renew = sub.add_parser("renew", help="set an account expiry date without clearing disabled/quota state")
    renew.add_argument("username")
    renew.add_argument("--expires", required=True, help="new expiry date YYYY-MM-DD")
    disable = sub.add_parser("disable", help="revoke all enabled protocol access for an account")
    disable.add_argument("username")
    enable = sub.add_parser("enable", help="reactivate an eligible disabled or renewed account")
    enable.add_argument("username")
    delete = sub.add_parser("account-delete", help="permanently remove an account after revoking access")
    delete.add_argument("username")
    delete.add_argument("--yes", action="store_true", help="confirm irreversible account and usage deletion")
    reset = sub.add_parser("ssh-password-reset", help="set a new SSH password for a managed OpenSSH account")
    reset.add_argument("username")
    quota = sub.add_parser("quota-set", help="set an account's shared lifetime quota in decimal GB")
    quota.add_argument("username")
    quota.add_argument("--limit-gb", type=int, required=True,
                       help="decimal GB (1 GB = 1,000,000,000 bytes); 0 = unlimited")
    link = sub.add_parser("link-protocol", help="link an external protocol identity to an account")
    link.add_argument("username")
    link.add_argument("protocol", choices=sorted(database.PROTOCOLS))
    link.add_argument("identity", help="SSH username or unique Xray stats email/tag; not a secret")
    link.add_argument("--credential-ref", help="reference to separately protected credentials, never the credential itself")
    sub.add_parser("usage", help="show stored per-account usage totals")
    sub.add_parser("online", help="show approximate online SSH/Dropbear process counts (not device counts)")
    sub.add_parser("expire", help="expire due accounts, lock Linux users, and reconcile active Xray config")
    xp = sub.add_parser("xray-provision", help="create a VLESS UUID or Trojan password for an existing account")
    xp.add_argument("username")
    xp.add_argument("protocol", choices=("vless", "trojan"))
    xp.add_argument("--rotate", action="store_true", help="replace an existing protocol credential")
    xc = sub.add_parser("xray-config", help="generate a protected, validated Xray VLESS/Trojan config")
    xc.add_argument("--cert-file", required=True, help="TLS certificate chain path")
    xc.add_argument("--key-file", required=True, help="TLS private key path")
    xc.add_argument("--output", default="/etc/ispbandits-dev/xray.json")
    xc.add_argument("--vless-port", type=int, default=443)
    xc.add_argument("--trojan-port", type=int, default=8443)
    xc.add_argument("--api-port", type=int, default=10085)
    xc.add_argument("--xray-bin", default=shutil.which("xray") or "/usr/local/bin/xray")
    xc.add_argument("--check-only", action="store_true", help="validate without writing the secret-bearing config")
    xc.add_argument("--apply", action="store_true", help="install config and restart ispbandits-xray.service with rollback on failure")
    b = sub.add_parser("backup", help="create an online SQLite backup archive")
    b.add_argument("--dir", default=None, help="backup directory (default: sibling backups/)")
    r = sub.add_parser("restore", help="validate and restore a backup; preserves a rollback backup")
    r.add_argument("archive")
    r.add_argument("--dir", default=None)
    v = sub.add_parser("verify", help="run SQLite integrity check")
    c = sub.add_parser("collect", help="collect Xray and SSH-meter counters atomically")
    c.add_argument("--dry-run", action="store_true", help="roll back after validating inputs")
    c.add_argument("--xray-only", action="store_true", help="collect VLESS/Trojan until the SSH network meter is installed")
    c.add_argument("--enforce-quotas", action="store_true",
                   help="after a full fresh Xray+SSH collection, disable accounts at their lifetime quota")
    meter = sub.add_parser("ssh-meter", help="sample the experimental OpenSSH per-session nftables meter")
    meter.add_argument("--state", default=str(ssh_meter.DEFAULT_STATE))
    meter.add_argument("--snapshot", default=str(ssh_meter.DEFAULT_SNAPSHOT))
    meter.add_argument("--sshd-bin", default="sshd")
    return p


def _quota_revoke(path: Path, usernames: list[str]) -> None:
    """Lock Linux sessions and remove quota-exceeded users from active Xray."""
    errors = []
    for username in usernames:
        try:
            if _has_managed_linux_ssh(path, username):
                provisioning.expire_linux_ssh_account(username)
        except Exception as exc:
            errors.append(f"{username}: {exc}")
    try:
        _reconcile_quota_xray(path)
    except Exception as exc:
        subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                       check=False, capture_output=True)
        raise RuntimeError("Quota-exceeded accounts were recorded, but Xray reconciliation failed; Xray was stopped fail-closed") from exc
    if errors:
        raise RuntimeError("Some SSH accounts could not be locked: " + "; ".join(errors))


def _reconcile_quota_xray(path: Path) -> None:
    _reconcile_xray_if_active(path)


def _quota_reactivate(path: Path, username: str, expiry_date: str) -> None:
    if _has_managed_linux_ssh(path, username):
        provisioning.reactivate_linux_ssh_account(username, expiry_date)
    _reconcile_xray_if_active(path)


def _has_managed_linux_ssh(path: Path, username: str) -> bool:
    try:
        return _managed_ssh_identity(database.get_account(path, username)) is not None
    except ValueError:
        return False


def _revoke_external_access(path: Path, username: str) -> None:
    account = database.get_account(path, username)
    ssh_identity = _managed_ssh_identity(account)
    if ssh_identity:
        provisioning.expire_linux_ssh_account(ssh_identity)
    try:
        _reconcile_xray_if_active(path)
    except Exception as exc:
        subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                       check=False, capture_output=True)
        raise RuntimeError("Account state changed, but Xray reconciliation failed; Xray was stopped fail-closed") from exc


def _activate_external_access(path: Path, username: str, expiry_date: str) -> None:
    account = database.get_account(path, username)
    ssh_identity = _managed_ssh_identity(account)
    try:
        if ssh_identity:
            provisioning.reactivate_linux_ssh_account(ssh_identity, expiry_date)
        _reconcile_xray_if_active(path)
    except Exception as exc:
        try:
            database.disable_user(path, username)
            if ssh_identity:
                provisioning.expire_linux_ssh_account(ssh_identity)
            _reconcile_xray_if_active(path)
        except Exception:
            subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                           check=False, capture_output=True)
        raise RuntimeError("Activation failed; the account was returned to a disabled state where possible") from exc


def _ssh_meter_timer_active() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "ispbandits-ssh-meter.timer"],
                              check=False, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _xray_service_active() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "ispbandits-xray.service"],
                              check=False, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ssh_meter_ready(path: Path) -> bool:
    if not _ssh_meter_timer_active() or not _xray_service_active():
        return False
    try:
        ssh_meter.load_managed_openssh_users(path)
        collector.read_ssh_samples()
        return True
    except (OSError, RuntimeError, ValueError, KeyError):
        return False


def _seed_new_account_checkpoints(path: Path, username: str, xray_active: bool,
                                  meter_active: bool) -> int:
    if not xray_active and not meter_active:
        return 0
    xray_samples = collector.read_xray_samples() if xray_active else []
    xray_gen = None
    if xray_active:
        xray_gen = xray_samples[0].generation if xray_samples else collector.xray_generation()
    ssh_samples = []
    if meter_active:
        ssh_meter.collect_snapshot(path)
        ssh_samples = collector.read_ssh_samples()
    return collector.seed_account_checkpoints(path, username, xray_samples,
                                              ssh_samples, xray_gen)


def _managed_ssh_identity(account: dict[str, object]) -> str | None:
    for protocol in account["protocols"]:
        if protocol["protocol"] == "openssh":
            return str(protocol["identity"])
    return None


def _reconcile_xray_if_active(path: Path) -> bool:
    active = _xray_service_active()
    output = Path("/etc/ispbandits-dev/xray.json")
    if not active and not output.exists():
        return False
    config = xray_config.build_config(path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                                     "/etc/ispbandits-dev/tls/current/privkey.pem")
    expected = json.dumps(config, indent=2, sort_keys=True) + "\n"
    try:
        current = output.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != expected:
        if active:
            xray_config.apply_config(config, output, "/usr/local/bin/xray")
        else:
            xray_config.write_config(config, output)
    return active


def _install_info() -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        for line in Path("/opt/ispbandits-dev/INSTALL_INFO").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key] = value
    except OSError:
        pass
    return info


def _configured_ssh_ports() -> list[int]:
    sshd = shutil.which("sshd")
    if not sshd:
        return []
    try:
        result = subprocess.run([sshd, "-T"], check=False, capture_output=True,
                                text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
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
    return sorted(ports)


def _require_working_ssh_login(username: str) -> list[int]:
    """Refuse to hand out unusable password/tunnel accounts; never edit sshd config."""
    sshd = shutil.which("sshd")
    if not sshd:
        raise RuntimeError("OpenSSH server (sshd) is not installed; ISPbandits will not alter the administrator's SSH service")
    try:
        result = subprocess.run([sshd, "-T", "-C",
                                 f"user={username},host=localhost,addr=127.0.0.1"],
                                check=False, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not inspect effective sshd settings; refusing to create an SSH account") from exc
    if result.returncode != 0:
        raise RuntimeError("sshd rejected its effective configuration; fix sshd before creating SSH accounts")
    settings = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key:
            settings[key.lower()] = value.strip().lower()
    if settings.get("passwordauthentication") != "yes":
        raise RuntimeError("Effective sshd PasswordAuthentication is not enabled for this account; no SSH config was changed")
    if settings.get("allowtcpforwarding") != "yes" or settings.get("disableforwarding") == "yes":
        raise RuntimeError("Effective sshd settings do not permit TCP forwarding for this account; no SSH config was changed")
    auth_methods = settings.get("authenticationmethods", "any")
    if auth_methods not in {"any", "password"}:
        raise RuntimeError("Effective sshd AuthenticationMethods requires more than the generated password; no SSH config was changed")
    active = False
    for unit in ("ssh.service", "sshd.service", "ssh.socket"):
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False,
                              capture_output=True, timeout=4).returncode == 0:
                active = True
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    if not active:
        raise RuntimeError("OpenSSH daemon/socket is not active; start the existing ssh service before creating SSH users")
    ports = []
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "port":
            try:
                port = int(value.strip())
            except ValueError:
                continue
            if 1 <= port <= 65535:
                ports.append(port)
    return sorted(set(ports))


def _print_connection_details(username: str, credentials: list[dict[str, str]],
                              has_ssh: bool = False,
                              ssh_ports: list[int] | None = None) -> None:
    info = _install_info()
    host = info.get("domain", "").strip()
    ports = ssh_ports if ssh_ports is not None else _configured_ssh_ports()
    if has_ssh:
        if host and ports:
            print(f"SSH endpoint: {host}; configured sshd port(s): {','.join(map(str, ports))} (tunnel-only account)")
        else:
            print("SSH endpoint: check this VPS sshd listener and firewall; ISPbandits did not change sshd.")
    for item in credentials:
        protocol = item["protocol"]
        if protocol not in {"vless", "trojan"}:
            continue
        if not host:
            print(f"{protocol.upper()} endpoint: hostname is not configured; no import link can be built.")
            continue
        if protocol == "vless":
            port = 443
            link = (f"vless://{item['credential']}@{host}:{port}?encryption=none&security=tls"
                    f"&sni={host}&type=tcp#{username}-vless")
        else:
            port = 8443
            from urllib.parse import quote
            secret = quote(item["credential"], safe="")
            link = (f"trojan://{secret}@{host}:{port}?security=tls&sni={host}"
                    f"&type=tcp#{username}-trojan")
        print(f"{protocol.upper()} import link (secret; shown once): {link}")


def _rollback_unpublished_account(path: Path, username: str, ssh_password: object,
                                  xray_managed: bool) -> None:
    errors = []
    try:
        database.delete_user_record(path, username)
    except Exception as exc:
        errors.append(exc)
    if ssh_password:
        try:
            provisioning.delete_linux_ssh_account(username)
        except Exception as exc:
            errors.append(exc)
    if xray_managed:
        try:
            if errors:
                raise RuntimeError("Account record or Linux user cleanup failed")
            config = xray_config.build_config(path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                                              "/etc/ispbandits-dev/tls/current/privkey.pem")
            if _xray_service_active():
                xray_config.apply_config(config, "/etc/ispbandits-dev/xray.json", "/usr/local/bin/xray")
            else:
                xray_config.write_config(config, "/etc/ispbandits-dev/xray.json")
        except Exception:
            subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                           check=False, capture_output=True)
            raise
    if errors:
        raise RuntimeError("Account record or Linux user cleanup failed") from errors[0]


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    path = Path(args.db)
    if args.command is None or args.command == "menu":
        if args.command is None and not sys.stdin.isatty():
            parser().print_help()
            return 2
        from .menu import run_menu
        return run_menu(path)
    try:
        if args.command == "about":
            print(f"ISPbandits Dev {NOTICE}")
        elif args.command == "init":
            database.initialize(path)
            print(f"Database ready: {path}")
        elif args.command == "user-add":
            user_id = database.add_user(path, args.username, args.expires, args.max_logins, args.limit_bytes)
            print(f"Created account record id={user_id} username={args.username}; protocol provisioning is not yet enabled.")
        elif args.command == "account-create":
            if os.geteuid() != 0:
                raise PermissionError("Creating managed system accounts requires root")
            if args.limit_gb < 0:
                raise ValueError("--limit-gb must be zero or greater")
            if args.limit_gb and not _ssh_meter_ready(path):
                raise RuntimeError("A shared lifetime quota requires an active experimental SSH meter and a fresh valid snapshot")
            if _ssh_meter_timer_active() and "dropbear" in args.protocols:
                raise ValueError("Dropbear is unsupported by the experimental per-user meter; do not provision it while SSH metering is active")
            ssh_ports = _require_working_ssh_login(args.username) if "openssh" in args.protocols else []
            result = provisioning.create_managed_account(path, args.username, args.expires,
                                                          protocols=tuple(args.protocols),
                                                          data_limit=args.limit_gb * 1_000_000_000)
            xray_activated = False
            xray_active = _xray_service_active()
            xray_managed = xray_active or Path("/etc/ispbandits-dev/xray.json").exists()
            if result["xray_credentials"]:
                try:
                    xray_activated = _reconcile_xray_if_active(path)
                except Exception as exc:
                    _rollback_unpublished_account(path, args.username, result["ssh_password"], xray_managed)
                    raise RuntimeError("Account creation rolled back because Xray config activation failed") from exc
            meter_active = _ssh_meter_timer_active()
            if xray_active or meter_active:
                try:
                    _seed_new_account_checkpoints(path, args.username, xray_active, meter_active)
                except Exception as exc:
                    try:
                        _rollback_unpublished_account(path, args.username, result["ssh_password"], xray_managed)
                    except Exception as rollback_exc:
                        raise RuntimeError("Counter baseline failed and account rollback was incomplete; Xray was stopped if its configuration could not be reconciled") from rollback_exc
                    raise RuntimeError("Account creation rolled back because initial traffic-counter checkpoints could not be established") from exc
            print(f"Created ISPbandits account: {result['username']} (expires {result['expiry_date']})")
            if args.limit_gb:
                print(f"Shared lifetime quota: {args.limit_gb} GB (experimental SSH+Xray enforcement)")
            if result["ssh_password"]:
                print(f"SSH password (shown once; store securely): {result['ssh_password']}")
                print("SSH uses a no-home /bin/false account; admin SSH configuration was not changed.")
            for credential in result["xray_credentials"]:
                print(f"{credential['protocol'].upper()} stats identity: {credential['identity']}")
                print(f"{credential['protocol'].upper()} client credential (shown once): {credential['credential']}")
            _print_connection_details(args.username, result["xray_credentials"],
                                      bool(result["ssh_password"]), ssh_ports)
            if result["xray_credentials"]:
                if xray_activated:
                    print("VLESS/Trojan identities were added to the validated Xray config and activated.")
                else:
                    print("Xray service is not active; credentials are stored but not yet accepting connections.")
        elif args.command == "account-list":
            rows = database.list_accounts(path)
            if not rows:
                print("No accounts.")
            else:
                print(f"{'USERNAME':<20} {'STATUS':<16} {'EXPIRES':<12} {'PROTOCOLS':<28} {'QUOTA BYTES':>12}")
                for row in rows:
                    protocols = ",".join(p["protocol"] for p in row["protocols"]) or "none"
                    limit = int(row["data_limit"])
                    print(f"{row['username']:<20} {row['status']:<16} {row['expiry_date'][:10]:<12} {protocols:<28} {limit:>12}")
        elif args.command == "account-show":
            row = database.get_account(path, args.username)
            print(f"Account: {row['username']} | status={row['status']} | expiry={row['expiry_date'][:10]}")
            print(f"Lifetime quota: {row['data_limit']} bytes (0 means unlimited); max logins={row['max_logins']}")
            print("Protocols (credentials are never shown by account-show):")
            if not row["protocols"]:
                print("  none")
            for protocol in row["protocols"]:
                enabled = "enabled" if protocol["enabled"] else "disabled"
                provisioned = "credential stored" if protocol["credential_provisioned"] else "no managed secret"
                print(f"  {protocol['protocol']}: identity={protocol['identity']} [{enabled}; {provisioned}]")
            print("Usage by protocol (bytes):")
            if not row["traffic"]:
                print("  no counters collected")
            for traffic in row["traffic"]:
                print(f"  {traffic['protocol']}: upload={traffic['upload_bytes']} download={traffic['download_bytes']} legacy={traffic['legacy_bytes']} updated={traffic['updated_at']}")
        elif args.command == "renew":
            if os.geteuid() != 0:
                raise PermissionError("Renewing managed accounts requires root")
            old = database.get_account(path, args.username)
            result = database.renew_user(path, args.username, args.expires)
            if result["status"] == "ACTIVE":
                ssh_identity = _managed_ssh_identity(old)
                if ssh_identity:
                    provisioning.reactivate_linux_ssh_account(ssh_identity, args.expires)
                _reconcile_xray_if_active(path)
                print(f"Renewed {args.username} through {args.expires}; access remains ACTIVE.")
            else:
                print(f"Renewed expiry for {args.username} through {args.expires}; status remains {result['status']}.")
                if result["status"] == "EXPIRED":
                    print("After payment/authorization, run `ispbandits enable USER` to explicitly restore access.")
        elif args.command == "disable":
            if os.geteuid() != 0:
                raise PermissionError("Disabling managed accounts requires root")
            result = database.disable_user(path, args.username)
            _revoke_external_access(path, args.username)
            print(f"Account {args.username} is {result['status']}; linked OpenSSH sessions were terminated and Xray was reconciled where active.")
        elif args.command == "enable":
            if os.geteuid() != 0:
                raise PermissionError("Enabling managed accounts requires root")
            result = database.enable_user(path, args.username)
            _activate_external_access(path, args.username, str(result["expiry_date"]))
            print(f"Account {args.username} is ACTIVE through {result['expiry_date']}.")
        elif args.command == "account-delete":
            if os.geteuid() != 0:
                raise PermissionError("Deleting managed accounts requires root")
            if not args.yes:
                raise ValueError("Permanent deletion requires --yes; first make a backup if the records may be needed")
            account = database.get_account(path, args.username)
            ssh_identity = _managed_ssh_identity(account)
            database.disable_user(path, args.username)
            if ssh_identity:
                provisioning.expire_linux_ssh_account(ssh_identity)
            try:
                _reconcile_xray_if_active(path)
            except Exception as exc:
                subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                               check=False, capture_output=True)
                raise RuntimeError("Account is disabled but Xray reconciliation failed; Xray was stopped; record retained") from exc
            database.delete_user_record(path, args.username)
            if ssh_identity:
                provisioning.delete_linux_ssh_account(ssh_identity)
            print(f"Permanently removed {args.username} and its account/usage records.")
        elif args.command == "ssh-password-reset":
            if os.geteuid() != 0:
                raise PermissionError("Resetting managed SSH passwords requires root")
            account = database.get_account(path, args.username)
            ssh_identity = _managed_ssh_identity(account)
            if not ssh_identity:
                raise ValueError("This account has no managed OpenSSH identity")
            password = provisioning.reset_linux_ssh_password(ssh_identity)
            print(f"New OpenSSH password for {args.username} (shown once; store securely): {password}")
        elif args.command == "link-protocol":
            if _ssh_meter_timer_active() and args.protocol == "dropbear":
                raise ValueError("Dropbear is unsupported by the experimental per-user meter")
            if _ssh_meter_timer_active() and args.protocol == "vmess":
                raise ValueError("VMess is not part of this alpha's managed Xray configuration and cannot be covered by shared quota enforcement")
            if _ssh_meter_timer_active() and args.protocol == "openssh":
                if args.identity != args.username:
                    raise ValueError("Experimental OpenSSH metering requires the managed Linux username as its protocol identity")
                try:
                    pwd.getpwnam(args.identity)
                except KeyError as exc:
                    raise ValueError("Experimental OpenSSH metering requires an existing Linux account; use account-create") from exc
            database.link_protocol(path, args.username, args.protocol, args.identity, args.credential_ref)
            print(f"Linked {args.protocol} identity for {args.username}.")
        elif args.command == "quota-set":
            if os.geteuid() != 0:
                raise PermissionError("Changing account quotas requires root")
            if args.limit_gb < 0:
                raise ValueError("--limit-gb must be zero or greater")
            if args.limit_gb and not _ssh_meter_ready(path):
                raise RuntimeError("A shared lifetime quota requires an active experimental SSH meter and a fresh valid snapshot")
            account = database.get_account(path, args.username)
            protocols = [(str(item["protocol"]), str(item["identity"]))
                         for item in account["protocols"]]
            must_validate_protocols = bool(args.limit_gb) or account["status"] == "QUOTA_EXCEEDED"
            unsupported = sorted({protocol for protocol, _identity in protocols} -
                                 {"openssh", "vless", "trojan"})
            if must_validate_protocols and unsupported:
                raise ValueError("Shared quota is not supported for this account's linked protocol(s): " + ", ".join(unsupported))
            if must_validate_protocols:
                for protocol, identity in protocols:
                    if protocol == "openssh":
                        if identity != args.username:
                            raise ValueError("Shared quota requires OpenSSH identity to match the managed Linux username")
                        try:
                            pwd.getpwnam(identity)
                        except KeyError as exc:
                            raise ValueError("Shared quota requires an existing managed Linux OpenSSH account") from exc
            result = database.set_data_limit(path, args.username, args.limit_gb * 1_000_000_000)
            if result["status"] == "QUOTA_EXCEEDED":
                _quota_revoke(path, [args.username])
            elif result["old_status"] == "QUOTA_EXCEEDED" and result["status"] == "ACTIVE":
                try:
                    _quota_reactivate(path, args.username, str(result["expiry_date"]))
                except Exception as exc:
                    database.restore_quota_lock(path, args.username, int(result["old_limit_bytes"]))
                    revoke_error = None
                    try:
                        _quota_revoke(path, [args.username])
                    except Exception as cleanup_exc:
                        revoke_error = cleanup_exc
                    if revoke_error:
                        raise RuntimeError(
                            "Quota lift failed and the database lock was restored, but external revocation needs operator verification: "
                            + str(revoke_error)) from exc
                    raise RuntimeError("Quota lift failed; the previous cap and external protocol lock were restored") from exc
            print(f"Quota for {args.username}: {args.limit_gb} GB lifetime; measured={result['measured_bytes']} bytes; status={result['status']}")
        elif args.command == "usage":
            for row in database.list_usage(path):
                quota = "unlimited" if not row["data_limit"] else f"{row['measured_bytes']}/{row['data_limit']} bytes"
                print(f"{row['username']:<20} {row['status']:<14} up={row['upload_bytes']} down={row['download_bytes']} measured={row['measured_bytes']} legacy={row['legacy_bytes']} quota={quota} expires={row['expiry_date']}")
        elif args.command == "online":
            rows = sessions.online_ssh_processes(path)
            print("Online SSH/Dropbear status — observed daemon processes; approximate, not unique device counts")
            print(f"Sampled at: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
            if not rows:
                print("No enabled SSH/Dropbear protocol accounts found.")
            else:
                print(f"{'USERNAME':<20} {'PROTOCOL':<10} {'OBSERVED PROCESSES':>18}")
                for row in rows:
                    print(f"{row['username']:<20} {row['protocol']:<10} {row['observed_processes']:>18}")
        elif args.command == "expire":
            if os.geteuid() != 0:
                raise PermissionError("Account expiry maintenance requires root")
            expired = database.expire_users(path)
            errors = []
            inactive = database.inactive_usernames(path)
            for username in inactive:
                try:
                    account = database.get_account(path, username)
                    ssh_identity = _managed_ssh_identity(account)
                    if ssh_identity:
                        provisioning.expire_linux_ssh_account(ssh_identity)
                except Exception as exc:
                    errors.append(f"{username}: {exc}")
            try:
                _reconcile_xray_if_active(path)
            except Exception as exc:
                subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                               check=False, capture_output=True)
                raise RuntimeError("Inactive users were revoked in the database, but Xray reconciliation failed; Xray was stopped fail-closed") from exc
            print(f"Expired accounts processed: {len(expired)}")
            print(f"Inactive accounts reconciled for external revocation: {len(inactive)}")
            if errors:
                raise RuntimeError("Some Linux accounts could not be locked: " + "; ".join(errors))
        elif args.command == "xray-provision":
            if os.geteuid() != 0:
                raise PermissionError("Xray credential management requires root")
            previous = database.get_xray_protocol_state(path, args.username, args.protocol)
            secret = database.create_xray_protocol(path, args.username, args.protocol, rotate=args.rotate)
            try:
                _reconcile_xray_if_active(path)
            except Exception as exc:
                try:
                    database.restore_xray_protocol_state(path, args.username, args.protocol, previous)
                    _reconcile_xray_if_active(path)
                except Exception:
                    subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                                   check=False, capture_output=True)
                raise RuntimeError("Xray credential was not activated; previous database credential restored where possible") from exc
            print(f"Provisioned {secret['protocol']} for {secret['username']}")
            print(f"Xray stats identity: {secret['identity']}")
            print(f"Client credential (shown now; store securely): {secret['credential']}")
            if _xray_service_active():
                _print_connection_details(args.username, [secret])
                print("Active Xray configuration was validated and reloaded.")
            else:
                print("Xray service is inactive; credential is stored but is not yet accepting connections.")
        elif args.command == "xray-config":
            if os.geteuid() != 0:
                raise PermissionError("Xray config generation requires root because it contains client credentials")
            config = xray_config.build_config(path, args.cert_file, args.key_file,
                                             args.vless_port, args.trojan_port, args.api_port)
            xray_config.validate_with_xray(config, args.xray_bin)
            if args.check_only:
                print("Xray accepted the generated configuration; no file was written.")
            else:
                if args.apply:
                    output = xray_config.apply_config(config, args.output, args.xray_bin)
                else:
                    output = xray_config.write_config(config, args.output)
                print(f"Validated Xray config written with protected permissions: {output}")
        elif args.command == "backup":
            directory = Path(args.dir) if args.dir else path.parent / "backups"
            print(backup.create_backup(path, directory))
        elif args.command == "restore":
            if os.geteuid() != 0:
                raise PermissionError("Restoring the managed database requires root")
            directory = Path(args.dir) if args.dir else path.parent / "backups"
            timers = ("ispbandits-collect.timer", "ispbandits-ssh-collect.timer",
                      "ispbandits-expire.timer", "ispbandits-ssh-meter.timer")
            services = ("ispbandits-collect.service", "ispbandits-ssh-collect.service",
                        "ispbandits-expire.service", "ispbandits-ssh-meter.service",
                        "ispbandits-xray.service")
            active_units = set()
            for unit in (*timers, *services):
                try:
                    active = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                                             check=False, capture_output=True, timeout=5).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    active = False
                if active:
                    active_units.add(unit)
            stopped_units = []
            pause_failures = []
            for unit in (*timers, *services):
                if unit in active_units:
                    try:
                        stopped = subprocess.run(["systemctl", "stop", unit], check=False,
                                                 capture_output=True, timeout=15)
                        still_active = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                                                      check=False, capture_output=True, timeout=5).returncode == 0
                    except (OSError, subprocess.SubprocessError):
                        pause_failures.append(unit)
                        break
                    if stopped.returncode != 0 or still_active:
                        pause_failures.append(unit)
                        break
                    stopped_units.append(unit)
            if pause_failures:
                for unit in reversed(stopped_units):
                    subprocess.run(["systemctl", "start", unit], check=False,
                                   capture_output=True, timeout=15)
                raise RuntimeError("Could not quiesce managed units before restore: " + ", ".join(pause_failures))
            rollback = None
            resume_errors = []
            try:
                rollback = backup.restore_backup(path, args.archive, directory)
                # Reconcile the persistent config while Xray is stopped, then
                # start it only if it had been active before the restore.
                _reconcile_xray_if_active(path)
                if "ispbandits-xray.service" in active_units:
                    started = subprocess.run(["systemctl", "start", "ispbandits-xray.service"],
                                             check=False, capture_output=True, timeout=30)
                    if started.returncode != 0 or not _xray_service_active():
                        raise RuntimeError("Restored Xray config did not start; service remains stopped")
            except Exception as exc:
                if rollback is not None and rollback.is_file():
                    try:
                        backup.restore_backup(path, rollback, directory)
                        _reconcile_xray_if_active(path)
                        if "ispbandits-xray.service" in active_units:
                            subprocess.run(["systemctl", "start", "ispbandits-xray.service"],
                                           check=False, capture_output=True, timeout=30)
                    except Exception as rollback_exc:
                        subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                                       check=False, capture_output=True)
                        raise RuntimeError("Restore failed and database rollback also failed; Xray was stopped") from rollback_exc
                raise RuntimeError("Restore failed; the rollback database was restored where available") from exc
            finally:
                for unit in timers:
                    if unit in active_units:
                        try:
                            resumed = subprocess.run(["systemctl", "start", unit], check=False,
                                                     capture_output=True, timeout=15)
                            failed = resumed.returncode != 0
                        except (OSError, subprocess.SubprocessError):
                            failed = True
                        if failed:
                            resume_errors.append(unit)
            if rollback:
                print(f"Restore complete. Previous database saved at: {rollback}")
            else:
                print("Restore complete. No previous database existed to back up.")
            print("Managed database writers were quiesced; previously active timers were resumed.")
            if resume_errors:
                print("WARNING: these previously active timers did not resume: " + ", ".join(resume_errors),
                      file=sys.stderr)
        elif args.command == "verify":
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            print(result)
            return 0 if result == "ok" else 1
        elif args.command == "collect":
            if os.geteuid() != 0:
                raise PermissionError("Traffic collection requires root access to local service metadata")
            if args.enforce_quotas and (args.xray_only or args.dry_run):
                raise ValueError("Quota enforcement requires a committed full Xray+SSH collection")
            collector.DB_PATH = path
            samples, delta_bytes, resets = collector.collect(dry_run=args.dry_run, xray_only=args.xray_only)
            action = "dry-run (rolled back)" if args.dry_run else "committed"
            print(f"Collection {action}: counter_directions={samples}, delta_bytes={delta_bytes}, generation_changes={resets}")
            if args.enforce_quotas:
                newly_exceeded = database.enforce_data_quotas(path)
                quota_users = database.list_quota_exceeded_users(path)
                if quota_users:
                    _quota_revoke(path, quota_users)
                print(f"Lifetime quota enforcement: newly exceeded={len(newly_exceeded)}; accounts reconciled={len(quota_users)}")
        elif args.command == "ssh-meter":
            result = ssh_meter.collect_snapshot(path, state_path=Path(args.state),
                                                snapshot_path=Path(args.snapshot),
                                                sshd_bin=args.sshd_bin)
            print(f"Experimental SSH meter snapshot: active_flows={result['active_flows']} tracked_users={result['tracked_users']} generation={result['generation']}")
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, tarfile.TarError,
            subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
