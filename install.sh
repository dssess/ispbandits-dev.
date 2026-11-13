#!/usr/bin/env bash
# © 2026 ISPbandits Dev. All rights reserved. Proprietary software.
# Installs the ISPbandits Dev v1 alpha foundation or full Xray services.
set -Eeuo pipefail
IFS=$'\n\t'
umask 022

PREFIX=/opt/ispbandits-dev
DB_PATH=/var/lib/ispbandits-dev/database.db
BIN_DIR=/usr/local/bin
ETC_DIR=/etc
BACKUP_DIR=/var/backups/ispbandits-dev
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
UPGRADE=0
CHECK_ONLY=0
FULL=0
DOMAIN=""
TLS_CERT=""
TLS_KEY=""
SELF_SIGNED=0
SSH_METER=0

usage() {
  cat <<'EOF'
ISPbandits Dev v1 alpha installer

Usage:
  sudo ./install.sh --full --domain vpn.example.com (--cert-file CERT --key-file KEY | --self-signed) [--experimental-ssh-meter]
  sudo ./install.sh --foundation-only [--upgrade] [--prefix PATH] [--db-path PATH]
                    [--bin-dir PATH] [--etc-dir PATH] [--backup-dir PATH]
  sudo ./install.sh --upgrade
  ./install.sh --check

Options:
  --upgrade         Code-only upgrade of a recognized managed install. Preserves
                    Xray/TLS/service topology, backs up SQLite, and rolls back on failure.
  --full            Install Xray, VLESS/Trojan listeners, Xray-only usage collection,
                    TLS refresh, and daily account expiry timers. Requires a domain
                    and certificate pair, or explicit --self-signed (testing only).
  --experimental-ssh-meter  Also install the prototype OpenSSH counter and shared
                    quota collector. Disabled by default; validate the exact VPS first.
  --foundation-only Install only the CLI/SQLite foundation (development/CI).
  --domain NAME     DNS hostname clients will use; its A/AAAA must point to this VPS.
  --cert-file PATH  TLS certificate chain covering --domain.
  --key-file PATH   Matching private key; copied into a protected Xray-readable path.
  --self-signed     Generate a self-signed cert for --domain (testing only).
  --prefix PATH     Application directory (default: /opt/ispbandits-dev)
  --db-path PATH    SQLite file (default: /var/lib/ispbandits-dev/database.db)
  --bin-dir PATH    CLI wrapper directory (default: /usr/local/bin)
  --etc-dir PATH    System configuration root, mainly for MOTD (default: /etc)
  --backup-dir PATH Upgrade/restore backup directory
                    (default: /var/backups/ispbandits-dev)
  --check           Validate source and run tests without installing or changing
                    the system. Does not require root.
  -h, --help        Show this help.

Scope:
  --full installs Xray VLESS/Trojan listeners on TCP 443/8443 and a loopback-only
  API. It does not change admin SSH settings or cloud firewall rules. SSH account
  provisioning is available through `ispbandits account-create`. The optional
  experimental meter counts OpenSSH network bytes, not pure payload bytes; Dropbear
  is unsupported and quota enforcement requires target-specific validation.
EOF
}

fail() { echo "install.sh: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --upgrade) UPGRADE=1; shift ;;
    --full) FULL=1; shift ;;
    --experimental-ssh-meter) SSH_METER=1; shift ;;
    --foundation-only) FULL=0; shift ;;
    --domain|--cert-file|--key-file)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      case "$1" in
        --domain) DOMAIN=$2 ;;
        --cert-file) TLS_CERT=$2 ;;
        --key-file) TLS_KEY=$2 ;;
      esac
      shift 2
      ;;
    --self-signed) SELF_SIGNED=1; shift ;;
    --prefix|--db-path|--bin-dir|--etc-dir|--backup-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      case "$1" in
        --prefix) PREFIX=$2 ;;
        --db-path) DB_PATH=$2 ;;
        --bin-dir) BIN_DIR=$2 ;;
        --etc-dir) ETC_DIR=$2 ;;
        --backup-dir) BACKUP_DIR=$2 ;;
      esac
      shift 2
      ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$SSH_METER" == 1 && "$FULL" != 1 ]]; then
  fail "--experimental-ssh-meter requires --full."
