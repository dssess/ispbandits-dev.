#!/usr/bin/env bash
# © 2026 ISPbandits Dev. All rights reserved. Proprietary software.
# Guided bootstrap for a fresh Ubuntu 22/24 or Debian 12/13 VPS.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

REPO_ARCHIVE="https://codeload.github.com/dssess/ispbandits-dev./tar.gz/refs/tags/v1.0.0-alpha.4"
DOMAIN=""
EMAIL=""
CERT_FILE=""
KEY_FILE=""
MODE=""
UPGRADE=0
CERTBOT_PACKAGE_INSTALLED_BY_RUN=0
CERTBOT_TIMER_RESTORE=0
CERTBOT_TIMER_ENABLED_BEFORE=0
CERTBOT_TIMER_ACTIVE_BEFORE=0
CERTBOT_CERT_EXISTED_BEFORE=0
CERTBOT_ISSUANCE_STARTED=0
QUICK_DEPLOY_SUCCEEDED=0

usage() {
  cat <<'EOF'
Neverbandits alpha-4 quick installer (publisher: ISPbandits Dev)

Usage:
  sudo bash quick-install.sh [--domain HOSTNAME] [--email EMAIL]
                            [--cert-file CERT --key-file KEY | --self-signed]
  sudo bash quick-install.sh --upgrade

If no TLS option is supplied, the script interactively offers Let's Encrypt,
existing certificate files, or a self-signed certificate (testing only).

Fresh installation is for a new VPS only. --upgrade updates a recognized
Neverbandits alpha-2/alpha-3/alpha-4 install in code-only mode with database backup and rollback;
it preserves Xray/Nginx/SSHWS/TLS topology and provider firewall rules.
EOF
}

fail() { echo "quick-install.sh: $*" >&2; exit 1; }

wait_for_acme_http_timewait() {
  command -v ss >/dev/null 2>&1 || fail "The ss command is required to safely wait for completed HTTP-01 connections to release port 80."
  local attempt remaining
  for attempt in $(seq 1 90); do
    remaining=$(ss -H -tan state time-wait '( sport = :80 )' 2>/dev/null | wc -l)
    if [[ "$remaining" == 0 ]]; then
      echo "HTTP-01 connections released port 80."
      return 0
    fi
    if (( attempt % 10 == 0 )); then
      echo "Waiting for ${remaining} completed HTTP-01 connection(s) on port 80 to expire (${attempt}s)."
    fi
    sleep 1
  done
  fail "Completed HTTP-01 connections did not release port 80 within 90 seconds; no service installation was started."
}

verify_source_archive() {
  python3 - "$1" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
archive = sys.argv[1]
with tarfile.open(archive, "r:gz") as tar:
    members = tar.getmembers()
    if not members or len(members) > 10000:
        raise SystemExit("source archive has an invalid member count")
    total = 0
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("source archive contains a traversal path")
        if not (member.isfile() or member.isdir()) or member.issym() or member.islnk():
            raise SystemExit("source archive contains a link or special file")
        total += member.size
        if total > 200 * 1024 * 1024:
            raise SystemExit("source archive exceeds the 200 MiB extraction limit")
PY
}

