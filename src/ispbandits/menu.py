"""Interactive ISPbandits Dev administration menu."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import sys

NOTICE = "© 2026 ISPbandits Dev. All rights reserved. Proprietary software."


def _call_cli(db_path: Path, *arguments: str) -> int:
    """Dispatch a menu action through the same CLI handlers used by operators."""
    from .cli import main as cli_main
    return cli_main(["--db", str(db_path), *arguments])


def _pause() -> None:
    try:
        input("\nPress Enter to return to the menu...")
    except (EOFError, KeyboardInterrupt):
        pass


def _create_account(db_path: Path) -> None:
    if os.geteuid() != 0:
        print("Account creation requires root. Start the menu with: sudo ispbandits")
        return
    try:
        username = input("New username: ").strip()
        if not username:
            print("Username cannot be empty.")
            return
        expiry = input("Expiry date (YYYY-MM-DD): ").strip()
        expiry_date = dt.date.fromisoformat(expiry)
        if expiry_date < dt.date.today():
            print("Expiry date must be today or later.")
            return
        print("\nProtocols:")
        print("  1) OpenSSH + VLESS + Trojan (recommended)")
        print("  2) VLESS + Trojan only")
        print("  3) OpenSSH only")
        selection = input("Choose protocols [1]: ").strip() or "1"
        protocol_sets = {
            "1": ("openssh", "vless", "trojan"),
            "2": ("vless", "trojan"),
            "3": ("openssh",),
        }
        if selection not in protocol_sets:
            print("Choose 1, 2, or 3.")
            return
        print("\nCreating account. Save any generated credentials securely; they are shown only once.\n")
        _call_cli(db_path, "account-create", username, "--expires", expiry,
                  "--protocols", *protocol_sets[selection])
    except (EOFError, KeyboardInterrupt):
        print("\nAccount creation cancelled.")
    except ValueError:
        print("Invalid expiry date. Use YYYY-MM-DD.")
    except OSError as exc:
        print(f"Unable to create account: {exc}")


def _service_status() -> None:
    units = (
        ("Xray proxy", "ispbandits-xray.service"),
        ("Traffic collector timer", "ispbandits-collect.timer"),
        ("Expiry timer", "ispbandits-expire.timer"),
        ("TLS refresh timer", "ispbandits-tls-refresh.timer"),
    )
    print("Managed service status:")
    for label, unit in units:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit], check=False,
                capture_output=True, text=True, timeout=5)
            state = result.stdout.strip() or "not installed"
        except (OSError, subprocess.TimeoutExpired):
            state = "systemctl unavailable"
        print(f"  {label:<24} {state}")


def run_menu(db_path: Path) -> int:
    """Run the terminal menu. Return cleanly on EOF or Ctrl-C."""
    if not sys.stdin.isatty():
        print("The interactive menu needs a terminal. Run: sudo ispbandits", file=sys.stderr)
        return 2
    while True:
        print("\n" + "=" * 56)
        print(" ISPbandits Dev — VPS Administration")
        print("=" * 56)
        print("  1) Create account (SSH / VLESS / Trojan)")
        print("  2) View account usage totals")
        print("  3) View online SSH/Dropbear processes")
        print("  4) Check managed service status")
        print("  5) Run account expiry check now")
        print("  6) Create a protected database backup")
        print("  7) Verify database integrity")
        print("  0) Exit")
        print("\n  Note: per-user SSH traffic metering and quota enforcement are not implemented.")
        try:
            choice = input("\nSelect an option: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting ISPbandits Dev menu.")
            return 0
        if choice in {"0", "q", "Q"}:
            print("Goodbye.")
            return 0
        if choice == "1":
            _create_account(db_path)
        elif choice == "2":
            _call_cli(db_path, "usage")
        elif choice == "3":
            _call_cli(db_path, "online")
        elif choice == "4":
            _service_status()
        elif choice == "5":
            if os.geteuid() != 0:
                print("Expiry maintenance requires root. Start the menu with: sudo ispbandits")
            else:
                _call_cli(db_path, "expire")
        elif choice == "6":
            if os.geteuid() != 0:
                print("Backups require root. Start the menu with: sudo ispbandits")
            else:
                _call_cli(db_path, "backup", "--dir", "/var/backups/ispbandits-dev")
        elif choice == "7":
            _call_cli(db_path, "verify")
        else:
            print("Unknown option. Choose one of the displayed numbers.")
        _pause()
