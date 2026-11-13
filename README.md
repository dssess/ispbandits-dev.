# ISPbandits Dev — Unified VPS Manager

**Version:** 1.0.0-alpha.1
**Publisher:** ISPbandits Dev
**License:** Proprietary (`LICENSE.proprietary`)

<p align="center">
  <img src="assets/ispbandits-breathing-banner.gif" alt="A cyan and amber network loop softly breathing against a deep navy background" width="100%" />
</p>

<p align="center"><strong>One control plane. A steady pulse.</strong><br />
SSH · VLESS · Trojan · SQLite</p>

ISPbandits Dev is an early single-VPS management alpha combining Linux SSH account provisioning with Xray VLESS and Trojan identities, SQLite account records, Xray traffic collection, approximate SSH online-process visibility, backups, expiry processing, and an opt-in experimental OpenSSH traffic meter.

> **Alpha warning:** The OpenSSH per-user meter and shared lifetime quota enforcement are experimental and have not been validated on a live VPS. They are disabled by default; the default full install collects Xray only. The prototype counts per-session IP-layer bytes, not pure SSH payload, and can miss bytes around authentication or exceed a quota between polls. Do not rely on it for production billing or hard data caps. Test only on a fresh, disposable VPS.

## Contents

- [Requirements](#requirements)
- [Quick install (fresh VPS)](#quick-install-fresh-vps)
- [Update a managed install](#update-a-managed-install)
- [Get the source](#get-the-source)
- [Pre-install checks](#pre-install-checks)
- [Install](#install)
- [Create accounts](#create-accounts)
- [Monitor and manage](#monitor-and-manage)
- [Experimental SSH meter and shared quotas](#experimental-ssh-meter-and-shared-quotas)
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

The script asks for the hostname and TLS choice, installs prerequisites, downloads the project release source, and calls the existing checked full installer. The default Let's Encrypt path needs DNS already pointed at the VPS and TCP 80 reachable. You can instead supply existing certificate files or explicitly select a self-signed certificate for testing only. It pauses for a typed `YES` before changing the server. Do not run it on an existing NevermoreSSH or Nginx Bandit VPS.

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

Fresh full installation is for a clean VPS only. Do not run it on the previously inspected NevermoreSSH or Nginx Bandit servers. Recognized ISPbandits installs can use the code-only upgrade path below; it preserves Xray, TLS, and service topology.

### Update a managed install

From a terminal on a server already marked as a managed ISPbandits install, fetch the guided updater and review it before running:

```bash
curl -fsSL https://raw.githubusercontent.com/dssess/ispbandits-dev./main/quick-install.sh -o quick-install.sh
less quick-install.sh
sudo bash quick-install.sh --upgrade
```

The updater requires typing `UPGRADE`, makes a protected SQLite backup before schema migration, pauses managed collector/expiry timers, preserves the installed deployment mode/domain/meter setting, and restores the prior package and database if the upgrade fails. It does not update Xray, TLS certificates, or service topology. Do not use it to migrate a NevermoreSSH or Nginx Bandit installation.

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

An interactive quick install opens this cyan-framed server dashboard when installation completes. It shows host/OS/uptime, account and measured-traffic summaries, service-state badges, and numbered options for account creation, usage, approximate online SSH process visibility, expiry processing, backups/restores, verification, quota updates, and account lifecycle actions. Administrative commands require root; the menu tells you when `sudo` is needed.

Account creation takes a username and required expiry date (`YYYY-MM-DD`). The default protocol set is OpenSSH, VLESS, and Trojan:

```bash
sudo ispbandits account-create alice --expires 2027-01-01
```

The command creates a no-home Linux SSH account and distinct VLESS/Trojan credentials. Before creating an SSH user, it checks the active OpenSSH service, effective password authentication, and TCP-forwarding settings; it does not silently change the VPS administrator's SSH configuration. If those prerequisites are disabled, correct them in your server policy before retrying. The output reports the effective sshd port(s), the VLESS/Trojan ports, and import links when an installed domain is available. When Xray and/or the experimental meter is active, it seeds the new account's traffic checkpoints before disclosing credentials, so the first observed traffic is not discarded as a baseline. It prints generated passwords/secrets and import links once; save them securely. Never put them in a ticket, public issue, repository, or shell script. When Xray is installed but stopped, the generated config is updated without starting the service.

Choose protocols explicitly when needed. `--limit-gb N` sets a shared lifetime cap in decimal GB (1 GB = 1,000,000,000 bytes); `0` means unlimited. A nonzero cap is accepted only while the Xray service and optional experimental meter timer are active, managed OpenSSH mappings are valid, and the meter has published a fresh valid snapshot. Shared quotas support managed OpenSSH, VLESS, and Trojan identities; Dropbear and VMess are unsupported.

```bash
sudo ispbandits account-create alice --expires 2027-01-01 --limit-gb 50
```

Choose protocols explicitly when needed:

```bash
# Xray protocols only; no Linux SSH account
sudo ispbandits account-create xrayonly --expires 2027-01-01 --protocols vless trojan

# SSH only
sudo ispbandits account-create sshonly --expires 2027-01-01 --protocols openssh
```

Supported `--protocols` values are `openssh`, `vless`, and `trojan`. New Dropbear and VMess provisioning is not supported; legacy protocol rows may be displayed but are not quota-managed.

## Monitor and manage

```bash
# Show account-level usage totals, measured traffic and quota state.
sudo ispbandits usage
sudo ispbandits account-list
sudo ispbandits account-show alice     # never prints stored credentials

# Renew expiry; an EXPIRED account remains locked until explicitly enabled.
sudo ispbandits renew alice --expires 2027-06-01
sudo ispbandits enable alice
sudo ispbandits disable alice
sudo ispbandits ssh-password-reset alice  # new password is shown once

# Permanent deletion requires an explicit confirmation flag.
sudo ispbandits account-delete alice --yes

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

On a full installation, expired accounts are marked expired, Linux SSH passwords are locked and their processes terminated, and expired Xray identities are removed from the generated configuration. Daily expiry reconciliation retries revocation for all non-active accounts. Disable removes all linked access while preserving account and traffic history. Renewing does not silently clear a disabled/expired/quota state; use `enable` only after expiry and quota checks pass. Permanent deletion removes traffic/history after deactivating service access, so make a backup first. If Xray config reconciliation fails, the service is stopped fail-closed. The default timer collects Xray traffic only; SSH bytes remain unmeasured unless the separate experimental mode is explicitly installed.

The default Xray traffic collector runs every minute in Xray-only mode. To run it manually:

```bash
sudo ispbandits collect --xray-only
```

## Experimental SSH meter and shared quotas

The source includes an opt-in OpenSSH meter. On a **fresh disposable full-install test VPS only**, add `--experimental-ssh-meter` to the normal `install.sh --full` command after reviewing [the metering contract](docs/ssh-meter-contract.md). This requires OpenSSH, nftables, and systemd. It replaces the default Xray-only collector timer with an experimental one-second SSH counter producer and five-second full collector/quota timer. The default installer does not install or enable these units.

The producer correlates authenticated OpenSSH worker UIDs with their socket inodes and counts both directions of each established session using a dedicated nftables counter table. It counts IP-layer bytes, including headers and retransmissions, not pure SSH payload. It fails closed when it sees enabled Dropbear identities or an active managed OpenSSH identity that cannot be mapped. It does not edit firewall verdicts, but it has not yet passed live validation on Ubuntu 22/24 or Debian; an unexpected kernel/OpenSSH layout may prevent attribution.

Quota enforcement sums measured lifetime OpenSSH and Xray traffic across upload and download directions. Imported legacy usage is excluded. When measured traffic reaches the cap, the account is marked `QUOTA_EXCEEDED`, its managed Linux SSH account is locked, and Xray config is rebuilt without that account. A failed Xray reconciliation stops Xray. Use `quota-set` to raise the cap above current usage or set it to zero to reactivate an unexpired account:

```bash
sudo ispbandits quota-set alice --limit-gb 100
sudo ispbandits quota-set alice --limit-gb 0   # unlimited
```

This is not a hard real-time cutoff: the configured polling interval, timer delays, concurrent sessions and in-flight bytes can cause overshoot. Do not use it for billing or production enforcement until you have validated known-byte transfers, IPv4/IPv6, concurrent sessions, restarts, rule flushes, collector failures and quota transitions on each target OS. Automated tests are not a substitute for those live tests.

## Back up and restore

Create a protected SQLite backup archive:

```bash
sudo ispbandits backup --dir /var/backups/ispbandits-dev
```

Restore from a previously created archive (the current database is backed up first when it exists):

```bash
sudo ispbandits restore /var/backups/ispbandits-dev/ispbandits-YYYYMMDDTHHMMSS.ffffffZ.tar.gz \
  --dir /var/backups/ispbandits-dev
```

Keep backup files private: they contain account records and protocol credentials. The archive includes a checksum manifest and is written with restrictive permissions. Restore pauses managed collector/expiry/Xray units, preserves a rollback database, reconciles Xray, and resumes prior timers. It is **not** a complete system snapshot of service config, certificates, or OS accounts, and it cannot reconstruct Linux users removed outside ISPbandits.

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

- The OpenSSH per-user meter and shared SSH+Xray lifetime quota enforcement are experimental, opt-in, and not live-validated; they are not production-ready or a hard real-time data cap.
- New managed accounts support OpenSSH, VLESS, and Trojan. Dropbear/VMess are not provisioned, and legacy linked identities are not eligible for shared quota enforcement.
- The online SSH view reports approximate process observations, not unique users/devices or guaranteed sessions.
- Full deployment, TLS renewal, expiry, SSH password policy, and client connectivity still need testing on disposable Ubuntu 22/24 and Debian 12/13 VPSes.
- Code-only managed upgrades preserve service topology; they do not upgrade Xray binaries, TLS certificates, nftables rules, or the protocol/service design.
- Restore covers SQLite and reconciles ISPbandits-owned services only; it is not a full host backup and must not be used to migrate from NevermoreSSH or Nginx Bandit.

Report security concerns privately to the project owner; do not post real credentials, private keys, or customer data in public issues.

## Branding and copyright

© 2026 ISPbandits Dev. All rights reserved. Proprietary software. Unauthorized copying, redistribution, or resale is prohibited.