while (($#)); do
  case "$1" in
    --domain|--email|--cert-file|--key-file)
      (($# >= 2)) || { usage >&2; exit 2; }
      case "$1" in
        --domain) DOMAIN=$2 ;;
        --email) EMAIL=$2 ;;
        --cert-file) CERT_FILE=$2 ;;
        --key-file) KEY_FILE=$2 ;;
      esac
      shift 2
      ;;
    --self-signed) MODE=self-signed; shift ;;
    --upgrade) UPGRADE=1; shift ;;
    -h|--help) usage; exit 0 ;;
      *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if (( UPGRADE )); then
  [[ -z "$DOMAIN$EMAIL$CERT_FILE$KEY_FILE$MODE" ]] || fail "Do not combine --upgrade with fresh-install domain or TLS options."
  [[ ${EUID} -eq 0 ]] || fail "Run upgrade as root: sudo bash quick-install.sh --upgrade"
  MARKER=/opt/ispbandits-dev/INSTALL_INFO
  [[ -f "$MARKER" ]] || fail "No managed ISPbandits install marker at $MARKER."
  grep -Fxq 'managed_by=ISPbandits-Dev-install.sh' "$MARKER" || fail "The existing install marker is not recognized."
  grep -Fxq 'database_path=/var/lib/ispbandits-dev/database.db' "$MARKER" || fail "The existing database path is not the supported managed path."
  [[ -r /etc/os-release ]] || fail "Cannot detect this operating system."
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
    *) fail "Unsupported OS ${PRETTY_NAME:-unknown}; use Ubuntu 22.04/24.04 or Debian 12/13." ;;
  esac
  command -v systemctl >/dev/null 2>&1 || fail "A systemd-based managed VPS is required."
  [[ -r /dev/tty ]] || fail "Run the guided upgrade from an interactive terminal."
  grep -Eq '^version=1\.0\.0-alpha\.(2|3|4)$' "$MARKER" || fail "This release changes network topology; install it on a fresh VPS rather than upgrading an earlier alpha."
  grep -Fxq 'ssh_websocket=1' "$MARKER" || fail "The existing install does not have the SSH-WebSocket topology; use a fresh VPS."
  echo "This code-only update backs up SQLite and preserves the existing alpha-2/alpha-3/alpha-4 Nginx/SSHWS/Xray topology."
  read -r -p "Continue? Type UPGRADE: " CONFIRM </dev/tty
  [[ "$CONFIRM" == UPGRADE ]] || fail "Upgrade cancelled."
  SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  TMP_DIR=""
  cleanup_upgrade() { [[ -z "$TMP_DIR" ]] || rm -rf -- "$TMP_DIR"; }
  trap cleanup_upgrade EXIT
  if [[ ! -f "$SOURCE_DIR/install.sh" || ! -f "$SOURCE_DIR/pyproject.toml" ]]; then
    TMP_DIR=$(mktemp -d /tmp/ispbandits-quick-upgrade.XXXXXX)
    command -v curl >/dev/null 2>&1 || fail "curl is required to fetch the pinned source archive."
    curl -fL --retry 3 --proto '=https' --tlsv1.2 "$REPO_ARCHIVE" -o "$TMP_DIR/source.tar.gz"
    verify_source_archive "$TMP_DIR/source.tar.gz" || fail "Downloaded source archive failed safety validation."
    mkdir "$TMP_DIR/source"
    tar -xzf "$TMP_DIR/source.tar.gz" --strip-components=1 -C "$TMP_DIR/source"
    SOURCE_DIR="$TMP_DIR/source"
  fi
  [[ -f "$SOURCE_DIR/install.sh" && -f "$SOURCE_DIR/pyproject.toml" ]] || fail "Project source is incomplete."
  bash "$SOURCE_DIR/install.sh" --upgrade
  echo "Upgrade complete. Review the updated guide and run: sudo neverbandits verify"
  exit 0
fi

prompt() {
  local __var=$1 __label=$2 __value
  [[ -r /dev/tty ]] || fail "Missing required value for $2; run from an interactive terminal or pass it as an option."
  read -r -p "$__label" __value </dev/tty
  printf -v "$__var" '%s' "$__value"
}

if [[ -n "$CERT_FILE" || -n "$KEY_FILE" ]]; then
  [[ -n "$CERT_FILE" && -n "$KEY_FILE" ]] || fail "Provide both --cert-file and --key-file."
  [[ -z "$MODE" ]] || fail "Choose either existing TLS files or --self-signed, not both."
  MODE=existing
fi

if [[ -z "$DOMAIN" ]]; then prompt DOMAIN "Hostname (for example vpn.example.com): "; fi
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]] || fail "Enter a valid DNS hostname."

if [[ -z "$MODE" ]]; then
  cat <<'EOF'
Choose TLS setup:
  1) Let's Encrypt certificate (recommended; requires DNS pointed here and inbound TCP 80)
  2) Use certificate and key files already on this VPS
  3) Self-signed certificate (testing only; not suitable for ordinary clients)
EOF
  prompt choice "Choice [1]: "
  choice=${choice:-1}
  case "$choice" in
    1) MODE=letsencrypt ;;
    2) MODE=existing ;;
    3) MODE=self-signed ;;
    *) fail "Choose 1, 2, or 3." ;;
  esac
