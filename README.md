# Neverbandits — Unified VPS Manager

**Version:** 1.0.0-alpha.2
**Publisher:** ISPbandits Dev
**License:** Proprietary (`LICENSE.proprietary`)

<p align="center">
  <img src="assets/ispbandits-breathing-banner.gif" alt="A cyan and amber network loop softly breathing against a deep navy background" width="100%" />
</p>

<p align="center"><strong>One control plane. A steady pulse.</strong><br />
SSH WebSocket · VLESS · Trojan · SQLite · Unified traffic records</p>

Neverbandits combines Nevermore-style account creation and connection sheets with ISPbandits Dev's unified SQLite account lifecycle, Xray VLESS/Trojan identities, usage collection, backups, expiry processing, and optional experimental shared-quota enforcement.

> **Alpha warning:** The WebSocket bridge and Nginx routes are implemented and are being tested in an isolated sandbox; the full installer has not yet been exercised on each supported VPS distribution or a live mobile client. Use a **fresh, disposable VPS** for the first deployment. The optional OpenSSH traffic meter and shared lifetime quota remain experimental and are not validated for production billing or hard caps.

## What runs on the VPS

| Client protocol | Public entry | Backend | Notes |
|---|---|---|---|
| SSH over WebSocket | TCP 80, path `/sshws` | loopback websockify → existing sshd port | Plain HTTP WebSocket; SSH still verifies/encrypts its own session |
| SSH over TLS WebSocket | TCP 443, path `/sshws` | Nginx TLS → loopback websockify → existing sshd port | WSS; use a trusted certificate and verify the SSH host key |
| VLESS over WebSocket/TLS | TCP 443, path `/vless` | Nginx TLS → Xray at `127.0.0.1:10001` | Shares 443 with SSH-WSS by Nginx path routing |
| Trojan over TLS/TCP | TCP 8443 | Xray | Uses the configured TLS certificate directly |
| Xray StatsService | loopback `127.0.0.1:10085` | Xray | Never exposed on a public interface |

Nginx owns public ports 80/443. websockify listens only on `127.0.0.1:6080` and connects to the effective port reported by `sshd -T`. Xray VLESS listens only on loopback. Neverbandits does **not** rewrite SSH configuration, install a replacement SSH daemon, modify firewall rules, or change cloud firewall settings.

See [the SSH-WebSocket architecture and safety notes](docs/sshws-architecture.md) for service details and external references.

## Requirements and pre-install checks

Use a fresh Ubuntu 22.04/24.04 or Debian 12/13 VPS with systemd, root access, and internet connectivity. The host must already have a working OpenSSH server/socket. Password authentication and TCP forwarding must be allowed by that account's effective sshd policy; Neverbandits will refuse SSH account creation rather than silently weakening that policy.

1. Point the chosen hostname's DNS A/AAAA records at this VPS. The hostname must resolve to the server and match the TLS certificate. The installer enables IPv6 Nginx listeners only when the VPS kernel supports IPv6; do not publish an AAAA record unless that IPv6 address is reachable. No special nameserver delegation is needed beyond your normal DNS setup.
2. Keep TCP **80, 443, and 8443** free for the Neverbandits frontend/protocols. Xray loopback ports **10001** and **10085** must also be free. The installer detects conflicts before deployment.
3. Allow inbound TCP **80, 443, and 8443** in the VPS/provider firewall. The installer does not change those rules. Keep your existing SSH admin port reachable; Neverbandits does not change it.
4. Use a Let's Encrypt certificate (recommended) or provide an existing certificate and matching key. A self-signed certificate is for testing only and must be explicitly trusted by clients.

Do not run a full install on an existing NevermoreSSH/Nginx Bandit VPS or a host with an active Nginx site. The installer refuses occupied ports, active Nginx, non-default enabled Nginx sites, and extra `conf.d` files instead of replacing them.

## Quick install (fresh VPS)

