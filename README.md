# ISPbandits Dev — Unified VPS Manager

**Version:** 0.1.0-alpha
**Publisher:** ISPbandits Dev
**License:** Proprietary (`LICENSE.proprietary`)

<p align="center">
  <img src="assets/ispbandits-breathing-banner.gif" alt="A cyan and amber network loop softly breathing against a deep navy background" width="100%" />
</p>

<p align="center"><strong>One control plane. A steady pulse.</strong><br />
SSH · VLESS · Trojan · SQLite</p>

ISPbandits Dev is an early single-VPS management alpha combining Linux SSH account provisioning with Xray VLESS and Trojan identities, SQLite account records, Xray traffic collection, approximate SSH online-process visibility, backups, and expiry processing.

> **Alpha warning:** This software has not yet been validated through a complete live client connection on a production VPS. SSH per-user network-byte metering and quota enforcement are not implemented. Xray collection is enabled in Xray-only mode; do not treat reported SSH usage as measured. Deploy only to a fresh, disposable VPS for testing, not to an existing production server.

## Contents

- [Requirements](#requirements)
- [Quick install (fresh VPS)](#quick-install-fresh-vps)
- [Get the source](#get-the-source)
- [Pre-install checks](#pre-install-checks)
- [Install](#install)
- [Create accounts](#create-accounts)
- [Monitor and manage](#monitor-and-manage)
- [Back up and restore](#back-up-and-restore)
- [Development and tests](#development-and-tests)
- [Current limitations](#current-limitations)

## Requirements

For the full install, use a **fresh** Ubuntu 22.04/24.04 or Debian 12/13 VPS with systemd, root/sudo access, and internet access. The installer recognizes these releases, but clean-install and client-connectivity testing on every target is still a release gate.

Before full installation:

1. Point the chosen hostname's DNS A/AAAA records at the VPS.
2. For manual installation, have a TLS certificate and matching private key covering that hostname. The quick installer can obtain a Let's Encrypt certificate for you.
3. Ensure TCP ports **443** and **8443** are free. The local Xray API uses loopback port **10085** and should not be exposed publicly.
4. Allow inbound TCP 443 and 8443 in the provider/VPS firewall. The quick install's Let's Encrypt option also requires inbound TCP 80 for certificate issuance and renewal. The installer does not change cloud firewall rules.

The full installer does not change administrator SSH configuration or install/reconfigure OpenSSH/Dropbear. SSH accounts created by the manager rely on the server's existing SSH service.

## Quick install (fresh VPS)

For a guided full installation, run this on the **fresh VPS**. Review the script before executing it:

```bash
curl -fsSL https://raw.githubusercontent.com/dssess/ispbandits-dev./main/quick-install.sh -o quick-install.sh && less quick-install.sh && sudo bash quick-install.sh
```

The script asks for the hostname and TLS choice, installs prerequisites, downloads the project, and calls the existing checked full installer. The default Let's Encrypt path needs DNS already pointed at the VPS and TCP 80 reachable. You can instead supply existing certificate files or explicitly select a self-signed certificate for testing only. It pauses for a typed `YES` before changing the server. Do not run it on an existing NevermoreSSH or Nginx Bandit VPS.

If you already cloned the project, run `sudo bash quick-install.sh` from the checkout; it will reuse those local files rather than downloading the source again.

## Get the source

The GitHub repository name ends with a period, so the clone URL contains two periods before `.git`:

```bash
git clone https://github.com/dssess/ispbandits-dev..git
cd ispbandits-dev.
```

The repository is publicly readable; that visibility does not change the proprietary license or grant permission to reuse or redistribute the software.

If you received a source archive instead, verify its supplied SHA-256 checksum before extracting, then change to the extracted project directory. Do not copy a live database, credentials, TLS private key, or backup archive into the source tree.

## Pre-install checks

Review the installer and run its non-mutating checks first:

```bash
less install.sh
./install.sh --check
```

`--check` runs syntax, compilation, and unit checks without installing system files. The installer rejects unknown OS releases, occupied service ports, mismatched certificate/key pairs, and existing managed service state rather than replacing it.

## Install

### Full Xray server (fresh VPS)

Use the full mode only after DNS and TLS are ready. Replace the example hostname and paths with your own:

```bash
sudo ./install.sh --full \
  --domain vpn.example.com \
  --cert-file /etc/letsencrypt/live/vpn.example.com/fullchain.pem \
  --key-file /etc/letsencrypt/live/vpn.example.com/privkey.pem
```

The installer installs the pinned Xray release with checksum verification (amd64/arm64), creates a dedicated unprivileged Xray service, and configures:

- VLESS over TLS on TCP 443.
- Trojan over TLS on TCP 8443.
- Xray StatsService on loopback only at 127.0.0.1:10085.
- A TLS refresh timer, daily account-expiry reconciliation, and once-per-minute Xray-only traffic collection.

After install, open TCP **443** and **8443** in the cloud/provider firewall if required. Do not open port 10085 to the internet. Use `--self-signed` only for a temporary connectivity test; self-signed TLS is not suitable for production clients.

The full installer is for a fresh VPS only. It intentionally refuses full-mode in-place upgrades. Do not run it on the previously inspected NevermoreSSH or Nginx Bandit servers.

### Foundation-only install

For development or database/CLI testing without deploying Xray services:

```bash
sudo ./install.sh --foundation-only
```

This installs the package and initializes a root-protected database, but does not create a working VLESS/Trojan server. The default database is `/var/lib/ispbandits-dev/database.db`; application files are installed under `/opt/ispbandits-dev`.

## Create accounts

After installation, run the interactive numbered menu with:

```bash
sudo ispbandits
# or explicitly:
sudo ispbandits menu
```

An interactive quick install opens this menu when installation completes. It provides account creation, usage totals, approximate online SSH/Dropbear process visibility, managed service status, expiry processing, protected database backups, and a database integrity check. Administrative commands require root; the menu tells you when `sudo` is needed. The ordinary subcommands below remain available for direct use and automation.

Account creation takes a username and required expiry date (`YYYY-MM-DD`). The default protocol set is OpenSSH, VLESS, and Trojan:

```bash
sudo ispbandits account-create alice --expires 2027-01-01
```

The command creates a no-home Linux SSH account and distinct VLESS/Trojan credentials. It prints generated passwords/secrets once; save them securely. Never put them in a ticket, public issue, repository, or shell script. When the managed Xray service is active, new Xray identities are applied to its validated config; otherwise they are stored but will not accept connections until Xray is installed and activated.

Choose protocols explicitly when needed:

```bash
# Xray protocols only; no Linux SSH account
sudo ispbandits account-create xrayonly --expires 2027-01-01 --protocols vless trojan

# SSH only
sudo ispbandits account-create sshonly --expires 2027-01-01 --protocols openssh
```

Supported `--protocols` values are `openssh`, `dropbear`, `vless`, and `trojan`. Creating a Dropbear account does not install or configure Dropbear. This alpha does not yet provide complete renew/disable/delete workflows.

## Monitor and manage

```bash
# Show account-level usage totals. On a full install, Xray usage is collected;
# SSH bytes are not currently metered.
sudo ispbandits usage

# Show observed SSH/Dropbear daemon processes for managed accounts.
# This is approximate and is not a count of unique devices.
sudo ispbandits online

# Run due-account expiry reconciliation now (also scheduled daily).
sudo ispbandits expire

# Validate database integrity.
sudo ispbandits verify

# Show publisher/build information.
sudo ispbandits about
```

On a full installation, expired accounts are marked expired, Linux SSH passwords are locked and their processes terminated, and expired Xray identities are removed from the generated configuration. If Xray config reconciliation fails, the service is stopped fail-closed. Automatic quota enforcement is disabled.

The Xray traffic collector runs every minute in Xray-only mode. To run it manually:

```bash
sudo ispbandits collect --xray-only
```

## Back up and restore

Create a protected SQLite backup archive:

```bash
sudo ispbandits backup --dir /var/backups/ispbandits-dev
```

Restore from a previously created archive (the current database is backed up first when it exists):

```bash
sudo ispbandits restore /var/backups/ispbandits-dev/ispbandits-YYYYMMDDTHHMMSSZ.tar.gz \
  --dir /var/backups/ispbandits-dev
```

Keep backup files private: they contain account records and protocol credentials. The archive includes a checksum manifest and is written with restrictive permissions. This alpha's backup covers the SQLite database; it is **not** a complete system snapshot of service config, certificates, or OS accounts.

## Development and tests

Requires Python 3.10+ with SQLite support. The application has no third-party runtime dependencies for local development.

```bash
python3 -m venv .venv
. .venv/bin/activate
PYTHONPATH=src python -m ispbandits --db ./dev.db init
PYTHONPATH=src python -m ispbandits --db ./dev.db user-add demo --expires 2027-01-01
PYTHONPATH=src python -m ispbandits --db ./dev.db usage
PYTHONPATH=src python -m ispbandits --db ./dev.db backup --dir ./backups
PYTHONPATH=src python -m ispbandits --db ./dev.db verify
```

`user-add` creates only a database record; it does not provision a Linux or Xray identity. Run the test suite and syntax checks with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n install.sh scripts/refresh-tls.sh
./install.sh --check
```

GitHub Actions runs these checks on Python 3.10–3.13 for pushes, pull requests, and manual dispatch. CI does not deploy to a VPS.

## Current limitations

- SSH per-user network-byte metering is not implemented; `usage` cannot report truthful SSH network traffic.
- Bandwidth quotas are not enforced.
- VMess is not included; supported Xray protocols are VLESS and Trojan.
- The online SSH view reports approximate process observations, not unique users/devices or guaranteed sessions.
- Full deployment, TLS renewal, expiry, and client connectivity still need testing on disposable target VPSes across the intended OS releases.
- Full-mode in-place upgrades and service-aware backups/restores are not supported yet.

Report security concerns privately to the project owner; do not post real credentials, private keys, or customer data in public issues.

## Branding and copyright

© 2026 ISPbandits Dev. All rights reserved. Proprietary software. Unauthorized copying, redistribution, or resale is prohibited.
