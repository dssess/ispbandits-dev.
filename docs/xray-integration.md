# Neverbandits protocol integration (alpha-2)

Neverbandits creates distinct VLESS UUIDs and Trojan passwords in the protected SQLite database, then generates a root-protected Xray configuration. VLESS uses WebSocket path `/vless` on Xray loopback port 10001; Nginx terminates TLS and routes it on public TCP 443. Trojan remains Xray TLS/TCP on 8443. Xray StatsService stays on `127.0.0.1:10085`. The generated configuration contains protocol credentials and must remain root-owned and readable only by the Xray service group.

SSH-over-WebSocket is a separate path: public Nginx `/sshws` routes to a loopback-only websockify service, which forwards to the existing sshd port discovered from `sshd -T`. Plain WebSocket is available on port 80 and TLS WebSocket on port 443. The installer does not change OpenSSH settings, install another SSH daemon, or adjust the firewall. Full deployment requires a fresh host, active existing sshd, free public ports, and matching DNS/TLS configuration. See [the SSHWS architecture note](sshws-architecture.md).

## Commands

```text
neverbandits account-create USER --expires YYYY-MM-DD [--protocols openssh vless trojan] [--prompt-password]
neverbandits xray-provision USER vless|trojan [--rotate]
sudo neverbandits xray-config --cert-file CERT --key-file KEY [--check-only]
sudo neverbandits online
sudo neverbandits renew USER --expires YYYY-MM-DD
sudo neverbandits disable USER
sudo neverbandits enable USER
sudo neverbandits account-delete USER --yes
```

`account-create` uses a no-home `/bin/false` Linux account for SSH. For an SSH identity, the dashboard prompts for a password twice without echo; the CLI supports `--prompt-password`. Before creation, Neverbandits checks the existing sshd's effective password-authentication and TCP-forwarding policy. It never alters the SSH daemon or administrator account. Secrets are sent to account tools over stdin, not command-line arguments, and are not stored in the database except Xray credentials, which remain protected for configuration reconciliation. New credentials and connection links are displayed once in the operator's terminal.

`xray-provision` and credential rotation require the alpha-2 full-install marker, an active Xray service, and active Nginx for VLESS. If activation or a post-activation health check fails, the previous credential state is restored where possible. The one-time credential is contained only in the corresponding import link.

`xray-config` validates through the installed Xray executable before writing a 0640 configuration. `--apply` is intended only after its managed systemd service is installed; it atomically updates configuration and restores the old file if restart fails. Expiry reconciliation stops Xray if its regenerated configuration cannot be activated.

## Traffic and online status

Xray statistics require a `stats` object, per-level `statsUserUplink` and `statsUserDownlink` policy flags, and unique non-empty user `email` identities. The collector maps each identity to one VLESS or Trojan account. `statsUserOnline` is enabled, but the management CLI does not yet display Xray online counts. SSH online status counts matching sshd processes and is not a unique-device count.

The default collector records Xray traffic only. An opt-in experimental OpenSSH producer samples nftables counters and a separate collector can enforce shared lifetime quotas. The meter counts IP-layer bytes, not pure payload, and may overshoot between polls. It has not passed live validation on all supported OS/kernel/OpenSSH combinations; do not use it for billing or hard caps.

## Validation and known limits

The alpha-2 automated tests validate the generated loopback VLESS WS config, Trojan TLS config, connection-sheet details, and Nginx routing invariants. Sandbox integration testing can validate an actual WebSocket handshake and binary forwarding through Nginx to a disposable TCP echo server. Neither test replaces a full installer run on each target OS or a real Android-client SSH tunnel test.

Xray's [WebSocket transport documentation](https://xtls.github.io/en/config/transports/websocket.html) confirms that WebSocket can be routed by Nginx and describes its path setting. It also recommends evaluating XHTTP for newer deployments because WebSocket can have identifiable traffic characteristics. This alpha uses WS for compatibility and shared-port routing; future protocol work should consider XHTTP without changing the SSHWS service.

The upstream [websockify project](https://github.com/novnc/websockify) describes its WebSocket-to-TCP bridge. Official archives list websockify in [Ubuntu 22.04](https://packages.ubuntu.com/jammy/websockify), [Ubuntu 24.04](https://packages.ubuntu.com/noble/websockify), [Debian 12](https://packages.debian.org/bookworm/websockify), and [Debian 13](https://packages.debian.org/trixie/websockify). Ubuntu hosts may need the Universe repository enabled by their administrator.

## Official references

- [Xray installation](https://xtls.github.io/en/document/install.html)
- [Xray API](https://xtls.github.io/en/config/api.html)
- [Xray policy](https://xtls.github.io/en/config/policy.html)
- [Xray traffic statistics](https://xtls.github.io/en/config/stats.html)
- [Xray VLESS inbound](https://xtls.github.io/en/config/inbounds/vless.html)
- [Xray Trojan inbound](https://xtls.github.io/en/config/inbounds/trojan.html)
- [Xray WebSocket transport](https://xtls.github.io/en/config/transports/websocket.html)
- [Xray CLI](https://xtls.github.io/en/document/command.html)