fi

if (( UPGRADE )); then
  [[ "$FULL" == 0 && "$SSH_METER" == 0 && "$SELF_SIGNED" == 0 && -z "$DOMAIN$TLS_CERT$TLS_KEY" ]] ||
    fail "--upgrade is code-only; do not combine it with fresh-deployment, TLS, or meter-topology options."
fi

if [[ "$CHECK_ONLY" != 1 && "$FULL" == 1 ]]; then
  [[ -n "$DOMAIN" ]] || fail "--full requires --domain."
  [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]] || fail "Invalid DNS hostname: $DOMAIN"
  if (( SELF_SIGNED )); then
    [[ -z "$TLS_CERT" && -z "$TLS_KEY" ]] || fail "Use either --self-signed or --cert-file/--key-file, not both."
  else
    [[ -n "$TLS_CERT" && -n "$TLS_KEY" ]] || fail "--full requires both --cert-file and --key-file, or --self-signed."
  fi
  for path in "$TLS_CERT" "$TLS_KEY"; do
    [[ -z "$path" ]] && continue
    [[ "$path" == /* && "$path" != *"'"* && "$path" != *$'\n'* ]] || fail "TLS paths must be absolute and cannot contain single quotes or newlines."
  done
fi

[[ -f "$SOURCE_DIR/src/ispbandits/__init__.py" ]] || fail "Run from the project checkout or extracted source bundle; src/ispbandits is missing."
[[ -f "$SOURCE_DIR/pyproject.toml" && -f "$SOURCE_DIR/LICENSE.proprietary" ]] || fail "Project metadata or proprietary notice is missing."

if [[ ! -r /etc/os-release ]]; then
  [[ "$CHECK_ONLY" == 1 ]] || fail "Cannot detect the operating system."
else
  # shellcheck disable=SC1091
  . /etc/os-release
fi

if [[ "$CHECK_ONLY" != 1 ]]; then
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
    *) fail "Unsupported release ${PRETTY_NAME:-unknown}. Intended targets are Ubuntu 22.04/24.04 and Debian 12/13; verify before adding another release." ;;
  esac
fi

for path in "$PREFIX" "$DB_PATH" "$BIN_DIR" "$ETC_DIR" "$BACKUP_DIR"; do
  [[ "$path" == /* ]] || fail "All paths must be absolute: $path"
  [[ "$path" != *"'"* && "$path" != *$'\n'* ]] || fail "Paths may not contain single quotes or newlines."
done
PREFIX=$(realpath -m "$PREFIX")
DB_PATH=$(realpath -m "$DB_PATH")
BIN_DIR=$(realpath -m "$BIN_DIR")
ETC_DIR=$(realpath -m "$ETC_DIR")
BACKUP_DIR=$(realpath -m "$BACKUP_DIR")
case "$PREFIX" in /|/opt|/usr|/home|/var|/etc|/tmp) fail "Unsafe application prefix: $PREFIX" ;; esac

if ! command -v python3 >/dev/null 2>&1; then
  if [[ "$CHECK_ONLY" == 1 ]]; then fail "Python 3 is required for checks."; fi
  [[ ${EUID} -eq 0 ]] || fail "Python 3 is missing; rerun with sudo to install the required package."
  command -v apt-get >/dev/null 2>&1 || fail "Python 3 is missing and apt-get is unavailable."
  echo "Installing the missing Python 3 runtime using apt..."
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3
fi

python3 - <<'PY' || fail "Python 3.10+ with SQLite support is required."
import sqlite3, sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10+ required; found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} with SQLite {sqlite3.sqlite_version}: OK")
PY

if [[ "$CHECK_ONLY" == 1 ]]; then
  (cd "$SOURCE_DIR" && bash -n install.sh quick-install.sh scripts/refresh-tls.sh && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 - <<'PY'
import ast
from pathlib import Path
for root in (Path("src"), Path("tests")):
    for source in root.rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
PY
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 -m unittest discover -s tests -v)
  echo "Source checks passed. No system files were changed."
  exit 0
fi

[[ ${EUID} -eq 0 ]] || fail "Run installation with sudo/root."

# Validate the exact source before touching deployment paths.
(cd "$SOURCE_DIR" && bash -n install.sh quick-install.sh scripts/refresh-tls.sh && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 - <<'PY'
import ast
from pathlib import Path
for root in (Path("src"), Path("tests")):
    for source in root.rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
PY
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 -m unittest discover -s tests -v)

if [[ "$FULL" == 1 ]]; then
  [[ "$PREFIX" == /opt/ispbandits-dev && "$DB_PATH" == /var/lib/ispbandits-dev/database.db &&
     "$BIN_DIR" == /usr/local/bin && "$ETC_DIR" == /etc ]] || fail "--full uses fixed service paths; remove custom --prefix/--db-path/--bin-dir/--etc-dir options."
  command -v systemctl >/dev/null 2>&1 || fail "--full requires systemd/systemctl."
  if ! command -v curl >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || fail "curl, unzip, and openssl are required; apt-get is unavailable."
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl unzip openssl
  fi
  if (( SSH_METER )); then
    command -v sshd >/dev/null 2>&1 || fail "--experimental-ssh-meter requires an installed OpenSSH server (sshd)."
    if ! command -v nft >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y nftables
    fi
  fi
  if (( SELF_SIGNED == 0 )); then
    [[ -r "$TLS_CERT" && -r "$TLS_KEY" ]] || fail "TLS certificate/key files must be readable: $TLS_CERT / $TLS_KEY"
    openssl x509 -in "$TLS_CERT" -noout -checkhost "$DOMAIN" >/dev/null || fail "Certificate does not cover --domain $DOMAIN."
    openssl x509 -in "$TLS_CERT" -noout -checkend 86400 >/dev/null || fail "TLS certificate expires within 24 hours."
    cert_pub=$(openssl x509 -in "$TLS_CERT" -noout -pubkey | openssl pkey -pubin -outform DER | sha256sum | awk '{print $1}')
    key_pub=$(openssl pkey -passin pass: -in "$TLS_KEY" -pubout -outform DER | sha256sum | awk '{print $1}')
    [[ "$cert_pub" == "$key_pub" ]] || fail "TLS certificate and private key do not match."
  fi
  python3 - <<'PY' || fail "At least one of TCP 443, 8443, or loopback 10085 is already occupied."
import socket, sys
for host, port in (("0.0.0.0",443),("0.0.0.0",8443),("127.0.0.1",10085)):
    s=socket.socket()
    try:
        s.bind((host,port))
    except OSError:
        raise SystemExit(1)
    finally:
        s.close()
PY
fi

MARKER="$PREFIX/INSTALL_INFO"
WRAPPER="$BIN_DIR/ispbandits"
DEPLOY_FULL=$FULL
DEPLOY_SSH_METER=$SSH_METER
DEPLOY_DOMAIN=$DOMAIN
if [[ "$UPGRADE" == 1 ]]; then
  [[ -f "$MARKER" ]] || fail "No installer marker at $MARKER; refusing to upgrade an unknown installation."
  grep -Fxq 'managed_by=ISPbandits-Dev-install.sh' "$MARKER" || fail "Install marker is not recognized."
  grep -Fxq "database_path=$DB_PATH" "$MARKER" || fail "Requested database path differs from the installed path."
  DEPLOY_FULL=$(sed -n 's/^full_xray=//p' "$MARKER" | head -n1)
  DEPLOY_SSH_METER=$(sed -n 's/^ssh_meter_experimental=//p' "$MARKER" | head -n1)
  DEPLOY_DOMAIN=$(sed -n 's/^domain=//p' "$MARKER" | head -n1)
  [[ "$DEPLOY_FULL" == 0 || "$DEPLOY_FULL" == 1 ]] || fail "Install marker has an invalid full_xray value."
  [[ "$DEPLOY_SSH_METER" == 0 || "$DEPLOY_SSH_METER" == 1 ]] || fail "Install marker has an invalid experimental meter value."
  if [[ "$DEPLOY_SSH_METER" == 1 && "$DEPLOY_FULL" != 1 ]]; then fail "Install marker has inconsistent meter deployment state."; fi
  [[ -f "$DB_PATH" ]] || fail "Installed database is missing; refusing an in-place upgrade."
  [[ -f "$WRAPPER" ]] && grep -Fq 'ISPbandits Dev managed wrapper' "$WRAPPER" || fail "Managed CLI wrapper is missing or has been replaced."
else
  [[ ! -e "$MARKER" && ! -e "$PREFIX/src/ispbandits" ]] || fail "An installation already exists at $PREFIX; use --upgrade only for a recognized managed installation."
  [[ ! -e "$DB_PATH" ]] || fail "Database path already exists: $DB_PATH. Refusing to overwrite or migrate it implicitly."
  [[ ! -e "$WRAPPER" ]] || fail "CLI path already exists: $WRAPPER. Refusing to overwrite it."
fi

ensure_dir() {
  local directory="$1" mode="$2"
  [[ -d "$directory" ]] || install -d -m "$mode" "$directory"
}

deploy_xray() {
  [[ ! -e /etc/ispbandits-dev ]] || fail "Xray config directory already exists; refusing to reuse unknown TLS/config state."
  [[ ! -e /etc/systemd/system/ispbandits-xray.service &&
     ! -e /etc/systemd/system/ispbandits-tls-refresh.service &&
     ! -e /etc/systemd/system/ispbandits-tls-refresh.timer &&
     ! -e /etc/systemd/system/ispbandits-expire.service &&
     ! -e /etc/systemd/system/ispbandits-expire.timer &&
     ! -e /etc/systemd/system/ispbandits-collect.service &&
     ! -e /etc/systemd/system/ispbandits-collect.timer &&
     ! -e /etc/systemd/system/ispbandits-ssh-meter.service &&
     ! -e /etc/systemd/system/ispbandits-ssh-meter.timer &&
     ! -e /etc/systemd/system/ispbandits-ssh-collect.service &&
     ! -e /etc/systemd/system/ispbandits-ssh-collect.timer ]] || fail "An ISPbandits systemd unit already exists."
  [[ ! -e /usr/local/sbin/ispbandits-dev-refresh-tls ]] || fail "TLS refresh executable already exists."
  getent group xray >/dev/null 2>&1 && fail "Group 'xray' already exists; refusing to reuse it."
  id xray >/dev/null 2>&1 && fail "User 'xray' already exists; refusing to reuse it."
  [[ ! -e /usr/local/bin/xray ]] || fail "/usr/local/bin/xray already exists; review/remove it manually before the pinned full installation."

  local asset digest arch
  case "$(uname -m)" in
    x86_64)
      asset=Xray-linux-64.zip
      digest=e4bd9119f50ae7c41658b1abd0679ae06f98bd864a2907ff33f2f2cf7081b214
      ;;
    aarch64|arm64)
      asset=Xray-linux-arm64-v8a.zip
      digest=1811c8f1da0c3b6dc707ebaf1c8390e987ba39859a8f0a7aadd8526157f2ec8d
      ;;
    *) fail "Full Xray deployment currently supports x86_64 and arm64 only (found $(uname -m))." ;;
  esac
  XRAY_TMP=$(mktemp -d /tmp/ispbandits-xray.XXXXXX)
  curl -fsSL --retry 3 "https://github.com/XTLS/Xray-core/releases/download/v26.10.31/$asset" -o "$XRAY_TMP/$asset"
  printf '%s  %s\n' "$digest" "$XRAY_TMP/$asset" | sha256sum -c -
  unzip -q "$XRAY_TMP/$asset" xray -d "$XRAY_TMP/unpacked"
  "$XRAY_TMP/unpacked/xray" version >/dev/null
  install -o root -g root -m 0755 "$XRAY_TMP/unpacked/xray" /usr/local/bin/xray
  XRAY_BINARY_INSTALLED=1
  rm -rf -- "$XRAY_TMP"
  XRAY_TMP=""

  groupadd --system xray
  XRAY_GROUP_CREATED=1
  useradd --system --gid xray --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin xray
  XRAY_USER_CREATED=1
  install -d -o root -g xray -m 0750 /etc/ispbandits-dev
  XRAY_FILES_INSTALLED=1

  if (( SELF_SIGNED )); then
    install -d -o root -g root -m 0700 /etc/ispbandits-dev/tls-source
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
      -subj "/CN=$DOMAIN" -addext "subjectAltName=DNS:$DOMAIN" \
      -keyout /etc/ispbandits-dev/tls-source/privkey.pem \
      -out /etc/ispbandits-dev/tls-source/fullchain.pem >/dev/null 2>&1
    chmod 0600 /etc/ispbandits-dev/tls-source/privkey.pem
    chmod 0644 /etc/ispbandits-dev/tls-source/fullchain.pem
    TLS_CERT_SOURCE=/etc/ispbandits-dev/tls-source/fullchain.pem
    TLS_KEY_SOURCE=/etc/ispbandits-dev/tls-source/privkey.pem
  else
    TLS_CERT_SOURCE=$TLS_CERT
    TLS_KEY_SOURCE=$TLS_KEY
  fi
  {
    printf 'TLS_CERT_SOURCE=%q\n' "$TLS_CERT_SOURCE"
    printf 'TLS_KEY_SOURCE=%q\n' "$TLS_KEY_SOURCE"
    printf 'TLS_DOMAIN=%q\n' "$DOMAIN"
  } > /etc/ispbandits-dev/tls-source.env
  chmod 0600 /etc/ispbandits-dev/tls-source.env

  install -o root -g root -m 0750 "$SOURCE_DIR/scripts/refresh-tls.sh" /usr/local/sbin/ispbandits-dev-refresh-tls
  XRAY_UNITS_INSTALLED=1
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-xray.service" /etc/systemd/system/ispbandits-xray.service
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-tls-refresh.service" /etc/systemd/system/ispbandits-tls-refresh.service
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-tls-refresh.timer" /etc/systemd/system/ispbandits-tls-refresh.timer
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-expire.service" /etc/systemd/system/ispbandits-expire.service
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-expire.timer" /etc/systemd/system/ispbandits-expire.timer
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-collect.service" /etc/systemd/system/ispbandits-collect.service
  install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-collect.timer" /etc/systemd/system/ispbandits-collect.timer
  if (( SSH_METER )); then
    SSH_METER_UNITS_INSTALLED=1
    install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-ssh-meter.service" /etc/systemd/system/ispbandits-ssh-meter.service
    install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-ssh-meter.timer" /etc/systemd/system/ispbandits-ssh-meter.timer
    install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-ssh-collect.service" /etc/systemd/system/ispbandits-ssh-collect.service
    install -o root -g root -m 0644 "$SOURCE_DIR/systemd/ispbandits-ssh-collect.timer" /etc/systemd/system/ispbandits-ssh-collect.timer
  fi
  /usr/local/sbin/ispbandits-dev-refresh-tls
  "$WRAPPER" xray-config \
    --cert-file /etc/ispbandits-dev/tls/current/fullchain.pem \
    --key-file /etc/ispbandits-dev/tls/current/privkey.pem \
    --xray-bin /usr/local/bin/xray

  systemctl daemon-reload
  XRAY_SERVICE_ENABLED=1
  systemctl enable --now ispbandits-xray.service
  systemctl enable --now ispbandits-tls-refresh.timer
  systemctl enable --now ispbandits-expire.timer
  if (( SSH_METER )); then
    systemctl disable --now ispbandits-collect.timer >/dev/null 2>&1 || true
    systemctl start ispbandits-ssh-meter.service
    "$WRAPPER" collect --enforce-quotas
    systemctl enable --now ispbandits-ssh-meter.timer ispbandits-ssh-collect.timer
    systemctl is-active --quiet ispbandits-ssh-meter.timer || fail "Experimental SSH meter timer did not become active."
  else
    systemctl enable --now ispbandits-collect.timer
  fi
  systemctl is-active --quiet ispbandits-xray.service || fail "Managed Xray service did not become active."
  /usr/local/bin/xray api statsquery --server=127.0.0.1:10085 >/dev/null || fail "Xray StatsService health check failed."
  echo "Managed Xray service is active. VLESS: TCP 443; Trojan: TCP 8443; API: loopback only."
}

# Create only missing directories; do not change caller-owned directory modes.
ensure_dir "$PREFIX" 0755
ensure_dir "$PREFIX/src" 0755
ensure_dir "$BIN_DIR" 0755
ensure_dir "$(dirname -- "$DB_PATH")" 0700
ensure_dir "$(dirname -- "$ETC_DIR/motd")" 0755
ensure_dir "$BACKUP_DIR" 0700

STAGE_DIR=""
OLD_PACKAGE=""
OLD_WRAPPER=""
MOVED_OLD=0
NEW_PACKAGE_INSTALLED=0
NEW_WRAPPER_INSTALLED=0
NEW_INSTALL=$((1 - UPGRADE))
XRAY_BINARY_INSTALLED=0
XRAY_GROUP_CREATED=0
XRAY_USER_CREATED=0
XRAY_FILES_INSTALLED=0
XRAY_UNITS_INSTALLED=0
XRAY_SERVICE_ENABLED=0
SSH_METER_UNITS_INSTALLED=0
XRAY_TMP=""
UPGRADE_BACKUP=""
UPGRADE_TIMERS_ACTIVE=()
UPGRADE_DATABASE_MAY_BE_MUTATED=0

cleanup() {
  local rc=$?
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then rm -rf -- "$STAGE_DIR"; fi
  if [[ -n "$XRAY_TMP" && -d "$XRAY_TMP" ]]; then rm -rf -- "$XRAY_TMP"; fi
  if (( rc != 0 )); then
    if (( XRAY_SERVICE_ENABLED )); then
      systemctl disable --now ispbandits-tls-refresh.timer ispbandits-expire.timer ispbandits-collect.timer ispbandits-ssh-meter.timer ispbandits-ssh-collect.timer ispbandits-xray.service >/dev/null 2>&1 || true
    fi
    if (( XRAY_UNITS_INSTALLED )); then
      rm -f /etc/systemd/system/ispbandits-xray.service \
            /etc/systemd/system/ispbandits-tls-refresh.service \
            /etc/systemd/system/ispbandits-tls-refresh.timer \
            /etc/systemd/system/ispbandits-expire.service \
            /etc/systemd/system/ispbandits-expire.timer \
            /etc/systemd/system/ispbandits-collect.service \
            /etc/systemd/system/ispbandits-collect.timer \
            /usr/local/sbin/ispbandits-dev-refresh-tls
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if (( SSH_METER_UNITS_INSTALLED )); then
      rm -f /etc/systemd/system/ispbandits-ssh-meter.service \
            /etc/systemd/system/ispbandits-ssh-meter.timer \
            /etc/systemd/system/ispbandits-ssh-collect.service \
            /etc/systemd/system/ispbandits-ssh-collect.timer
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if (( XRAY_FILES_INSTALLED )); then rm -rf /etc/ispbandits-dev; fi
    if (( XRAY_USER_CREATED )); then userdel -f xray >/dev/null 2>&1 || true; fi
    if (( XRAY_GROUP_CREATED )); then groupdel xray >/dev/null 2>&1 || true; fi
    if (( XRAY_BINARY_INSTALLED )); then rm -f /usr/local/bin/xray; fi
    if (( NEW_WRAPPER_INSTALLED )); then rm -f -- "$WRAPPER"; fi
    if [[ -n "$OLD_WRAPPER" && -e "$OLD_WRAPPER" ]]; then mv -- "$OLD_WRAPPER" "$WRAPPER"; fi
    if (( MOVED_OLD )) && [[ -n "$OLD_PACKAGE" && -d "$OLD_PACKAGE" ]]; then
      rm -rf -- "$PREFIX/src/ispbandits"
      mv -- "$OLD_PACKAGE" "$PREFIX/src/ispbandits"
    elif (( NEW_PACKAGE_INSTALLED )); then
      rm -rf -- "$PREFIX/src/ispbandits"
    fi
    if (( UPGRADE && UPGRADE_DATABASE_MAY_BE_MUTATED )) && [[ -n "$UPGRADE_BACKUP" && -f "$UPGRADE_BACKUP" ]]; then
      if ! PYTHONPATH="$PREFIX/src" python3 -c 'import sys; from ispbandits.backup import restore_backup; restore_backup(sys.argv[1], sys.argv[2], sys.argv[3])' \
          "$DB_PATH" "$UPGRADE_BACKUP" "$BACKUP_DIR" >/dev/null 2>&1; then
        echo "CRITICAL: package rollback completed but the pre-upgrade database restore failed; inspect $UPGRADE_BACKUP immediately." >&2
      fi
    fi
    if (( NEW_INSTALL )); then
      rm -f -- "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" "$MARKER"
    fi
    if (( UPGRADE )) && command -v systemctl >/dev/null 2>&1; then
      for unit in "${UPGRADE_TIMERS_ACTIVE[@]}"; do systemctl start "$unit" >/dev/null 2>&1 || true; done
    fi
  elif [[ -n "$OLD_PACKAGE" && -d "$OLD_PACKAGE" ]]; then
    rm -rf -- "$OLD_PACKAGE"
  fi
  if (( rc == 0 )) && [[ -n "$OLD_WRAPPER" && -e "$OLD_WRAPPER" ]]; then rm -f -- "$OLD_WRAPPER"; fi
  exit "$rc"
}
trap cleanup EXIT

STAGE_DIR=$(mktemp -d "$PREFIX/src/.ispbandits-stage.XXXXXX")
cp -R "$SOURCE_DIR/src/ispbandits" "$STAGE_DIR/ispbandits"
find "$STAGE_DIR/ispbandits" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE_DIR/ispbandits" -type d -exec chmod 0755 {} +
find "$STAGE_DIR/ispbandits" -type f -exec chmod 0644 {} +
PYTHONPATH="$STAGE_DIR" python3 -m compileall -q "$STAGE_DIR/ispbandits"
find "$STAGE_DIR/ispbandits" -type d -name __pycache__ -prune -exec rm -rf {} +

if [[ "$UPGRADE" == 1 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    for unit in ispbandits-collect.timer ispbandits-ssh-collect.timer ispbandits-expire.timer ispbandits-ssh-meter.timer; do
      if systemctl is-active --quiet "$unit"; then
        UPGRADE_TIMERS_ACTIVE+=("$unit")
        systemctl stop "$unit" || fail "Could not stop active writer timer $unit; aborting upgrade."
        systemctl is-active --quiet "$unit" && fail "Writer timer $unit remained active; aborting upgrade."
      fi
    done
    for unit in ispbandits-collect.service ispbandits-ssh-collect.service ispbandits-expire.service ispbandits-ssh-meter.service; do
      if systemctl is-active --quiet "$unit"; then
        systemctl stop "$unit" || fail "Could not stop active writer service $unit; aborting upgrade."
        systemctl is-active --quiet "$unit" && fail "Writer service $unit remained active; aborting upgrade."
      fi
    done
  fi
  # Capture the rollback snapshot only after all managed writers are quiescent.
  UPGRADE_BACKUP=$(PYTHONPATH="$STAGE_DIR" python3 -m ispbandits --db "$DB_PATH" backup --dir "$BACKUP_DIR")
fi

OLD_PACKAGE="$PREFIX/src/.ispbandits.previous.$$"
if [[ -e "$PREFIX/src/ispbandits" ]]; then
  mv -- "$PREFIX/src/ispbandits" "$OLD_PACKAGE"
  MOVED_OLD=1
fi
mv -- "$STAGE_DIR/ispbandits" "$PREFIX/src/ispbandits"
NEW_PACKAGE_INSTALLED=1

# Install documentation and license without fetching code or dependencies.
install -m 0644 "$SOURCE_DIR/LICENSE.proprietary" "$PREFIX/LICENSE.proprietary"
install -m 0644 "$SOURCE_DIR/README.md" "$PREFIX/README.md"
if [[ -d "$SOURCE_DIR/docs" ]]; then
  install -d -m 0755 "$PREFIX/docs"
  while IFS= read -r -d '' file; do
    relative=${file#"$SOURCE_DIR/docs/"}
    install -D -m 0644 "$file" "$PREFIX/docs/$relative"
  done < <(find "$SOURCE_DIR/docs" -type f -print0)
fi

# On upgrades the configured DB already exists; initialize performs compatible,
# transactional schema migrations. Fresh installs create a new empty DB.
if (( UPGRADE )); then UPGRADE_DATABASE_MAY_BE_MUTATED=1; fi
PYTHONPATH="$PREFIX/src" python3 -m ispbandits --db "$DB_PATH" init
chmod 0600 "$DB_PATH"

WRAPPER_TMP="$BIN_DIR/.ispbandits.$$"
cat > "$WRAPPER_TMP" <<EOF
#!/usr/bin/env bash
# ISPbandits Dev managed wrapper
export ISPB_DB='$DB_PATH'
export PYTHONPATH='$PREFIX/src'
exec python3 -m ispbandits "\$@"
EOF
chmod 0755 "$WRAPPER_TMP"
"$WRAPPER_TMP" about
"$WRAPPER_TMP" verify
if [[ "$UPGRADE" == 1 ]]; then
  OLD_WRAPPER="$BIN_DIR/.ispbandits.previous.$$"
  cp -p -- "$WRAPPER" "$OLD_WRAPPER"
fi
mv -f -- "$WRAPPER_TMP" "$WRAPPER"
NEW_WRAPPER_INSTALLED=1

if [[ "$FULL" == 1 ]]; then
  deploy_xray
fi

MOTD="$ETC_DIR/motd"
[[ ! -L "$MOTD" ]] || fail "Refusing to append to a symlinked MOTD: $MOTD"
if [[ ! -e "$MOTD" ]]; then
  install -m 0644 /dev/null "$MOTD"
fi
if ! grep -Fq '[ISPbandits Dev]' "$MOTD"; then
  printf '\n[ISPbandits Dev]\n© 2026 ISPbandits Dev. All rights reserved. Proprietary software.\nRun sudo ispbandits to open the interactive administration menu.\n' >> "$MOTD"
fi

cat > "$MARKER" <<EOF
managed_by=ISPbandits-Dev-install.sh
version=1.0.0-alpha.1
database_path=$DB_PATH
full_xray=$DEPLOY_FULL
ssh_meter_experimental=$DEPLOY_SSH_METER
domain=$DEPLOY_DOMAIN
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
chmod 0644 "$MARKER"

echo
if (( UPGRADE )); then
  echo "ISPbandits Dev v1 alpha code upgrade completed successfully."
elif [[ "$DEPLOY_FULL" == 1 ]]; then
  echo "ISPbandits Dev v1 alpha full deployment installed successfully."
else
  echo "ISPbandits Dev v1 alpha foundation installed successfully."
fi
echo "Application: $PREFIX"
echo "Database:    $DB_PATH"
echo "Admin menu:  sudo ispbandits"
echo "Copyright notice: $MOTD"
if [[ "$DEPLOY_FULL" == 1 ]]; then
  echo "Xray:         TCP 443 (VLESS) and TCP 8443 (Trojan); open these in the VPS/cloud firewall if needed."
  if [[ "$DEPLOY_SSH_METER" == 1 ]]; then
    echo "SSH meter:    experimental OpenSSH network-byte meter enabled; validate accuracy before production use."
    echo "Quota scope:  measured lifetime SSH+Xray bytes; legacy usage excluded; Dropbear unsupported."
  else
    echo "SSH meter:    disabled by default; shared lifetime quota enforcement is unavailable."
  fi
else
  if (( UPGRADE )); then
    echo "Service topology was preserved; the existing full/foundation mode remains unchanged."
  else
    echo "Scope note: no SSH/Dropbear or Xray services were installed or configured."
  fi
fi
if (( UPGRADE )) && command -v systemctl >/dev/null 2>&1; then
  for unit in "${UPGRADE_TIMERS_ACTIVE[@]}"; do systemctl start "$unit"; done
  echo "Upgrade complete. Prior active managed timers were resumed; Xray/TLS/service topology was preserved."
fi
