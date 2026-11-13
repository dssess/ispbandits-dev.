#!/usr/bin/env bash
# Installed only for the Neverbandits standalone Certbot deployment.
set -Eeuo pipefail
exec /usr/local/sbin/ispbandits-dev-refresh-tls
