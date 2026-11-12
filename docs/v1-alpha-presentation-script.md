# ISPbandits Dev v1 Alpha — Team Alignment Talk Track

**Audience:** development, operations, and product team<br>
**Estimated time:** 5–7 minutes<br>
**Purpose:** align the team on what the alpha is, what it is not, and what must be true before a fresh-VPS deployment.

---

## 1. Opening — why this project exists

“Thanks, everyone. Today I want to align us on the ISPbandits Dev Version 1 alpha. The goal is to bring together the SSH account-management and per-user usage experience we value from Nginx Bandit with Nevermore’s Xray protocols—especially Trojan and VLESS—on one fresh VPS.

“The central idea is one account-management flow, several separate connection protocols, and one place to review a customer’s account status and measured traffic. We are starting with SQLite on a single server. This is a foundation for a controlled first release, not a claim that the full VPN manager is already complete.”

## 2. Walk through the architecture

“Starting at the top of the diagram, deployment begins with a clean server. Our intended targets are Ubuntu 22.04, Ubuntu 24.04, and Debian, but we will only call an operating system supported after a clean-install test on that OS. The installer must detect the platform, check dependencies and port conflicts, preserve administrator access, and fail safely rather than making unknown changes.”

“Next is the client-access layer. SSH and Dropbear remain their own path; Xray carries Trojan and VLESS, with VMess optional. The server’s administrator SSH access must stay separate from customer accounts. These paths may share the account record, but they should not be coupled in a way that lets a protocol change lock us out of the VPS.”

“We also want the useful online-user screen from Nginx Bandit’s SSH panel, option 1 then 5. Its current implementation reads a monitor-generated report that scans for SSH/Dropbear processes about every 30 seconds. That gives a quick presence view, but the displayed count is a process count, not proven unique devices. In our version we should show online SSH sessions with freshness, and only claim an exact session or device count after testing it.”

“Above those services sits the account-control layer. An administrator creates, renews, or disables one account. That account can map to a Linux SSH identity and separate Xray identities. The database stores the relationship, expiry, status, limits, and usage—not raw credentials in logs or the menu. For Xray, distinct per-protocol stats identities let us attribute counters correctly.”

## 3. Explain the traffic-accounting decision

“The usage path combines counters from two very different sources. Xray has a per-user StatsService, which gives us cumulative uplink and downlink byte counters. We can map those into upload and download after confirming direction with known-byte transfers.

“SSH is the harder part. Stock OpenSSH and Dropbear do not give us a simple, reliable per-account network-byte counter. Nginx Bandit’s older process-I/O approach can be useful as an estimate of process activity, but it is not a network meter. We will not label it as actual network usage. The current collector expects a separate, validated SSH network-meter producer. Until that exists and passes tests, the SSH usage line is not release-ready, and automatic quota enforcement stays disabled.”

## 4. Explain persistence and recovery

“SQLite is the first backend because Version 1 is one VPS and a local database keeps deployment straightforward. We preserve the existing Nginx Bandit users table and import older usage into a legacy bucket when its direction or measurement source is unknown. New counters are stored by account, protocol, and direction. A transaction updates the counters and checkpoints together, so a retry does not add the same sample twice.

“Backups use SQLite’s online backup mechanism, a private archive with a checksum, integrity validation on restore, and a rollback copy of the current database. We are making the archive and restore formats consistent. If we later move to multiple VPS nodes or need concurrent writers, we can evaluate a central service or a MySQL-compatible backend. That is not required for this single-server alpha.”

## 5. Be explicit about the alpha’s current state

“What exists today is more than the schema: we have transactional SQLite migrations and backup/restore, managed Linux SSH-account creation, VLESS/Trojan credentials and config generation, an approximate SSH process view, expiry reconciliation, a full fresh-install mode for Xray, protected TLS refresh, and scheduled Xray-only traffic collection. The local unit tests and foundation installer smoke tests pass, and the Xray config and TLS-refresh hook have been tested in disposable local runs.

“What does not exist yet is equally important: we have not run the full installer on a target OS or proven client connectivity to the new service. We do not install or alter the administrator SSH daemon, we do not change the cloud firewall, and VMess is not included. Xray usage is collected in Xray-only mode; the SSH network-meter producer is still a dependency, so SSH byte usage and all quota enforcement remain off. We have not deployed to either of the existing VPS installations. Deployment is planned for a separate clean VPS after the release gates pass.”

## 6. Agree the next milestones

“Our next work is ordered by risk. First, run the full installer on a disposable VPS and validate TLS, VLESS/Trojan connections, usage deltas, expiry, restarts, and recovery. Validate the SSH process view against interactive logins, tunnels, multiple logins, and disconnects. Then select and implement the SSH network-metering method and validate known-byte transfers across reconnects and restarts. Add the remaining disable/renew/delete workflows and service-aware backup/restore. Only after measurement results are trustworthy should we consider quota enforcement.”

“Finally, we test clean installs, authentication, port conflicts, restart persistence, and backup/restore on Ubuntu 22.04, Ubuntu 24.04, and Debian. We deploy only to a new clean VPS—not the systems we inspected.”

## 7. Copyright and alignment questions

“The publisher brand is ISPbandits Dev. We have put an ownership notice in the code and installer and plan to show it in the VPS login message. That helps signal ownership but cannot technically prevent copying. The source should remain in a private GitHub repository when transferred, and the final proprietary license terms should be reviewed before wider distribution.”

“To close, let’s align on four points: the product’s public name; who owns the SSH-metering implementation and acceptance tests; whether VMess belongs in the first public release or stays optional; and who will provision the separate clean test VPS. If we agree on those, we can work through the release gates without risking the current servers.”

---

## Presenter’s one-line takeaway

**One account layer, separate SSH and Xray service paths, one durable SQLite record, and no claims of accurate SSH usage until a real network meter proves it.**
