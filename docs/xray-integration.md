# Xray integration notes (v1 work in progress)

The project now creates distinct VLESS UUIDs and Trojan passwords in the root-only SQLite database and can generate a protected Xray configuration. The full fresh-install path can install the official pinned Xray release and a hardened systemd service. The generated config uses two TCP+TLS inbounds (VLESS on 443 and Trojan on 8443 by default), a loopback-only API on 127.0.0.1:10085, `HandlerService` and `StatsService`, and per-user upload/download statistics keyed by unique email identities. The Xray config contains client credentials and must remain root-owned and readable only by the Xray service group.

A valid certificate chain and its matching private key are required. The certificate must cover the hostname clients use. Do not use TLS `allowInsecure` as a production substitute for a trusted certificate. The full installer checks the domain, certificate, and key, copies credentials into a protected Xray-readable location, and enables a 30-minute refresh timer so renewals are picked up. The installer also enables a daily expiry timer; it locks due SSH accounts and removes due Xray identities from the generated config.

## Commands added

```text
ispbandits account-create USER --expires YYYY-MM-DD [--protocols openssh vless trojan]
ispbandits xray-provision USER vless|trojan [--rotate]
sudo ispbandits xray-config --cert-file CERT --key-file KEY [--check-only]
sudo ispbandits online
sudo ispbandits renew USER --expires YYYY-MM-DD
sudo ispbandits disable USER
sudo ispbandits enable USER
sudo ispbandits account-delete USER --yes
```

`account-create` uses the Nginx Bandit-compatible no-home `/bin/false` system account pattern for SSH and prints the generated SSH password once. Before provisioning it checks the existing sshd's effective password-authentication and TCP-forwarding policy; it does not alter SSH daemon configuration or the administrator account. VLESS/Trojan secrets are stored in the root-only DB and printed once. `xray-config` validates through an installed Xray executable before writing a 0640 configuration. `--apply` is intended only after the managed systemd service has been installed; it attempts an atomic config update and restores the old file if restart fails. Expiry reconciliation fails closed by stopping Xray if its regenerated config cannot be activated.

SSH online view is best-effort: it counts matching SSH/Dropbear daemon processes owned by managed local accounts. It is explicitly not a unique-device count or a protocol-level session guarantee; validate it on each target's OpenSSH/Dropbear build before relying on it operationally.

## Traffic and online status

Xray user statistics require a `stats` object, the per-level `statsUserUplink` and `statsUserDownlink` policy flags, and unique non-empty user `email` identities. The Xray collector maps those identities to exactly one VLESS or Trojan account. `statsUserOnline` is enabled in the generated policy, but the management CLI does not yet display Xray online counts. Xray's online map is based on activity in the preceding 20 seconds and may not count loopback client addresses; use traffic-counter deltas for a robust “active recently” approximation when a local proxy is in front.

The full installer runs the collector once per minute with explicit `--xray-only` mode, so VLESS/Trojan usage can be recorded without pretending SSH is measured. An opt-in prototype OpenSSH producer now writes a root-only nftables-counter snapshot, and a separate full collector can enforce shared lifetime quotas. This implementation is experimental, counts IP-layer bytes, and has no live validation on a target VPS. Do not use it for billing or hard caps until known-byte transfer, reconnect, restart, concurrency, and failure tests pass on each supported target OS.

## Validation performed

The configuration generator was checked against the official Xray 26.10.31 Linux amd64 binary. The downloaded archive SHA-256 was verified against its GitHub release asset metadata (`e4bd9119f50ae7c41658b1abd0679ae06f98bd864a2907ff33f2f2cf7081b214`). A temporary self-signed certificate and disposable DB with one VLESS and one Trojan identity passed `xray run -test -config`; an empty-user first-boot config also passed. The TLS refresh hook passed a disposable smoke test covering cert/key match, atomic symlink publication, service-group key readability, and idempotency. These are local sandbox tests only—not a full installer run on a target OS, production deployment, or client connectivity test.

## Official references

- [Xray installation](https://xtls.github.io/en/document/install.html)
- [Xray API](https://xtls.github.io/en/config/api.html)
- [Xray policy](https://xtls.github.io/en/config/policy.html)
- [Xray traffic statistics](https://xtls.github.io/en/config/stats.html)
- [Xray VLESS inbound](https://xtls.github.io/en/config/inbounds/vless.html)
- [Xray Trojan inbound](https://xtls.github.io/en/config/inbounds/trojan.html)
- [Xray TLS transport](https://xtls.github.io/en/config/transports/tls.html)
- [Xray CLI](https://xtls.github.io/en/document/command.html)
- [Xray `statsUserOnline` discussion and loopback caveat](https://github.com/XTLS/Xray-core/discussions/5769)
