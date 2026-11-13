"""ISPbandits Dev administration CLI."""
from __future__ import annotations
import argparse
import datetime as dt
import os
from pathlib import Path
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
    create.add_argument("--protocols", nargs="+", choices=("openssh", "dropbear", "vless", "trojan"),
                        default=("openssh", "vless", "trojan"))
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
    return p


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
            result = provisioning.create_managed_account(path, args.username, args.expires,
                                                          protocols=tuple(args.protocols))
            xray_activated = False
            if result["xray_credentials"]:
                try:
                    service_active = subprocess.run(
                        ["systemctl", "is-active", "--quiet", "ispbandits-xray.service"],
                        check=False, capture_output=True).returncode == 0
                except OSError:
                    service_active = False
                if service_active:
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
            print(f"Created ISPbandits account: {result['username']} (expires {result['expiry_date']})")
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
            database.link_protocol(path, args.username, args.protocol, args.identity, args.credential_ref)
            print(f"Linked {args.protocol} identity for {args.username}.")
        elif args.command == "usage":
            for row in database.list_usage(path):
                print(f"{row['username']:<20} {row['status']:<8} up={row['upload_bytes']} down={row['download_bytes']} legacy={row['legacy_bytes']} total={row['total_bytes']} expires={row['expiry_date']}")
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
            collector.DB_PATH = path
            samples, delta_bytes, resets = collector.collect(dry_run=args.dry_run, xray_only=args.xray_only)
            action = "dry-run (rolled back)" if args.dry_run else "committed"
            print(f"Collection {action}: counter_directions={samples}, delta_bytes={delta_bytes}, generation_changes={resets}")
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
