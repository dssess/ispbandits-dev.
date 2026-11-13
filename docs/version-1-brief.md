# ISPbandits Dev v1.0.0-alpha.1 — Project Brief

## Product identity and deployment target

**Publisher and product:** ISPbandits Dev. The project combines selected workflows from Nginx Bandit and NevermoreSSH into one fresh-install, single-VPS management platform. The source repository is public by the owner's request; the software remains proprietary under `LICENSE.proprietary`. Public visibility is not a grant of reuse rights.

The intended target is a new, clean Ubuntu 22.04/24.04 or Debian 12/13 VPS. This alpha must not be installed over the user's existing NevermoreSSH or Nginx Bandit systems. The full installer supports x86_64 and arm64, provisions Xray VLESS over TLS on TCP 443 and Trojan over TLS on TCP 8443, and leaves the existing administrator SSH daemon/configuration unchanged.

## Implemented v1 scope

- Unified SQLite account records for managed OpenSSH, VLESS, and Trojan identities, with legacy traffic preserved separately from measured counters.
- Safe OpenSSH account creation with a generated password, no home directory, and a tunnel-only `/bin/false` shell. Account creation verifies effective sshd password authentication and TCP forwarding and reports the configured SSH port(s); it does not silently alter server-wide SSH policy.
- Distinct VLESS UUIDs and Trojan credentials, protected Xray configuration generation, service-aware credential reconciliation, and endpoint/import-link output when a managed domain exists.
- Account inspection, expiry renewal, disable/enable, explicit permanent deletion, SSH password reset, usage reports, expiry processing, backups/restores, verification, and a cyan-framed numbered admin menu.
- Xray StatsService traffic collection by unique per-user identity. The default full-install collector is Xray-only.
- An opt-in experimental OpenSSH per-session nftables byte counter and shared lifetime SSH+Xray quota workflow. It counts IP-layer bytes, not pure payload, is not live-validated on the supported distributions, and can overshoot between polls. It is disabled by default and is not suitable for billing or hard real-time caps.
- A recognized-install, code-only upgrade path that backs up SQLite, pauses managed writer timers, preserves deployment/TLS/service topology, and rolls back the code/database if migration or installation fails.

New Dropbear and VMess provisioning is not part of this alpha. Legacy linked identities may be visible in stored data but are not covered by the shared quota prototype.

## Ownership notice

The installer, Python source, menu, and login/MOTD include:

> © 2026 ISPbandits Dev. All rights reserved. Proprietary software.

This communicates ownership but is not a technical anti-copying control. The full license terms are in `LICENSE.proprietary`.

## Release and production acceptance gates

The local unit suite and GitHub Actions matrix cover Python 3.10–3.13, database migrations, account lifecycle transitions, backup/restore, config generation, counter parsing, and CLI/menu paths. They do not replace live deployment tests.

Before calling the system production-ready, use a disposable VPS for each target OS and validate: clean installation and rollback; DNS, TLS issuance/renewal, and firewall reachability; SSH password login and tunnel-only forwarding on the detected sshd port; real VLESS/Trojan client connections; known-byte IPv4/IPv6 upload/download attribution; concurrent sessions; quota transition and overshoot; collector/meter restart and state loss; expiry/disable/enable/renew/delete; and backup restore while the managed services are active. Publish test results and record the exact OS, kernel, OpenSSH, and Xray versions.

## Earlier design artifacts

- Architecture visual: `/home/ubuntu/nevermore-vpn-architecture.png`
- Editable architecture source: `/home/ubuntu/nevermore-vpn-architecture.html`
- Historical traffic collector prototype: `/home/ubuntu/traffic_collector.py`
