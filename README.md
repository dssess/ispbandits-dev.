# ISPbandits Dev — Unified VPS Manager

**Version:** 0.1.0-alpha · **Owner:** ISPbandits Dev · **License:** proprietary

Version 1 is being built as a clean, GitHub-ready project for a fresh single VPS. The intended system combines SSH/Dropbear account management with Xray Trojan and VLESS (VMess optional), per-account usage, online SSH visibility, and consistent backup/restore. The code includes the compatible SQLite schema, database backup/restore, an on-demand approximate SSH/Dropbear process view, coordinated system-account and Xray-credential creation, automatic expiry reconciliation, and a TLS VLESS/Trojan config generator. The full installer now has an explicit Xray deployment path. The generated config passed local validation with the official Xray binary, but no live protocol connection or target VPS has been validated, and SSH per-user byte metering is still missing.

## Safe local development

Requires Python 3.10+ and SQLite support in Python. No third-party runtime dependencies are required.

```bash
python3 -m venv .venv
. .venv/bin/activate
PYTHONPATH=src python -m ispbandits --db ./dev.db init
PYTHONPATH=src python -m ispbandits --db ./dev.db user-add demo --expires 2027-01-01
PYTHONPATH=src python -m ispbandits --db ./dev.db link-protocol demo vless demo-vless@example.invalid
PYTHONPATH=src python -m ispbandits --db ./dev.db usage
PYTHONPATH=src python -m ispbandits --db ./dev.db backup --dir ./backups
PYTHONPATH=src python -m ispbandits --db ./dev.db verify
```

The legacy `link-protocol` command stores an external identifier only. `account-create` generates and stores VLESS/Trojan credentials in the root-only SQLite database and prints them once; protect the database and any generated Xray config as secrets. Never commit a real VPS database, backup archive, credentials, keys, or `.env` file.

Run tests with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Installer

The checked-in `install.sh` accepts Ubuntu 22.04/24.04 and Debian 12/13. `--foundation-only` installs the local SQLite/CLI alpha without services. `--full` additionally installs the pinned Xray 26.10.31 release (SHA-256 checked for amd64/arm64), creates a dedicated unprivileged service, deploys VLESS TCP 443 and Trojan TCP 8443, enables a loopback-only StatsService API, sets up TLS refresh, daily expiry, and once-per-minute **Xray-only usage** timers, and adds the copyright MOTD notice. The Xray binary is fetched only in `--full` mode from the official XTLS/Xray-core release URL; the digest is pinned. No cloud firewall or admin SSH settings are changed. VMess and per-user SSH byte metering are not included in this alpha.

For a clean disposable VPS with DNS already pointing at it and a valid certificate/key pair, review the installer and run `sudo ./install.sh --full --domain vpn.example.com --cert-file /path/fullchain.pem --key-file /path/privkey.pem`. The explicit `--self-signed` option is for connectivity tests only; it does not provide production server authentication. Use `sudo ./install.sh --foundation-only` for development-only deployment and `./install.sh --check` for source checks without system changes. `--upgrade` only supports the foundation installation; it deliberately refuses a live full-proxy upgrade until service-aware rollback is implemented. Unknown databases and existing CLI/config paths are refused rather than overwritten. Do not use the existing production VPSes as test hosts.

The system database is root-only (`0700` directory, `0600` file); on an installed VPS use `sudo ispbandits ...` for administrative commands until a dedicated least-privilege operator group is designed.

## Data compatibility and safety

- Existing Nginx Bandit `users` fields are preserved. Old `data_usage` is imported once into `traffic_totals` as `legacy_bytes`, because its direction/source may not be reliable network traffic.
- New per-protocol totals split upload and download. Xray collection uses Xray StatsService but still needs live-target validation. SSH accounting must use a validated network-byte source; `/proc/<pid>/io` is not a network meter.
- Account creation, Xray config generation, service deployment, and scheduled expiry reconciliation are present. Disable/delete/renew workflows beyond automatic expiry are not complete; quota enforcement is disabled.
- Backup uses SQLite's online backup API, a private `0600` tar.gz archive with checksum manifest, atomic publication, archive validation, integrity checks, and a pre-restore rollback backup. This fixes the previous archive-extension/restore-menu mismatch.

## Roadmap to Version 1

1. Run the full installer on a disposable target and test client connectivity, TLS renewal, expiry, and rollback.
2. Add safe disable/delete/renew operations and reconcile each one with Xray and SSH services.
3. Implement a trustworthy SSH/Dropbear network-byte meter and test known transfers through reconnects and restarts.
4. Validate Xray StatsService deltas on a live target and ensure SSH/Xray totals stay consistent.
5. Harden service-aware backup/restore for generated config, certificate state, SQLite, and OS accounts.
6. Verify clean installs on Ubuntu 22.04, Ubuntu 24.04, Debian 12, and Debian 13 before declaring them supported; leave quota enforcement disabled until metering passes.

## Branding and copyright

The project publisher is **ISPbandits Dev**. The combined product's public name is not yet finalized. A copyright/proprietary notice is included in source and installation materials. Copyright notices discourage casual theft but do not technically prevent copying; repository access controls and final legal terms matter.

© 2026 ISPbandits Dev. All rights reserved. Proprietary software.
