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
PREFLIGHT_ONLY=0
DOMAIN=""
TLS_CERT=""
TLS_KEY=""
SELF_SIGNED=0
SSH_METER=0
CERTBOT_STANDALONE=0
SSH_TARGET_PORT=""
IPV6_AVAILABLE=0

usage() {
  cat <<'EOF'
Neverbandits alpha installer (publisher: ISPbandits Dev)

Usage:
  sudo ./install.sh --full --domain vpn.example.com (--cert-file CERT --key-file KEY | --self-signed) [--certbot-standalone] [--experimental-ssh-meter]
  sudo ./install.sh --preflight-only --domain vpn.example.com
  sudo ./install.sh --foundation-only [--upgrade] [--prefix PATH] [--db-path PATH]
                    [--bin-dir PATH] [--etc-dir PATH] [--backup-dir PATH]
  sudo ./install.sh --upgrade
  ./install.sh --check

Options:
  --upgrade         Code-only upgrade of a recognized managed install. Preserves
                    Xray/TLS/service topology, backs up SQLite, and rolls back on failure.
  --full            Install Xray, Nginx, SSH-over-WebSocket, VLESS/Trojan, TLS refresh,
                    usage collection, and daily expiry timers. Requires a domain and
                    certificate pair, or explicit --self-signed (testing only).
  --certbot-standalone  Install renewal hooks that briefly stop/start Nginx for the
                    standalone HTTP-01 Certbot method used by quick-install.sh.
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
  --preflight-only  Validate fresh-VPS SSH/Nginx/port safety gates without changing
                    packages, configs, or service state. Used by quick-install.
  -h, --help        Show this help.

Scope:
  --full installs Nginx on TCP 80/443, a loopback-only websockify-to-sshd bridge,
  VLESS WebSocket behind Nginx, Trojan TLS on TCP 8443, and a loopback-only API.
  It does not change admin SSH settings or cloud firewall rules. SSH account
  provisioning is available through `neverbandits account-create`. The optional
  experimental meter counts OpenSSH network bytes, not pure payload bytes; Dropbear
  is unsupported and quota enforcement requires target-specific validation.
EOF
}

fail() { echo "install.sh: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --upgrade) UPGRADE=1; shift ;;
    --full) FULL=1; shift ;;
    --preflight-only) PREFLIGHT_ONLY=1; FULL=1; shift ;;
    --experimental-ssh-meter) SSH_METER=1; shift ;;
    --certbot-standalone) CERTBOT_STANDALONE=1; shift ;;
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
if [[ "$CERTBOT_STANDALONE" == 1 && "$FULL" != 1 ]]; then
  fail "--certbot-standalone requires --full."
fi
if (( PREFLIGHT_ONLY && CHECK_ONLY )); then
  fail "--preflight-only and --check are separate modes; do not combine them."
fi

if (( UPGRADE )); then
  [[ "$FULL" == 0 && "$SSH_METER" == 0 && "$CERTBOT_STANDALONE" == 0 && "$SELF_SIGNED" == 0 && -z "$DOMAIN$TLS_CERT$TLS_KEY" ]] ||
    fail "--upgrade is code-only; do not combine it with fresh-deployment, TLS, or meter-topology options."
fi

