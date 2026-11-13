# Neverbandits SSH-over-WebSocket architecture

## Data path

- TCP 80 and 443 terminate at the managed Nginx virtual host. `/sshws` is proxied to a loopback-only websockify listener at `127.0.0.1:6080`; Nginx terminates TLS for WSS on 443.
- websockify forwards the WebSocket byte stream to `127.0.0.1:<effective-sshd-port>`. It runs as the unprivileged `ispbd-ws` service account and is never bound to a public interface.
- VLESS uses Xray WebSocket transport on loopback port 10001 and path `/vless`; Nginx terminates TLS and forwards that path on 443. Trojan remains Xray TLS/TCP on 8443. Xray StatsService remains loopback-only on 10085.
- Existing sshd configuration, administrator credentials, firewall rules, and cloud firewall settings are not changed. Full deployment requires a fresh VPS, free listener ports, and a functioning existing OpenSSH server. Provider firewall rules must allow TCP 80, 443, and 8443.

## Account connection sheet

The numbered menu asks for a username, expiry date, and password entered twice through a hidden prompt. The selected password is sent to `chpasswd` over stdin, never included in a command-line argument, SQLite record, Nginx configuration, or log. The creation response displays it once along with the direct SSH ports, `ws://HOST:80/sshws`, `wss://HOST:443/sshws`, a WebSocket HTTP upgrade payload, and protocol import links.

The HTTP (port 80) WebSocket path is not TLS-protected. SSH still provides its own authenticated encryption, so clients must verify the SSH host key. For WSS, use a publicly trusted certificate when possible. A self-signed certificate is only for testing and must be explicitly trusted by the client.

## Safety rules

The full installer is for a clean host only. It refuses occupied ports, a running Nginx listener, pre-existing managed Neverbandits files, and non-default enabled Nginx sites or extra `conf.d` configuration rather than replacing them. It adds its own Nginx site and service unit; it does not rewrite `/etc/ssh/sshd_config`, install a different SSH daemon, or open firewall ports. Code-only upgrade is not a migration path from the earlier 1.0.0-alpha.1 topology; use a fresh VPS for this alpha.

The creation sheet is printed in the local terminal only. Neverbandits does not reproduce Nevermore's public HTTP credential-sheet behavior because exposing passwords through an unauthenticated web path would disclose account credentials.

## References

- [websockify upstream project](https://github.com/novnc/websockify) describes its WebSocket-to-TCP proxy and encrypted WebSocket support. The managed service uses the distro executable with explicit `127.0.0.1:6080 127.0.0.1:<sshd-port>` source/target addresses.
- The [Ubuntu 22.04 Jammy package](https://packages.ubuntu.com/jammy/websockify), [Ubuntu 24.04 Noble package](https://packages.ubuntu.com/noble/websockify), [Debian 12 Bookworm package](https://packages.debian.org/bookworm/websockify), and [Debian 13 Trixie package](https://packages.debian.org/trixie/websockify) are present in the respective official archives. Ubuntu publishes websockify in Universe; a host with that component disabled may need its administrator to enable the repository before installation.
- [Xray WebSocket transport documentation](https://xtls.github.io/en/config/transports/websocket.html) states that WebSocket transport can be routed by HTTP servers such as Nginx and documents the `wsSettings.path` field used for `/vless`.
