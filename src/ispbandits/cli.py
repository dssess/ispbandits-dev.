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
    create.add_argument("--protocols", nargs="+", choices=("openssh", "dropbear", "vless", "trojan"),
                        default=("openssh", "vless", "trojan"))
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
        active = subprocess.run(["systemctl", "is-active", "--quiet", "ispbandits-xray.service"],
                                check=False, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        active = False
    if active:
        try:
            _reconcile_quota_xray(path)
        except Exception as exc:
            subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                           check=False, capture_output=True)
            raise RuntimeError("Quota-exceeded accounts were recorded, but Xray reconciliation failed; Xray was stopped fail-closed") from exc
    if errors:
        raise RuntimeError("Some SSH accounts could not be locked: " + "; ".join(errors))


def _reconcile_quota_xray(path: Path) -> None:
    output = Path("/etc/ispbandits-dev/xray.json")
    config = xray_config.build_config(path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                                     "/etc/ispbandits-dev/tls/current/privkey.pem")
    expected = json.dumps(config, indent=2, sort_keys=True) + "\n"
    try:
        current = output.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != expected:
        xray_config.apply_config(config, output, "/usr/local/bin/xray")


def _quota_reactivate(path: Path, username: str, expiry_date: str) -> None:
    if _has_managed_linux_ssh(path, username):
        provisioning.reactivate_linux_ssh_account(username, expiry_date)
    try:
        active = subprocess.run(["systemctl", "is-active", "--quiet", "ispbandits-xray.service"],
                                check=False, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        active = False
    if active:
        config = xray_config.build_config(path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                                          "/etc/ispbandits-dev/tls/current/privkey.pem")
        xray_config.apply_config(config, "/etc/ispbandits-dev/xray.json", "/usr/local/bin/xray")


def _has_managed_linux_ssh(path: Path, username: str) -> bool:
    return any(protocol in {"openssh", "dropbear"} and identity == username
               for protocol, identity in database.list_enabled_protocols(path, username))


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


def _rollback_unpublished_account(path: Path, username: str, ssh_password: object,
                                  xray_active: bool) -> None:
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
    if xray_active:
        try:
            if errors:
                raise RuntimeError("Account record or Linux user cleanup failed")
            config = xray_config.build_config(path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                                              "/etc/ispbandits-dev/tls/current/privkey.pem")
            xray_config.apply_config(config, "/etc/ispbandits-dev/xray.json", "/usr/local/bin/xray")
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
            result = provisioning.create_managed_account(path, args.username, args.expires,
                                                          protocols=tuple(args.protocols),
                                                          data_limit=args.limit_gb * 1_000_000_000)
            xray_activated = False
            xray_active = _xray_service_active()
            if result["xray_credentials"]:
                if xray_active:
                    try:
                        config = xray_config.build_config(
                            path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                            "/etc/ispbandits-dev/tls/current/privkey.pem")
                        xray_config.apply_config(config, "/etc/ispbandits-dev/xray.json", "/usr/local/bin/xray")
                        xray_activated = True
                    except Exception as exc:
                        database.delete_user_record(path, args.username)
                        if result["ssh_password"]:
                            provisioning.delete_linux_ssh_account(args.username)
                        raise RuntimeError("Account creation rolled back because Xray config activation failed") from exc
            meter_active = _ssh_meter_timer_active()
            if xray_active or meter_active:
                try:
                    _seed_new_account_checkpoints(path, args.username, xray_active, meter_active)
                except Exception as exc:
                    try:
                        _rollback_unpublished_account(path, args.username, result["ssh_password"], xray_active)
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
            if result["xray_credentials"]:
                if xray_activated:
                    print("VLESS/Trojan identities were added to the validated Xray config and activated.")
                else:
                    print("Xray service is not active; credentials are stored but not yet accepting connections.")
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
            enabled_protocols = database.list_enabled_protocols(path, args.username)
            unsupported = sorted({protocol for protocol, _identity in enabled_protocols} -
                                 {"openssh", "vless", "trojan"})
            if args.limit_gb and unsupported:
                raise ValueError("Shared quota is not supported for this account's enabled protocol(s): " + ", ".join(unsupported))
            if args.limit_gb:
                for protocol, identity in enabled_protocols:
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
                _quota_reactivate(path, args.username, str(result["expiry_date"]))
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
            for username in expired:
                try:
                    if _has_managed_linux_ssh(path, username):
                        provisioning.expire_linux_ssh_account(username)
                except Exception as exc:
                    errors.append(f"{username}: {exc}")
            service_active = bool(expired) and subprocess.run(
                ["systemctl", "is-active", "--quiet", "ispbandits-xray.service"],
                check=False, capture_output=True).returncode == 0
            if service_active:
                try:
                    config = xray_config.build_config(
                        path, "/etc/ispbandits-dev/tls/current/fullchain.pem",
                        "/etc/ispbandits-dev/tls/current/privkey.pem")
                    xray_config.apply_config(config, "/etc/ispbandits-dev/xray.json", "/usr/local/bin/xray")
                except Exception as exc:
                    subprocess.run(["systemctl", "stop", "ispbandits-xray.service"],
                                   check=False, capture_output=True)
                    raise RuntimeError("Expired users were revoked, but Xray config reconciliation failed; Xray was stopped fail-closed") from exc
            print(f"Expired accounts processed: {len(expired)}")
            if errors:
                raise RuntimeError("Some Linux accounts could not be locked: " + "; ".join(errors))
            if expired and not service_active:
                print("Xray service is not active; no Xray config was reloaded.")
        elif args.command == "xray-provision":
            if os.geteuid() != 0:
                raise PermissionError("Xray credential management requires root")
            secret = database.create_xray_protocol(path, args.username, args.protocol, rotate=args.rotate)
            print(f"Provisioned {secret['protocol']} for {secret['username']}")
            print(f"Xray stats identity: {secret['identity']}")
            print(f"Client credential (shown now; store securely): {secret['credential']}")
            print("Generate and validate the server config with `ispbandits xray-config` before enabling a listener.")
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
            directory = Path(args.dir) if args.dir else path.parent / "backups"
            rollback = backup.restore_backup(path, args.archive, directory)
            if rollback:
                print(f"Restore complete. Previous database saved at: {rollback}")
            else:
                print("Restore complete. No previous database existed to back up.")
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
    except (ValueError, OSError, RuntimeError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