Review the bootstrapper before running it on the new VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/dssess/ispbandits-dev./main/quick-install.sh -o quick-install.sh
less quick-install.sh
sudo bash quick-install.sh
```

The guided installer asks for the hostname and TLS choice. Let's Encrypt requires DNS already pointed at the VPS and port 80 reachable. For its standalone HTTP-01 renewal method, the installed Certbot hooks briefly stop Nginx for renewal and start it again afterward. You can instead provide certificate files or explicitly choose self-signed testing TLS. The script pauses for a typed `YES` before changing the fresh VPS.

The full installer installs distro-packaged Nginx/websockify, Xray, Neverbandits-owned systemd units, SQLite/CLI files, and the network configuration. It does not open ports in the provider firewall. When installation completes, open the cyan-framed menu with:

```bash
sudo neverbandits
# Legacy compatibility alias:
sudo ispbandits
```

## Manual full install

From a checked-out source tree, supply a matching TLS certificate/key pair:

```bash
sudo ./install.sh --full \
  --domain vpn.example.com \
  --cert-file /etc/letsencrypt/live/vpn.example.com/fullchain.pem \
  --key-file /etc/letsencrypt/live/vpn.example.com/privkey.pem
```

For Certbot certificates obtained using the standalone HTTP-01 method, add `--certbot-standalone` so renewal hooks stop/start Nginx and reload the updated certificate. Use `--self-signed` only for testing. `./install.sh --check` runs syntax and unit tests without installing system files.

The earlier `v1.0.0-alpha.1` full deployment has a different network topology. This alpha intentionally refuses a code-only upgrade from it; test alpha-2 on a fresh VPS rather than migrating a live server. Code-only upgrades are only accepted from a recognized alpha-2 install and do not alter service topology.

## Create accounts and get connection details

The numbered dashboard's **[01] CREATE SSH WEBSOCKET** flow asks for a username, expiry date, selected protocols, and (for SSH) a password entered twice without echo. The password is set through `chpasswd` on stdin. It is not stored in SQLite or in the Nginx configuration. After provisioning, the command prints the connection sheet to the local terminal once; Neverbandits does not publish password-containing account files over unauthenticated HTTP.

Protocol account creation is enabled only on a recognized full alpha-2 deployment. Before creating an account, Neverbandits checks the relevant live services (and the loopback sshd target); it rolls back the new account if a required listener fails before the connection sheet is disclosed. Foundation-only installs are for development/CI and cannot provision network accounts. VLESS UUIDs and Trojan passwords appear only inside their one-time import links.

Example CLI flow:

```bash
sudo neverbandits account-create alice \
  --expires 2027-01-01 \
  --protocols openssh vless trojan \
  --prompt-password
```

The generated connection sheet includes:

- Hostname and current DNS-resolved IP address(es).
- Direct SSH host and the effective sshd port(s), without changing admin SSH settings.
- SSH WebSocket endpoint `ws://HOST:80/sshws`.
- SSH TLS-WebSocket endpoint `wss://HOST:443/sshws`, with TLS/SNI hostname.
- A WebSocket HTTP-upgrade payload template for compatible clients.
- VLESS WSS import link on port 443/path `/vless` and Trojan TLS import link on port 8443, when selected.
- Username, password, and account expiry.

The raw payload template is:

```text
GET /sshws HTTP/1.1[crlf]Host: HOST[crlf]Upgrade: websocket[crlf]Connection: Upgrade[crlf]Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==[crlf]Sec-WebSocket-Version: 13[crlf][crlf]
```

Use a client with an actual SSH-over-WebSocket/WebSocket mode; an ordinary SSH client cannot interpret a WebSocket handshake by itself. Port 80 is not TLS-protected. SSH encrypts and authenticates its own session, so verify the SSH server host key. For WSS, use a trusted certificate where possible; self-signed testing certificates require explicit client trust.

To create only an SSH-WebSocket user, select OpenSSH only in the dashboard or use `--protocols openssh --prompt-password`. To create VLESS/Trojan-only identities, omit `openssh` from `--protocols`. New Dropbear and VMess provisioning are not supported.

## Account lifecycle and monitoring

