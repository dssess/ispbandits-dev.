#!/usr/bin/env bash
# © 2026 ISPbandits Dev. All rights reserved. Proprietary software.
# Guided bootstrap for a fresh Ubuntu 22/24 or Debian 12/13 VPS.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

REPO_ARCHIVE="${ISPB_SOURCE_URL:-https://codeload.github.com/dssess/ispbandits-dev./tar.gz/refs/heads/main}"
DOMAIN=""
EMAIL=""
CERT_FILE=""
KEY_FILE=""
MODE=""

usage() {
  cat <<'EOF'
ISPbandits Dev v1 alpha quick installer

Usage:
  sudo bash quick-install.sh [--domain HOSTNAME] [--email EMAIL]
                            [--cert-file CERT --key-file KEY | --self-signed]

If no TLS option is supplied, the script interactively offers Let's Encrypt,
existing certificate files, or a self-signed certificate (testing only).

This installs on a fresh VPS only. It downloads the latest main branch and runs
install.sh --full. It does not configure a cloud-provider firewall.
EOF
}

fail() { echo "quick-install.sh: $*" >&2; exit 1; }

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
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

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
disposable VPS. It will install Xray and may occupy TCP 443 and 8443. SSH per-user
traffic metering and quota enforcement are not implemented.

Before continuing, point $DOMAIN to this VPS, ensure TCP 443 and 8443 are available,
and allow those ports through the VPS/provider firewall. Let's Encrypt additionally
needs inbound TCP 80 for certificate issuance and renewal. The script cannot edit
your provider firewall. It will not alter administrator SSH settings.
EOF

[[ -r /dev/tty ]] || fail "Run this installer from an interactive terminal."
read -r -p "Continue on this fresh VPS? Type YES to proceed: " CONFIRM </dev/tty
[[ "$CONFIRM" == YES ]] || fail "Installation cancelled."

export DEBIAN_FRONTEND=noninteractive
apt-get update
if [[ "$MODE" == letsencrypt ]]; then
  apt-get install -y ca-certificates curl tar certbot
else
  apt-get install -y ca-certificates curl tar
fi

if [[ "$MODE" == letsencrypt ]]; then
  certbot certonly --standalone --non-interactive --agree-tos --email "$EMAIL" \
    --preferred-challenges http -d "$DOMAIN"
  if ! systemctl list-unit-files certbot.timer --no-legend | grep -q '^certbot.timer'; then
    fail "The certbot package did not provide certbot.timer; refusing to install without automatic certificate renewal."
  fi
  systemctl enable --now certbot.timer
  CERT_FILE="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  KEY_FILE="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TMP_DIR=""
cleanup() { [[ -z "$TMP_DIR" ]] || rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT
if [[ ! -f "$SOURCE_DIR/install.sh" || ! -f "$SOURCE_DIR/pyproject.toml" ]]; then
  TMP_DIR=$(mktemp -d /tmp/ispbandits-quick-install.XXXXXX)
  curl -fL --retry 3 --proto '=https' --tlsv1.2 "$REPO_ARCHIVE" -o "$TMP_DIR/source.tar.gz"
  mkdir "$TMP_DIR/source"
  # Reject unsafe archive paths before extracting the trusted GitHub source bundle.
  if tar -tzf "$TMP_DIR/source.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    fail "Downloaded source archive contains an unsafe path."
  fi
  tar -xzf "$TMP_DIR/source.tar.gz" --strip-components=1 -C "$TMP_DIR/source"
  SOURCE_DIR="$TMP_DIR/source"
fi
[[ -f "$SOURCE_DIR/install.sh" && -f "$SOURCE_DIR/pyproject.toml" ]] || fail "Project source is incomplete."

case "$MODE" in
  letsencrypt|existing)
    bash "$SOURCE_DIR/install.sh" --full --domain "$DOMAIN" --cert-file "$CERT_FILE" --key-file "$KEY_FILE"
    ;;
  self-signed)
    bash "$SOURCE_DIR/install.sh" --full --domain "$DOMAIN" --self-signed
    ;;
  *) fail "Internal TLS mode error." ;;
esac

cat <<'EOF'

Next: create an account with:
  sudo ispbandits account-create USERNAME --expires YYYY-MM-DD

Open/keep open TCP 443 and 8443 in the provider firewall. With Let's Encrypt,
keep TCP 80 reachable for automatic certificate renewal. Review the README for
limitations and management commands.
EOF

if [[ -t 0 && -x /usr/local/bin/ispbandits ]]; then
  /usr/local/bin/ispbandits menu
fi
