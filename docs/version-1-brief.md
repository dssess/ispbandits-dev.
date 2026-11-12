# ISPbandits Dev — Version 1 Project Brief

## Identity

**Company / publisher:** ISPbandits Dev. This is the owner/publisher brand, not yet the final public product name. The combined system’s product name remains undecided; NevermoreSSH and Nginx Bandit are the existing systems whose useful functionality informs Version 1.

## Version 1 goal

Build a fresh-install, single-VPS management system combining Nginx Bandit-style SSH user management and usage display with Xray protocols, while preserving a unified account record and a recoverable SQLite database. Do not modify the two existing VPS installations as the Version 1 deployment target; deployment is for a new, clean VPS.

## Agreed initial scope

- SSH and Dropbear customer accounts, keeping server-administration SSH separate.
- Xray Trojan and VLESS; VMess may be included as an optional protocol.
- One account-management flow for creation, renewal, disabling, expiry, connection limits, and protocol mappings.
- An online SSH/Dropbear view inspired by Nginx Bandit SSH panel option 1 → 5. Show active session observations and freshness; do not label process counts as unique devices unless tests validate that mapping.
- SQLite as the first backend, extending the existing Nginx Bandit `users` database compatibly rather than replacing it with a new account store.
- Nginx Bandit-style backup/restore, with the archive format, encryption behavior, permissions, and restore path made consistent and tested.
- Per-user upload/download/total usage reporting. Xray usage comes from its StatsService. SSH usage must use a verified network-byte metering source; `/proc/<pid>/io` is not an acceptable substitute for network traffic.
- Automatic quota enforcement remains off until counter accuracy is validated with known-byte transfers.
- Target Ubuntu 22.04, Ubuntu 24.04, and Debian. Advertise support only after clean-install and protocol tests pass on each target.

## Copyright and anti-copying notices

Use a consistent notice in the installer, source headers, admin menu, and server login/MOTD. Proposed starting notice:

> © 2026 ISPbandits Dev. All rights reserved. Proprietary software. Unauthorized copying, redistribution, or resale is prohibited.

The notice is a deterrent and ownership notice, not a technical barrier to copying. Protect source distribution with private repository access and a clear proprietary license/terms. Final legal terms and the display-name wording should be reviewed before public release.

## Release gates

1. Confirm the working repository and final product name.
2. Implement the backwards-compatible SQLite changes and verified SSH metering source.
3. Implement SSH and Xray account provisioning/reconciliation.
4. Implement online-session monitoring, the admin usage menu, and safe backup/restore. Keep “online” distinct from accurate network-byte accounting.
5. Test install, authentication, known-byte upload/downloads, restarts, backup/restore, port conflicts, and rollback on clean Ubuntu 22.04, Ubuntu 24.04, and Debian VMs.
6. Deploy only to the new clean VPS after a successful test/review.

## Existing design artifacts

- Architecture visual: `/home/ubuntu/nevermore-vpn-architecture.png`
- Editable architecture source: `/home/ubuntu/nevermore-vpn-architecture.html`
- SQLite usage collector aggregation code: `/home/ubuntu/traffic_collector.py`

The collector requires an independent SSH network-accounting producer; it does not pretend process I/O is network usage.