```bash
sudo neverbandits account-list
sudo neverbandits account-show alice       # never prints stored credentials
sudo neverbandits usage
sudo neverbandits online                  # approximate sshd process counts, not devices
sudo neverbandits renew alice --expires 2027-06-01
sudo neverbandits disable alice
sudo neverbandits enable alice
sudo neverbandits ssh-password-reset alice # displays the replacement once
sudo neverbandits expire
sudo neverbandits verify
```

Account creation provisions a no-home `/bin/false` Linux SSH account and selected Xray identities. Before creating an SSH account it checks effective `PasswordAuthentication`, TCP forwarding, authentication-method, and daemon state; it never edits sshd configuration. Disable/expiry locks the Linux password, terminates managed SSH sessions, and removes the account's Xray identities from active configuration. Renewal does not silently clear disabled, expired, or quota states. Permanent deletion requires `account-delete USER --yes` and removes usage history, so back up first.

The default collector records Xray VLESS/Trojan usage every minute. SSH network-byte metering is a separate opt-in experimental install option. It counts per-session IP-layer bytes (not pure payload), can miss authentication-period traffic, and may overshoot between samples. Do not use it for billing or production data caps until validated on each exact OS/kernel/OpenSSH configuration.

## Shared lifetime quota (experimental)

The experimental quota mode combines measured managed OpenSSH plus VLESS/Trojan traffic in SQLite. It disables all linked protocols when measured usage reaches the cap. A quota is not a real-time cutoff: polling intervals, concurrent sessions, retransmissions, and in-flight traffic can overshoot. It requires a fresh, valid SSH-meter snapshot and an active Xray service.

On a fresh disposable test VPS only, opt in during installation with `--experimental-ssh-meter` and review [the metering contract](docs/ssh-meter-contract.md). Example quota commands:

```bash
sudo neverbandits quota-set alice --limit-gb 50
sudo neverbandits quota-set alice --limit-gb 0  # unlimited
```

## Back up and restore

```bash
sudo neverbandits backup --dir /var/backups/ispbandits-dev
sudo neverbandits restore /var/backups/ispbandits-dev/ispbandits-YYYYMMDDTHHMMSS.ffffffZ.tar.gz \
  --dir /var/backups/ispbandits-dev
```

Backups contain account records and protocol secrets; keep them private. Restore validates the archive and checksum, preserves a rollback backup, quiesces managed services, and reconciles Xray. It is not a full VPS image and does not restore certificates, Nginx/service files, or Linux accounts removed outside Neverbandits.

## Development and release checks

The Python application requires Python 3.10+ with SQLite. Local tests do not require third-party Python runtime packages:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n install.sh quick-install.sh scripts/refresh-tls.sh scripts/certbot-stop-nginx.sh scripts/certbot-start-nginx.sh scripts/certbot-refresh-tls.sh
./install.sh --check
```

The remaining release gate is a full installer run on fresh Ubuntu 22.04/24.04 and Debian 12/13 VPS images, a real SSH login forwarded through the bridge, compatibility checks in supported Android clients, and validation with a trusted public certificate. Sandbox tests are not a substitute for those fresh-VPS tests.

The sandbox has now passed the optional local integration runner below: it starts Nginx and websockify on dynamically allocated high ports, validates an HTTP and a self-signed WSS WebSocket upgrade, and verifies binary bytes reach a temporary TCP echo service and return intact. It does not touch system Nginx configuration or use the host's ports 80/443. Run the same check on a development machine with Nginx, `/usr/bin/websockify`, and OpenSSL installed:

```bash
python3 scripts/test-sshws-integration.py
```

Passing that test confirms the proxy/bridge protocol path only. It does **not** prove login against sshd, real Android app payload compatibility, certificate trust on clients, or a successful alpha-2 install on every supported operating system.

## Branding and copyright

**Neverbandits** is the product name. **ISPbandits Dev** is the publisher and copyright owner.

© 2026 ISPbandits Dev. All rights reserved. Proprietary software. Unauthorized copying, redistribution, or resale is prohibited. Public repository visibility does not grant a right to reuse or redistribute this code; see `LICENSE.proprietary`.
