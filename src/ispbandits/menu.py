"""Interactive ISPbandits Dev administration dashboard."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys

from . import database

NOTICE = "© 2026 ISPbandits Dev. All rights reserved. Proprietary software."
WIDTH = 68
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


class _Colors:
    CYAN = "\033[1;36m"
    WHITE = "\033[1;37m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[1;31m"
    MAGENTA = "\033[1;35m"
    RESET = "\033[0m"


def _colors_enabled() -> bool:
    return bool(sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ)


def _paint(text: str, color: str) -> str:
    return f"{color}{text}{_Colors.RESET}" if _colors_enabled() else text


def _frame(char: str = "─") -> str:
    return _paint("┌" + char * WIDTH + "┐" if char == "─" else "├" + char * WIDTH + "┤", _Colors.CYAN)


def _bottom_frame() -> str:
    return _paint("└" + "─" * WIDTH + "┘", _Colors.CYAN)


def _box_line(text: str = "") -> str:
    visible = _ANSI_RE.sub("", text)
    if len(visible) > WIDTH:
        text = visible[:WIDTH]
        visible = text
    text += " " * max(0, WIDTH - len(visible))
    return f"{_paint('│', _Colors.CYAN)}{text}{_paint('│', _Colors.CYAN)}"


def _uptime() -> str:
    try:
        seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return "unknown"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _memory() -> str:
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.split()[0])
    except (OSError, ValueError, IndexError):
        return "unknown"
    if not {"MemTotal", "MemAvailable"}.issubset(values):
        return "unknown"
    total = values["MemTotal"] // 1024
    used = (values["MemTotal"] - values["MemAvailable"]) // 1024
    return f"{used} / {total} MiB"


def _load() -> str:
    try:
        values = os.getloadavg()
        cores = os.cpu_count() or 1
        return f"{values[0]:.2f} / {cores} cores"
    except (OSError, AttributeError):
        return "unknown"


def _domain() -> str:
    marker = Path("/opt/ispbandits-dev/INSTALL_INFO")
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.startswith("domain="):
                return line.partition("=")[2].strip() or "not configured"
    except OSError:
        pass
    return "not configured"


def _service_state(unit: str) -> str:
    try:
        result = subprocess.run(["systemctl", "is-active", unit], check=False,
                                capture_output=True, text=True, timeout=4)
        state = result.stdout.strip() or "inactive"
    except (OSError, subprocess.TimeoutExpired):
        state = "unavailable"
    return state


def _service_badge(label: str, unit: str) -> str:
    state = _service_state(unit)
    color = _Colors.GREEN if state == "active" else _Colors.RED
    return f"{_paint(label, _Colors.CYAN)} {_paint('[' + state.upper() + ']', color)}"


def _server_summary(db_path: Path) -> list[str]:
    try:
        os_info = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except (OSError, AttributeError):
        os_info = platform.system()
    try:
        rows = database.list_usage(db_path)
    except Exception:
        rows = []
    active = sum(1 for row in rows if str(row["status"]).upper() == "ACTIVE")
    expired = sum(1 for row in rows if str(row["status"]).upper() == "EXPIRED")
    quota = sum(1 for row in rows if str(row["status"]).upper() == "QUOTA_EXCEEDED")
    measured = sum(int(row["measured_bytes"]) for row in rows)
    traffic_gb = measured / 1_000_000_000
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown"
    try:
        kernel = platform.release()
    except Exception:
        kernel = "unknown"
    return [
        f"Host            : {hostname}",
        f"Operating system: {os_info}",
        f"Kernel          : {kernel}",
        f"Uptime          : {_uptime()}   Load: {_load()}",
        f"Memory used     : {_memory()}",
        f"Primary domain  : {_domain()}",
        f"Accounts        : {active} active / {len(rows)} total | expired {expired} | quota-locked {quota}",
        f"Measured traffic: {traffic_gb:.3f} GB stored measured counters",
    ]


def _show_dashboard(db_path: Path) -> None:
    print(_frame())
    print(_box_line(_paint("ISPbandits Dev — VPS Administration", _Colors.WHITE).center(WIDTH)))
    print(_paint("├" + "─" * WIDTH + "┤", _Colors.CYAN))
    for line in _server_summary(db_path):
        print(_box_line("  " + line))
    print(_paint("├" + "─" * WIDTH + "┤", _Colors.CYAN))
    services = [
        _service_badge("XRAY", "ispbandits-xray.service"),
        _service_badge("COLLECTOR", "ispbandits-collect.timer"),
        _service_badge("SSH METER", "ispbandits-ssh-meter.timer"),
        _service_badge("EXPIRY", "ispbandits-expire.timer"),
        _service_badge("TLS", "ispbandits-tls-refresh.timer"),
    ]
    print(_box_line("  " + "   ".join(services[:3])))
    print(_box_line("  " + "   ".join(services[3:])))
    print(_bottom_frame())
    print()
    print(f"  {_paint('[01]', _Colors.WHITE)}  CREATE ACCOUNT          {_paint('[02]', _Colors.WHITE)}  ACCOUNT USAGE")
    print(f"  {_paint('[03]', _Colors.WHITE)}  ONLINE SSH               {_paint('[04]', _Colors.WHITE)}  SERVICE STATUS")
    print(f"  {_paint('[05]', _Colors.WHITE)}  RUN EXPIRY CHECK         {_paint('[06]', _Colors.WHITE)}  BACKUP DATABASE")
    print(f"  {_paint('[07]', _Colors.WHITE)}  VERIFY DATABASE          {_paint('[08]', _Colors.WHITE)}  SET LIFETIME QUOTA")
    print(f"  {_paint('[09]', _Colors.WHITE)}  LIST ACCOUNTS             {_paint('[10]', _Colors.WHITE)}  ACCOUNT DETAILS")
    print(f"  {_paint('[11]', _Colors.WHITE)}  RENEW ACCOUNT             {_paint('[12]', _Colors.WHITE)}  DISABLE ACCOUNT")
    print(f"  {_paint('[13]', _Colors.WHITE)}  ENABLE ACCOUNT            {_paint('[14]', _Colors.WHITE)}  RESET SSH PASSWORD")
    print(f"  {_paint('[15]', _Colors.WHITE)}  DELETE ACCOUNT            {_paint('[16]', _Colors.WHITE)}  RESTORE DATABASE")
    print(f"  {_paint('[00]', _Colors.RED)}  EXIT")
    print()
    print(_paint(f"  ISPbandits Dev v1 alpha  |  {NOTICE}", _Colors.CYAN))
    print(_paint("  Experimental SSH meter is opt-in; validate per-session attribution before quota use.", _Colors.YELLOW))


def _call_cli(db_path: Path, *arguments: str) -> int:
    """Dispatch a menu action through the same CLI handlers used by operators."""
    from .cli import main as cli_main
    return cli_main(["--db", str(db_path), *arguments])


def _pause() -> None:
    try:
        input("\nPress Enter to return to the dashboard...")
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
        quota = int(input("Shared lifetime quota in GB [0 = unlimited]: ").strip() or "0")
        if quota < 0:
            print("Quota must be zero or greater.")
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
        print("\nCreating account. Store any generated credentials securely; they are shown only once.\n")
        _call_cli(db_path, "account-create", username, "--expires", expiry,
                  "--limit-gb", str(quota), "--protocols", *protocol_sets[selection])
    except (EOFError, KeyboardInterrupt):
        print("\nAccount creation cancelled.")
    except ValueError:
        print("Invalid date or quota. Use YYYY-MM-DD and a non-negative whole number of GB.")
    except OSError as exc:
        print(f"Unable to create account: {exc}")


def _set_quota(db_path: Path) -> None:
    if os.geteuid() != 0:
        print("Changing account quotas requires root. Start the menu with: sudo ispbandits")
        return
    try:
        username = input("Account username: ").strip()
        limit = int(input("New shared lifetime quota in GB [0 = unlimited]: ").strip())
        if not username or limit < 0:
            print("Enter a username and a non-negative whole number of GB.")
            return
        _call_cli(db_path, "quota-set", username, "--limit-gb", str(limit))
    except (EOFError, KeyboardInterrupt):
        print("\nQuota update cancelled.")
    except ValueError:
        print("Quota must be a non-negative whole number of GB.")


def _root_action(db_path: Path, command: str, prompt: str, extra: tuple[str, ...] = ()) -> None:
    if os.geteuid() != 0:
        print(f"This operation requires root. Start the menu with: sudo ispbandits")
        return
    try:
        username = input(prompt).strip()
        if not username:
            print("Username cannot be empty.")
            return
        if command == "renew":
            expiry = input("New expiry date (YYYY-MM-DD): ").strip()
            _call_cli(db_path, "renew", username, "--expires", expiry)
        else:
            _call_cli(db_path, command, username, *extra)
    except (EOFError, KeyboardInterrupt):
        print("\nOperation cancelled.")


def _delete_account(db_path: Path) -> None:
    if os.geteuid() != 0:
        print("Account deletion requires root. Start the menu with: sudo ispbandits")
        return
    try:
        username = input("Account username to permanently delete: ").strip()
        if not username:
            return
        confirm = input(f"This permanently deletes {username} and its usage. Type the username again: ").strip()
        if confirm != username:
            print("Confirmation did not match; no changes made.")
            return
        _call_cli(db_path, "account-delete", username, "--yes")
    except (EOFError, KeyboardInterrupt):
        print("\nDeletion cancelled.")


def _restore_database(db_path: Path) -> None:
    if os.geteuid() != 0:
        print("Database restore requires root. Start the menu with: sudo ispbandits")
        return
    try:
        archive = input("Absolute path to ISPbandits backup archive: ").strip()
        if not archive.startswith("/"):
            print("Use an absolute archive path; no changes made.")
            return
        confirm = input("Restore replaces the current database; a rollback backup will be saved. Type RESTORE: ").strip()
        if confirm != "RESTORE":
            print("Restore cancelled; no changes made.")
            return
        _call_cli(db_path, "restore", archive, "--dir", "/var/backups/ispbandits-dev")
        print("Important: database restore does not restore OS accounts, certificates, or service files.")
    except (EOFError, KeyboardInterrupt):
        print("\nRestore cancelled.")


def _service_status() -> None:
    units = (
        ("Xray proxy", "ispbandits-xray.service"),
        ("Xray-only collector", "ispbandits-collect.timer"),
        ("Experimental SSH meter", "ispbandits-ssh-meter.timer"),
        ("Full quota collector", "ispbandits-ssh-collect.timer"),
        ("Expiry timer", "ispbandits-expire.timer"),
        ("TLS refresh timer", "ispbandits-tls-refresh.timer"),
    )
    print("Managed service status:")
    for label, unit in units:
        state = _service_state(unit)
        color = _Colors.GREEN if state == "active" else _Colors.RED
        print(f"  {label:<26} {_paint(state, color)}")


def run_menu(db_path: Path) -> int:
    """Run the terminal dashboard. Return cleanly on EOF or Ctrl-C."""
    if not sys.stdin.isatty():
        print("The interactive menu needs a terminal. Run: sudo ispbandits", file=sys.stderr)
        return 2
    while True:
        try:
            if os.environ.get("TERM") not in {None, "", "dumb"}:
                subprocess.run(["clear"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _show_dashboard(db_path)
            choice = input(" Select menu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting ISPbandits Dev.")
            return 0
        if choice in {"0", "00", "q", "Q"}:
            print("Goodbye.")
            return 0
        if choice in {"1", "01"}:
            _create_account(db_path)
        elif choice in {"2", "02"}:
            _call_cli(db_path, "usage")
        elif choice in {"3", "03"}:
            _call_cli(db_path, "online")
        elif choice in {"4", "04"}:
            _service_status()
        elif choice in {"5", "05"}:
            if os.geteuid() != 0:
                print("Expiry maintenance requires root. Start the menu with: sudo ispbandits")
            else:
                _call_cli(db_path, "expire")
        elif choice in {"6", "06"}:
            if os.geteuid() != 0:
                print("Backups require root. Start the menu with: sudo ispbandits")
            else:
                _call_cli(db_path, "backup", "--dir", "/var/backups/ispbandits-dev")
        elif choice in {"7", "07"}:
            _call_cli(db_path, "verify")
        elif choice in {"8", "08"}:
            _set_quota(db_path)
        elif choice in {"9", "09"}:
            _call_cli(db_path, "account-list")
        elif choice in {"10"}:
            _root_action(db_path, "account-show", "Account username: ")
        elif choice in {"11"}:
            _root_action(db_path, "renew", "Account username: ")
        elif choice in {"12"}:
            _root_action(db_path, "disable", "Account username to disable: ")
        elif choice in {"13"}:
            _root_action(db_path, "enable", "Account username to enable: ")
        elif choice in {"14"}:
            _root_action(db_path, "ssh-password-reset", "Account username: ")
        elif choice in {"15"}:
            _delete_account(db_path)
        elif choice in {"16"}:
            _restore_database(db_path)
        else:
            print("Unknown option. Choose one of the displayed numbers.")
        _pause()


__all__ = ["run_menu"]
