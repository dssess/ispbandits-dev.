#!/usr/bin/env bash
# Installed only when quick-install.sh used Certbot's standalone authenticator.
set -Eeuo pipefail
/usr/bin/systemctl stop nginx.service