fi

if [[ "$MODE" == existing ]]; then
  [[ -n "$CERT_FILE" ]] || prompt CERT_FILE "Absolute path to TLS fullchain certificate: "
  [[ -n "$KEY_FILE" ]] || prompt KEY_FILE "Absolute path to matching TLS private key: "
  [[ "$CERT_FILE" == /* && "$KEY_FILE" == /* ]] || fail "Certificate and key paths must be absolute."
  [[ -r "$CERT_FILE" && -r "$KEY_FILE" ]] || fail "Cannot read the supplied certificate or private key."
elif [[ "$MODE" == letsencrypt ]]; then
  if [[ -z "$EMAIL" ]]; then prompt EMAIL "Email for Let's Encrypt renewal notices: "; fi
  [[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || fail "Enter a valid email address for certificate registration."
fi

[[ ${EUID} -eq 0 ]] || fail "Run as root, for example: sudo bash quick-install.sh"
[[ -r /etc/os-release ]] || fail "Cannot detect this operating system."
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
  *) fail "Unsupported OS ${PRETTY_NAME:-unknown}; use Ubuntu 22.04/24.04 or Debian 12/13." ;;
esac
case "$(uname -m)" in
  x86_64|aarch64|arm64) ;;
  *) fail "Full Xray installation supports x86_64 and arm64 only." ;;
esac
command -v systemctl >/dev/null 2>&1 || fail "A systemd-based VPS is required."
command -v apt-get >/dev/null 2>&1 || fail "This quick installer requires apt-get."

cat <<EOF

INSTALLATION WARNING
This alpha has not yet passed production client-connectivity testing. Use a fresh,
disposable VPS. It installs Nginx on TCP 80/443, SSH-over-WebSocket, and Xray;
Trojan uses TCP 8443. SSH per-user
traffic metering and shared quota enforcement are experimental and not production-validated.

Before continuing, point $DOMAIN to this VPS, ensure TCP 80, 443, and 8443 are available,
and allow those ports through the VPS/provider firewall. The script cannot edit
your provider firewall. It will not alter administrator SSH settings.
EOF

[[ -r /dev/tty ]] || fail "Run this installer from an interactive terminal."
read -r -p "Continue on this fresh VPS? Type YES to proceed: " CONFIRM </dev/tty
[[ "$CONFIRM" == YES ]] || fail "Installation cancelled."

command -v curl >/dev/null 2>&1 || fail "curl is required to fetch and verify the pinned release source."
command -v python3 >/dev/null 2>&1 || fail "Python 3 is required to verify the pinned source archive; install it before continuing."
command -v tar >/dev/null 2>&1 || fail "tar is required to extract the verified source archive; install it before continuing."
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TMP_DIR=""
cleanup() {
  local rc=$?
  trap - EXIT
  if (( rc != 0 && QUICK_DEPLOY_SUCCEEDED == 0 )); then
    if (( CERTBOT_TIMER_RESTORE )); then
      if (( CERTBOT_TIMER_ENABLED_BEFORE )); then
        systemctl enable certbot.timer >/dev/null 2>&1 || true
      else
        systemctl disable certbot.timer >/dev/null 2>&1 || true
      fi
      if (( CERTBOT_TIMER_ACTIVE_BEFORE )); then
        systemctl start certbot.timer >/dev/null 2>&1 || true
      else
        systemctl stop certbot.timer >/dev/null 2>&1 || true
      fi
    fi
    if (( CERTBOT_ISSUANCE_STARTED && CERTBOT_CERT_EXISTED_BEFORE == 0 )) && command -v certbot >/dev/null 2>&1; then
      certbot delete --cert-name "$DOMAIN" --non-interactive --quiet >/dev/null 2>&1 ||
        echo "WARNING: Could not automatically remove the Let's Encrypt certificate created during the failed installation." >&2
    fi
    if (( CERTBOT_PACKAGE_INSTALLED_BY_RUN )) &&
       dpkg-query -W -f='${db:Status-Status}' certbot 2>/dev/null | grep -qx installed; then
      DEBIAN_FRONTEND=noninteractive apt-get purge -y certbot >/dev/null 2>&1 ||
        echo "WARNING: Could not automatically purge the newly installed Certbot package." >&2
    fi
  fi
  if [[ -n "$TMP_DIR" ]]; then rm -rf -- "$TMP_DIR"; fi
  exit "$rc"
}
trap cleanup EXIT
if [[ ! -f "$SOURCE_DIR/install.sh" || ! -f "$SOURCE_DIR/pyproject.toml" ]]; then
  TMP_DIR=$(mktemp -d /tmp/ispbandits-quick-install.XXXXXX)
  curl -fL --retry 3 --proto '=https' --tlsv1.2 "$REPO_ARCHIVE" -o "$TMP_DIR/source.tar.gz"
  mkdir "$TMP_DIR/source"
  verify_source_archive "$TMP_DIR/source.tar.gz" || fail "Downloaded source archive failed safety validation."
  tar -xzf "$TMP_DIR/source.tar.gz" --strip-components=1 -C "$TMP_DIR/source"
  SOURCE_DIR="$TMP_DIR/source"
fi
[[ -f "$SOURCE_DIR/install.sh" && -f "$SOURCE_DIR/pyproject.toml" ]] || fail "Project source is incomplete."
bash "$SOURCE_DIR/install.sh" --preflight-only --domain "$DOMAIN"

export DEBIAN_FRONTEND=noninteractive
if [[ "$MODE" == letsencrypt ]]; then
  CERTBOT_TIMER_RESTORE=1
  systemctl is-enabled --quiet certbot.timer 2>/dev/null && CERTBOT_TIMER_ENABLED_BEFORE=1 || true
  systemctl is-active --quiet certbot.timer 2>/dev/null && CERTBOT_TIMER_ACTIVE_BEFORE=1 || true
  [[ ! -e "/etc/letsencrypt/live/$DOMAIN" ]] || CERTBOT_CERT_EXISTED_BEFORE=1
  if ! dpkg-query -W -f='${db:Status-Status}' certbot 2>/dev/null | grep -qx installed; then
    CERTBOT_PACKAGE_INSTALLED_BY_RUN=1
  fi
fi
apt-get update
if [[ "$MODE" == letsencrypt ]]; then
  apt-get install -y ca-certificates curl tar python3 unzip openssl certbot
else
  apt-get install -y ca-certificates curl tar python3 unzip openssl
fi

if [[ "$MODE" == letsencrypt ]]; then
  CERTBOT_ISSUANCE_STARTED=1
  certbot certonly --standalone --non-interactive --agree-tos --email "$EMAIL" \
    --preferred-challenges http -d "$DOMAIN"
  wait_for_acme_http_timewait
  if ! systemctl list-unit-files certbot.timer --no-legend | grep -q '^certbot.timer'; then
    fail "The certbot package did not provide certbot.timer; refusing to install without automatic certificate renewal."
  fi
  systemctl enable --now certbot.timer
  CERT_FILE="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  KEY_FILE="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
fi

case "$MODE" in
  letsencrypt|existing)
    if [[ "$MODE" == letsencrypt ]]; then
      bash "$SOURCE_DIR/install.sh" --full --domain "$DOMAIN" --cert-file "$CERT_FILE" --key-file "$KEY_FILE" --certbot-standalone
    else
      bash "$SOURCE_DIR/install.sh" --full --domain "$DOMAIN" --cert-file "$CERT_FILE" --key-file "$KEY_FILE"
    fi
    ;;
  self-signed)
    bash "$SOURCE_DIR/install.sh" --full --domain "$DOMAIN" --self-signed
    ;;
  *) fail "Internal TLS mode error." ;;
esac
QUICK_DEPLOY_SUCCEEDED=1

cat <<'EOF'

Next: create an account with:
  sudo neverbandits menu

Open TCP 80, 443, and 8443 in the provider firewall. TCP 80 serves plain SSH-WS
and is also used for Let's Encrypt renewal; TCP 443 serves SSH-WSS/VLESS-WS.
Review the README for
limitations and management commands.
EOF

if [[ -t 0 && -x /usr/local/bin/neverbandits ]]; then
  /usr/local/bin/neverbandits menu
fi