if [[ "$CHECK_ONLY" != 1 && "$FULL" == 1 ]]; then
  [[ -n "$DOMAIN" ]] || fail "--full requires --domain."
  [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]] || fail "Invalid DNS hostname: $DOMAIN"
  if (( PREFLIGHT_ONLY )); then
    [[ -z "$TLS_CERT$TLS_KEY" && "$SELF_SIGNED" == 0 && "$CERTBOT_STANDALONE" == 0 ]] ||
      fail "--preflight-only accepts only --domain and is not a certificate provisioning mode."
  else
    if (( CERTBOT_STANDALONE )); then
      [[ "$TLS_CERT" == "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" &&
         "$TLS_KEY" == "/etc/letsencrypt/live/$DOMAIN/privkey.pem" ]] ||
        fail "--certbot-standalone requires the standard /etc/letsencrypt/live/$DOMAIN certificate paths."
    fi
    if (( SELF_SIGNED )); then
      [[ -z "$TLS_CERT" && -z "$TLS_KEY" ]] || fail "Use either --self-signed or --cert-file/--key-file, not both."
    else
      [[ -n "$TLS_CERT" && -n "$TLS_KEY" ]] || fail "--full requires both --cert-file and --key-file, or --self-signed."
    fi
  fi
  for path in "$TLS_CERT" "$TLS_KEY"; do
    [[ -z "$path" ]] && continue
    [[ "$path" == /* && "$path" != *"'"* && "$path" != *$'\n'* ]] || fail "TLS paths must be absolute and cannot contain single quotes or newlines."
  done
fi

[[ -f "$SOURCE_DIR/src/ispbandits/__init__.py" ]] || fail "Run from the project checkout or extracted source bundle; src/ispbandits is missing."
[[ -f "$SOURCE_DIR/pyproject.toml" && -f "$SOURCE_DIR/LICENSE.proprietary" ]] || fail "Project metadata or proprietary notice is missing."
if (( FULL )); then
  [[ -f "$SOURCE_DIR/nginx/neverbandits.conf.in" && -f "$SOURCE_DIR/systemd/neverbandits-websockify.service.in" ]] || fail "Neverbandits Nginx/websockify templates are missing."
fi

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
  if [[ "$CHECK_ONLY" == 1 || "$PREFLIGHT_ONLY" == 1 ]]; then fail "Python 3 is required for checks/preflight; this mode will not install it."; fi
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
  (cd "$SOURCE_DIR" && bash -n install.sh quick-install.sh scripts/refresh-tls.sh scripts/certbot-stop-nginx.sh scripts/certbot-start-nginx.sh scripts/certbot-refresh-tls.sh && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 - <<'PY'
import ast
from pathlib import Path
for root in (Path("src"), Path("tests"), Path("scripts")):
    for source in root.rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
PY
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 -m unittest discover -s tests -v)
  echo "Source checks passed. No system files were changed."
  exit 0
fi

[[ ${EUID} -eq 0 ]] || fail "Run installation with sudo/root."

# Validate the exact source before touching deployment paths.
(cd "$SOURCE_DIR" && bash -n install.sh quick-install.sh scripts/refresh-tls.sh scripts/certbot-stop-nginx.sh scripts/certbot-start-nginx.sh scripts/certbot-refresh-tls.sh && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 - <<'PY'
import ast
from pathlib import Path
for root in (Path("src"), Path("tests"), Path("scripts")):
    for source in root.rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
PY
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_DIR/src" python3 -m unittest discover -s tests -v)

if [[ "$FULL" == 1 ]]; then
  [[ "$PREFIX" == /opt/ispbandits-dev && "$DB_PATH" == /var/lib/ispbandits-dev/database.db &&
     "$BIN_DIR" == /usr/local/bin && "$ETC_DIR" == /etc ]] || fail "--full uses fixed service paths; remove custom --prefix/--db-path/--bin-dir/--etc-dir options."
  command -v systemctl >/dev/null 2>&1 || fail "--full requires systemd/systemctl."
  if (( PREFLIGHT_ONLY == 0 )); then
    command -v openssl >/dev/null 2>&1 || fail "OpenSSL is required for certificate checks; install it before running the full installer."
  fi
  FULL_INSTALL_PACKAGES=()
  command -v curl >/dev/null 2>&1 || FULL_INSTALL_PACKAGES+=(curl)
  command -v unzip >/dev/null 2>&1 || FULL_INSTALL_PACKAGES+=(unzip)
  command -v sshd >/dev/null 2>&1 || fail "Full Neverbandits requires the VPS's existing OpenSSH server; it will not install or reconfigure sshd."
  for unit in ssh.service sshd.service ssh.socket; do
    if systemctl is-active --quiet "$unit"; then SSHD_ACTIVE=1; break; fi
  done
  [[ "${SSHD_ACTIVE:-0}" == 1 ]] || fail "The existing OpenSSH service/socket must be active before full installation."
  SSHD_EFFECTIVE_CONFIG=$(sshd -T 2>/dev/null) || fail "The existing sshd configuration could not be evaluated with sshd -T."
  SSH_TARGET_PORT=$(awk 'tolower($1)=="port" {print $2; exit}' <<<"$SSHD_EFFECTIVE_CONFIG")
  [[ "$SSH_TARGET_PORT" =~ ^[0-9]+$ ]] && (( SSH_TARGET_PORT >= 1 && SSH_TARGET_PORT <= 65535 )) ||
    fail "Could not determine a valid effective sshd port from sshd -T."
  if ! python3 - "$SSH_TARGET_PORT" <<'PY'
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2):
    pass
PY
  then
    fail "The existing sshd must accept TCP connections on 127.0.0.1:$SSH_TARGET_PORT for the loopback WebSocket bridge."
  fi
  if systemctl is-active --quiet nginx.service; then
    fail "Nginx is already active. Full install is for a fresh VPS and will not replace or merge an existing web service."
  fi
  command -v nginx >/dev/null 2>&1 && fail "Nginx is already installed. Full install is for a fresh VPS and will not adopt or start an existing web server."
  command -v websockify >/dev/null 2>&1 && fail "websockify is already installed. Full install is for a fresh VPS and will not adopt existing bridge software."
  [[ ! -e /etc/nginx ]] || fail "Nginx configuration directory already exists; refusing to reuse or replace it."
  if [[ -d /etc/nginx/sites-enabled ]]; then
    for site in /etc/nginx/sites-enabled/*; do
      [[ -e "$site" || -L "$site" ]] || continue
      [[ "$(basename -- "$site")" == default ]] || fail "Non-default Nginx site is enabled ($site); refusing to modify an existing web configuration."
    done
  fi
  if [[ -d /etc/nginx/conf.d ]]; then
    for config in /etc/nginx/conf.d/*.conf; do
      [[ -e "$config" ]] || continue
      fail "Existing Nginx conf.d file found ($config); refusing to alter an existing Nginx configuration."
    done
  fi
  if (( SSH_METER )); then
    command -v nft >/dev/null 2>&1 || FULL_INSTALL_PACKAGES+=(nftables)
  fi
  if (( SELF_SIGNED == 0 && PREFLIGHT_ONLY == 0 )); then
    [[ -r "$TLS_CERT" && -r "$TLS_KEY" ]] || fail "TLS certificate/key files must be readable: $TLS_CERT / $TLS_KEY"
    openssl x509 -in "$TLS_CERT" -noout -checkhost "$DOMAIN" >/dev/null || fail "Certificate does not cover --domain $DOMAIN."
    openssl x509 -in "$TLS_CERT" -noout -checkend 86400 >/dev/null || fail "TLS certificate expires within 24 hours."
    cert_pub=$(openssl x509 -in "$TLS_CERT" -noout -pubkey | openssl pkey -pubin -outform DER | sha256sum | awk '{print $1}')
    key_pub=$(openssl pkey -passin pass: -in "$TLS_KEY" -pubout -outform DER | sha256sum | awk '{print $1}')
    [[ "$cert_pub" == "$key_pub" ]] || fail "TLS certificate and private key do not match."
  fi
  if python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.bind(("::1", 0))
s.close()
PY
  then IPV6_AVAILABLE=1; fi
  python3 - "$IPV6_AVAILABLE" <<'PY' || fail "At least one required IPv4/IPv6 listener port is already occupied."
import socket, sys
checks = [(socket.AF_INET, "0.0.0.0", p) for p in (80, 443, 8443)]
checks += [(socket.AF_INET, "127.0.0.1", p) for p in (10001, 10085)]
if sys.argv[1] == "1":
    checks += [(socket.AF_INET6, "::", p) for p in (80, 443, 8443)]
for family, host, port in checks:
    s=socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((host,port))
    except OSError:
        raise SystemExit(1)
    finally:
        s.close()
PY
  [[ ! -e /opt/ispbandits-dev/INSTALL_INFO && ! -e /opt/ispbandits-dev/src/ispbandits ]] ||
    fail "An application install already exists; full deployment is for a fresh VPS."
  [[ ! -e /var/lib/ispbandits-dev/database.db ]] || fail "The managed database path already exists; refusing to overwrite it."
  [[ ! -e /usr/local/bin/ispbandits && ! -e /usr/local/bin/neverbandits ]] ||
    fail "A managed CLI path already exists; refusing to overwrite it."
  [[ ! -e /etc/ispbandits-dev && ! -e /usr/local/bin/xray && ! -e /usr/local/sbin/ispbandits-dev-refresh-tls ]] ||
    fail "Existing Xray/Neverbandits files were found; full install requires a fresh VPS."
  for unit in neverbandits-websockify.service ispbandits-xray.service ispbandits-tls-refresh.service \
              ispbandits-tls-refresh.timer ispbandits-expire.service ispbandits-expire.timer \
              ispbandits-collect.service ispbandits-collect.timer ispbandits-ssh-meter.service \
              ispbandits-ssh-meter.timer ispbandits-ssh-collect.service ispbandits-ssh-collect.timer; do
    [[ ! -e "/etc/systemd/system/$unit" ]] || fail "Existing managed unit found (/etc/systemd/system/$unit); refusing to adopt it."
  done
  id xray >/dev/null 2>&1 && fail "System user xray already exists; refusing to reuse it."
  id ispbd-ws >/dev/null 2>&1 && fail "System user ispbd-ws already exists; refusing to reuse it."
  getent group xray >/dev/null 2>&1 && fail "Group xray already exists; refusing to reuse it."
  getent group ispbd-ws >/dev/null 2>&1 && fail "Group ispbd-ws already exists; refusing to reuse it."
  if (( CERTBOT_STANDALONE )); then
    for hook in /etc/letsencrypt/renewal-hooks/pre/neverbandits-stop-nginx \
                /etc/letsencrypt/renewal-hooks/post/neverbandits-start-nginx \
                /etc/letsencrypt/renewal-hooks/deploy/neverbandits-refresh-tls; do
      [[ ! -e "$hook" ]] || fail "Certbot hook already exists ($hook); refusing to overwrite it."
    done
    if (( PREFLIGHT_ONLY == 0 )); then
      command -v certbot >/dev/null 2>&1 || fail "--certbot-standalone requires Certbot to be installed before full deployment."
      systemctl list-unit-files certbot.timer --no-legend 2>/dev/null | grep -q '^certbot.timer' ||
        fail "Certbot renewal timer is unavailable; install Certbot with its systemd timer before deployment."
    fi
  fi
  if (( PREFLIGHT_ONLY )); then
    echo "Fresh-VPS preflight passed. No packages, configurations, or services were changed."
    exit 0
  fi
  if ((${#FULL_INSTALL_PACKAGES[@]})); then
    command -v apt-get >/dev/null 2>&1 || fail "Missing required packages (${FULL_INSTALL_PACKAGES[*]}) and apt-get is unavailable."
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${FULL_INSTALL_PACKAGES[@]}"
  fi
fi

MARKER="$PREFIX/INSTALL_INFO"
WRAPPER="$BIN_DIR/ispbandits"
NEVERBANDITS_WRAPPER="$BIN_DIR/neverbandits"
DEPLOY_FULL=$FULL
DEPLOY_SSH_METER=$SSH_METER
DEPLOY_DOMAIN=$DOMAIN
DEPLOY_SSH_WEBSOCKET=$FULL
DEPLOY_SSH_TARGET_PORT=$SSH_TARGET_PORT
if [[ "$UPGRADE" == 1 ]]; then
  [[ -f "$MARKER" ]] || fail "No installer marker at $MARKER; refusing to upgrade an unknown installation."
  grep -Fxq 'managed_by=ISPbandits-Dev-install.sh' "$MARKER" || fail "Install marker is not recognized."
  grep -Fxq "database_path=$DB_PATH" "$MARKER" || fail "Requested database path differs from the installed path."
  grep -Fxq 'version=1.0.0-alpha.2' "$MARKER" || fail "This release changes network topology. Code-only upgrade from an earlier alpha is unsupported; install on a fresh VPS."
  DEPLOY_FULL=$(sed -n 's/^full_xray=//p' "$MARKER" | head -n1)
  DEPLOY_SSH_METER=$(sed -n 's/^ssh_meter_experimental=//p' "$MARKER" | head -n1)
  DEPLOY_DOMAIN=$(sed -n 's/^domain=//p' "$MARKER" | head -n1)
  DEPLOY_SSH_WEBSOCKET=$(sed -n 's/^ssh_websocket=//p' "$MARKER" | head -n1)
  DEPLOY_SSH_TARGET_PORT=$(sed -n 's/^ssh_target_port=//p' "$MARKER" | head -n1)
  [[ "$DEPLOY_FULL" == 0 || "$DEPLOY_FULL" == 1 ]] || fail "Install marker has an invalid full_xray value."
  [[ "$DEPLOY_SSH_WEBSOCKET" == 0 || "$DEPLOY_SSH_WEBSOCKET" == 1 ]] || fail "Install marker has an invalid ssh_websocket value."
  if [[ "$DEPLOY_FULL" == 1 && "$DEPLOY_SSH_WEBSOCKET" != 1 ]]; then fail "Managed full install has no SSH-WebSocket topology; use a fresh VPS rather than upgrading in place."; fi
  [[ "$DEPLOY_SSH_METER" == 0 || "$DEPLOY_SSH_METER" == 1 ]] || fail "Install marker has an invalid experimental meter value."
  if [[ "$DEPLOY_SSH_METER" == 1 && "$DEPLOY_FULL" != 1 ]]; then fail "Install marker has inconsistent meter deployment state."; fi
  [[ -f "$DB_PATH" ]] || fail "Installed database is missing; refusing an in-place upgrade."
  [[ -f "$WRAPPER" ]] && grep -Fq 'ISPbandits Dev managed wrapper' "$WRAPPER" || fail "Managed CLI wrapper is missing or has been replaced."
  [[ -f "$NEVERBANDITS_WRAPPER" ]] && grep -Fq 'Neverbandits managed wrapper' "$NEVERBANDITS_WRAPPER" || fail "Neverbandits CLI wrapper is missing or has been replaced."
else
  [[ ! -e "$MARKER" && ! -e "$PREFIX/src/ispbandits" ]] || fail "An installation already exists at $PREFIX; use --upgrade only for a recognized managed installation."
  [[ ! -e "$DB_PATH" ]] || fail "Database path already exists: $DB_PATH. Refusing to overwrite or migrate it implicitly."
  [[ ! -e "$WRAPPER" ]] || fail "CLI path already exists: $WRAPPER. Refusing to overwrite it."
  [[ ! -e "$NEVERBANDITS_WRAPPER" ]] || fail "CLI path already exists: $NEVERBANDITS_WRAPPER. Refusing to overwrite it."
fi

ensure_dir() {
  local directory="$1" mode="$2"
  [[ -d "$directory" ]] || install -d -m "$mode" "$directory"
}

deploy_xray() {
  [[ ! -e /etc/ispbandits-dev ]] || fail "Xray config directory already exists; refusing to reuse unknown TLS/config state."
  [[ ! -e /etc/systemd/system/neverbandits-websockify.service ]] || fail "Neverbandits websockify unit already exists; refusing to replace it."
  [[ ! -e /etc/nginx/sites-available/neverbandits && ! -e /etc/nginx/sites-enabled/neverbandits ]] || fail "Neverbandits Nginx site already exists; refusing to replace it."
  [[ ! -e /var/www/neverbandits-acme ]] || fail "Neverbandits ACME webroot already exists; refusing to reuse it."
  id ispbd-ws >/dev/null 2>&1 && fail "System user ispbd-ws already exists; refusing to reuse it."
  [[ ! -e /etc/systemd/system/ispbandits-xray.service &&
     ! -e /etc/systemd/system/ispbandits-tls-refresh.service &&
     ! -e /etc/systemd/system/ispbandits-tls-refresh.timer &&
     ! -e /etc/systemd/system/ispbandits-expire.service &&
     ! -e /etc/systemd/system/ispbandits-expire.timer &&
     ! -e /etc/systemd/system/ispbandits-collect.service &&
     ! -e /etc/systemd/system/ispbandits-collect.timer &&
     ! -e /etc/systemd/system/neverbandits-websockify.service &&
     ! -e /etc/systemd/system/ispbandits-ssh-meter.service &&
     ! -e /etc/systemd/system/ispbandits-ssh-meter.timer &&
     ! -e /etc/systemd/system/ispbandits-ssh-collect.service &&
     ! -e /etc/systemd/system/ispbandits-ssh-collect.timer ]] || fail "An ISPbandits systemd unit already exists."
  [[ ! -e /usr/local/sbin/ispbandits-dev-refresh-tls ]] || fail "TLS refresh executable already exists."
  getent group ispbd-ws >/dev/null 2>&1 && fail "Group ispbd-ws already exists; refusing to reuse it."
  getent group xray >/dev/null 2>&1 && fail "Group 'xray' already exists; refusing to reuse it."
  id xray >/dev/null 2>&1 && fail "User 'xray' already exists; refusing to reuse it."
  [[ ! -e /usr/local/bin/xray ]] || fail "/usr/local/bin/xray already exists; review/remove it manually before the pinned full installation."

  if systemctl is-enabled --quiet nginx.service; then NGINX_ENABLED_BEFORE=1; else NGINX_ENABLED_BEFORE=0; fi
  NGINX_SERVICE_TOUCHED=1
  DEBIAN_FRONTEND=noninteractive apt-get update
  NGINX_PACKAGES_INSTALLED=1
  DEBIAN_FRONTEND=noninteractive apt-get install -y nginx websockify
  [[ -x /usr/bin/websockify && -x /usr/sbin/nginx ]] || fail "Distro Nginx or websockify executable is missing after package installation."
  systemctl stop nginx.service >/dev/null 2>&1 || true

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

  WEBSOCKIFY_USER_CREATED=1
  useradd --system --user-group --no-create-home --shell /usr/sbin/nologin ispbd-ws
  install -d -o root -g root -m 0755 /var/www/neverbandits-acme
  ACME_WEBROOT_CREATED=1
  NGINX_FILES_INSTALLED=1
  if (( IPV6_AVAILABLE )); then
    sed "s/@DOMAIN@/$DOMAIN/g" "$SOURCE_DIR/nginx/neverbandits.conf.in" > /etc/nginx/sites-available/neverbandits
  else
    sed -e "s/@DOMAIN@/$DOMAIN/g" -e '/^[[:space:]]*listen \[::\]:/d' \
      "$SOURCE_DIR/nginx/neverbandits.conf.in" > /etc/nginx/sites-available/neverbandits
  fi
  chown root:root /etc/nginx/sites-available/neverbandits
  chmod 0644 /etc/nginx/sites-available/neverbandits
  ln -s /etc/nginx/sites-available/neverbandits /etc/nginx/sites-enabled/neverbandits
  WEBSOCKIFY_UNIT_INSTALLED=1
  sed "s/@SSH_PORT@/$SSH_TARGET_PORT/g" "$SOURCE_DIR/systemd/neverbandits-websockify.service.in" \
    > /etc/systemd/system/neverbandits-websockify.service
  chown root:root /etc/systemd/system/neverbandits-websockify.service
  chmod 0644 /etc/systemd/system/neverbandits-websockify.service

  if (( CERTBOT_STANDALONE )); then
    command -v certbot >/dev/null 2>&1 || fail "--certbot-standalone requires Certbot to be installed by quick-install.sh."
    systemctl list-unit-files certbot.timer --no-legend | grep -q '^certbot.timer' ||
      fail "Certbot renewal timer is unavailable; refusing to install without automatic certificate renewal."
    for hook in /etc/letsencrypt/renewal-hooks/pre/neverbandits-stop-nginx \
                /etc/letsencrypt/renewal-hooks/post/neverbandits-start-nginx \
                /etc/letsencrypt/renewal-hooks/deploy/neverbandits-refresh-tls; do
      [[ ! -e "$hook" ]] || fail "Certbot hook already exists ($hook); refusing to overwrite it."
    done
    for dir in /etc/letsencrypt/renewal-hooks/pre /etc/letsencrypt/renewal-hooks/post /etc/letsencrypt/renewal-hooks/deploy; do
      [[ -d "$dir" ]] || install -d -o root -g root -m 0755 "$dir"
    done
    CERTBOT_HOOKS_INSTALLED=1
    install -o root -g root -m 0755 "$SOURCE_DIR/scripts/certbot-stop-nginx.sh" \
      /etc/letsencrypt/renewal-hooks/pre/neverbandits-stop-nginx
    install -o root -g root -m 0755 "$SOURCE_DIR/scripts/certbot-start-nginx.sh" \
      /etc/letsencrypt/renewal-hooks/post/neverbandits-start-nginx
    install -o root -g root -m 0755 "$SOURCE_DIR/scripts/certbot-refresh-tls.sh" \
      /etc/letsencrypt/renewal-hooks/deploy/neverbandits-refresh-tls
  fi

  /usr/local/sbin/ispbandits-dev-refresh-tls
  "$WRAPPER" xray-config \
    --cert-file /etc/ispbandits-dev/tls/current/fullchain.pem \
    --key-file /etc/ispbandits-dev/tls/current/privkey.pem \
    --xray-bin /usr/local/bin/xray
  /usr/sbin/nginx -t || fail "Rendered Neverbandits Nginx configuration did not pass nginx -t."

  systemctl daemon-reload
  SSHWS_SERVICES_ENABLED=1
  systemctl enable --now neverbandits-websockify.service
  systemctl enable --now nginx.service
  if (( CERTBOT_STANDALONE )); then
    systemctl is-enabled --quiet certbot.timer 2>/dev/null && CERTBOT_TIMER_ENABLED_BEFORE=1 || true
    systemctl is-active --quiet certbot.timer 2>/dev/null && CERTBOT_TIMER_ACTIVE_BEFORE=1 || true
    CERTBOT_TIMER_TOUCHED=1
    systemctl enable --now certbot.timer
  fi
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
  systemctl is-active --quiet nginx.service || fail "Nginx frontend did not become active."
  systemctl is-active --quiet neverbandits-websockify.service || fail "SSH-WebSocket bridge did not become active."
  echo "Neverbandits services active. SSH-WS: TCP 80/443; VLESS WS: TCP 443; Trojan TLS: TCP 8443; API: loopback only."
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
OLD_NEVERBANDITS_WRAPPER=""
MOVED_OLD=0
NEW_PACKAGE_INSTALLED=0
NEW_WRAPPER_INSTALLED=0
NEW_NEVERBANDITS_WRAPPER_INSTALLED=0
NEW_INSTALL=$((1 - UPGRADE))
XRAY_BINARY_INSTALLED=0
XRAY_GROUP_CREATED=0
XRAY_USER_CREATED=0
XRAY_FILES_INSTALLED=0
XRAY_UNITS_INSTALLED=0
XRAY_SERVICE_ENABLED=0
SSH_METER_UNITS_INSTALLED=0
NGINX_ENABLED_BEFORE=0
NGINX_SERVICE_TOUCHED=0
NGINX_PACKAGES_INSTALLED=0
NGINX_FILES_INSTALLED=0
WEBSOCKIFY_UNIT_INSTALLED=0
WEBSOCKIFY_USER_CREATED=0
ACME_WEBROOT_CREATED=0
CERTBOT_HOOKS_INSTALLED=0
CERTBOT_TIMER_ENABLED_BEFORE=0
CERTBOT_TIMER_ACTIVE_BEFORE=0
CERTBOT_TIMER_TOUCHED=0
SSHWS_SERVICES_ENABLED=0
XRAY_TMP=""
UPGRADE_BACKUP=""
UPGRADE_TIMERS_ACTIVE=()
UPGRADE_DATABASE_MAY_BE_MUTATED=0

cleanup() {
  local rc=$?
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then rm -rf -- "$STAGE_DIR"; fi
  if [[ -n "$XRAY_TMP" && -d "$XRAY_TMP" ]]; then rm -rf -- "$XRAY_TMP"; fi
  if (( rc != 0 )); then
    if (( SSHWS_SERVICES_ENABLED )); then
      systemctl disable --now neverbandits-websockify.service >/dev/null 2>&1 || true
    fi
    if (( NGINX_SERVICE_TOUCHED )); then
      systemctl stop nginx.service >/dev/null 2>&1 || true
      if (( NGINX_ENABLED_BEFORE == 0 )); then systemctl disable nginx.service >/dev/null 2>&1 || true; fi
    fi
    if (( CERTBOT_HOOKS_INSTALLED )); then
      rm -f /etc/letsencrypt/renewal-hooks/pre/neverbandits-stop-nginx \
            /etc/letsencrypt/renewal-hooks/post/neverbandits-start-nginx \
            /etc/letsencrypt/renewal-hooks/deploy/neverbandits-refresh-tls
    fi
    if (( NGINX_FILES_INSTALLED )); then
      rm -f /etc/nginx/sites-enabled/neverbandits /etc/nginx/sites-available/neverbandits
    fi
    if (( WEBSOCKIFY_UNIT_INSTALLED )); then rm -f /etc/systemd/system/neverbandits-websockify.service; fi
    if (( WEBSOCKIFY_USER_CREATED )); then
      userdel --force ispbd-ws >/dev/null 2>&1 || true
      groupdel ispbd-ws >/dev/null 2>&1 || true
    fi
    if (( ACME_WEBROOT_CREATED )); then rm -rf /var/www/neverbandits-acme; fi
    if (( NGINX_PACKAGES_INSTALLED )); then
      local installed_network_packages=()
      for package in nginx nginx-common nginx-core nginx-full nginx-light nginx-extras websockify; do
        if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
          installed_network_packages+=("$package")
        fi
      done
      if ((${#installed_network_packages[@]})); then
        DEBIAN_FRONTEND=noninteractive apt-get purge -y "${installed_network_packages[@]}" >/dev/null 2>&1 ||
          echo "WARNING: automatic rollback could not purge newly installed Nginx/websockify packages; review them before retrying." >&2
      fi
    fi
    if (( CERTBOT_TIMER_TOUCHED )); then
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
    systemctl daemon-reload >/dev/null 2>&1 || true
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
    if (( NEW_NEVERBANDITS_WRAPPER_INSTALLED )); then rm -f -- "$NEVERBANDITS_WRAPPER"; fi
    if [[ -n "$OLD_WRAPPER" && -e "$OLD_WRAPPER" ]]; then mv -- "$OLD_WRAPPER" "$WRAPPER"; fi
    if [[ -n "$OLD_NEVERBANDITS_WRAPPER" && -e "$OLD_NEVERBANDITS_WRAPPER" ]]; then mv -- "$OLD_NEVERBANDITS_WRAPPER" "$NEVERBANDITS_WRAPPER"; fi
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
  if (( rc == 0 )) && [[ -n "$OLD_NEVERBANDITS_WRAPPER" && -e "$OLD_NEVERBANDITS_WRAPPER" ]]; then rm -f -- "$OLD_NEVERBANDITS_WRAPPER"; fi
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
  OLD_NEVERBANDITS_WRAPPER="$BIN_DIR/.neverbandits.previous.$$"
  cp -p -- "$NEVERBANDITS_WRAPPER" "$OLD_NEVERBANDITS_WRAPPER"
fi
mv -f -- "$WRAPPER_TMP" "$WRAPPER"
NEW_WRAPPER_INSTALLED=1

NEVERBANDITS_WRAPPER_TMP="$BIN_DIR/.neverbandits.$$"
cat > "$NEVERBANDITS_WRAPPER_TMP" <<EOF
#!/usr/bin/env bash
# Neverbandits managed wrapper
export ISPB_DB='$DB_PATH'
export PYTHONPATH='$PREFIX/src'
exec python3 -m ispbandits "\$@"
EOF
chmod 0755 "$NEVERBANDITS_WRAPPER_TMP"
mv -f -- "$NEVERBANDITS_WRAPPER_TMP" "$NEVERBANDITS_WRAPPER"
NEW_NEVERBANDITS_WRAPPER_INSTALLED=1

if [[ "$FULL" == 1 ]]; then
  deploy_xray
fi

MOTD="$ETC_DIR/motd"
[[ ! -L "$MOTD" ]] || fail "Refusing to append to a symlinked MOTD: $MOTD"
if [[ ! -e "$MOTD" ]]; then
  install -m 0644 /dev/null "$MOTD"
fi
if ! grep -Fq '[Neverbandits]' "$MOTD"; then
  printf '\n[Neverbandits]\n© 2026 ISPbandits Dev. All rights reserved. Proprietary software.\nRun sudo neverbandits to open the interactive administration menu. Legacy alias: sudo ispbandits.\n' >> "$MOTD"
fi

cat > "$MARKER" <<EOF
managed_by=ISPbandits-Dev-install.sh
version=1.0.0-alpha.2
database_path=$DB_PATH
full_xray=$DEPLOY_FULL
ssh_meter_experimental=$DEPLOY_SSH_METER
ssh_websocket=$DEPLOY_SSH_WEBSOCKET
ssh_target_port=$DEPLOY_SSH_TARGET_PORT
domain=$DEPLOY_DOMAIN
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
chmod 0644 "$MARKER"

echo
if (( UPGRADE )); then
  echo "Neverbandits alpha-2 code upgrade completed successfully."
elif [[ "$DEPLOY_FULL" == 1 ]]; then
  echo "Neverbandits alpha-2 full deployment installed successfully."
else
  echo "Neverbandits alpha-2 foundation installed successfully."
fi
echo "Application: $PREFIX"
echo "Database:    $DB_PATH"
echo "Admin menu:  sudo neverbandits (legacy alias: sudo ispbandits)"
echo "Copyright notice: $MOTD"
if [[ "$DEPLOY_FULL" == 1 ]]; then
  echo "Network:      TCP 80 (SSH-WS), TCP 443 (SSH-WSS/VLESS-WS), TCP 8443 (Trojan TLS)."
  echo "SSH backend:  127.0.0.1:$DEPLOY_SSH_TARGET_PORT; existing sshd configuration was not changed."
  echo "Firewall:     allow TCP 80, 443, and 8443 in the VPS/cloud firewall if required."
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
  echo "Scope note: foundation-only mode installs no SSH, Nginx, or Xray network services."
  fi
fi
if (( UPGRADE )) && command -v systemctl >/dev/null 2>&1; then
  for unit in "${UPGRADE_TIMERS_ACTIVE[@]}"; do systemctl start "$unit"; done
  echo "Upgrade complete. Prior active managed timers were resumed; Xray/TLS/service topology was preserved."
fi
