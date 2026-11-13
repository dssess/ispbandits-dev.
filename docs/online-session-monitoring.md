# Online SSH session monitoring: source behavior and Neverbandits alpha-4

## What Nginx Bandit’s SSH option 1 → 5 does

The SSH account panel labels option 5 **“Check Online Users”** and invokes `janabitech sys online`; it prints a username and a count under the heading “ACTIVE DEVICES.” [1] [2]

The CLI reads a generated `online_users.txt` report. A separate monitor daemon refreshes that file on a 30-second loop. It scans process rows for `sshd` and `dropbear`, ignores selected system users, and retains processes whose account shell is exactly `/bin/false`. It groups matching process IDs by username and writes `username|process_count`. [2] [3]

This is a useful quick online-presence view, but the code counts matching processes, not independently verified devices. One connection could potentially create multiple processes, and a process scan may miss or misclassify edge cases. The report can also be up to one monitor interval old. Therefore, the new system should not call the number “devices” unless testing proves a reliable one-to-one mapping.

The same daemon also reads `/proc/<pid>/io` (`rchar` and `wchar`) for its older bandwidth field. Those are process I/O counters, not a verified network-byte meter. Keep online-session detection and traffic accounting as separate features with separate acceptance tests. [3]

## Neverbandits behavior

The cyan-framed admin menu provides **ONLINE SSH** and the CLI exposes `neverbandits online`. It reports approximate matching SSH/Dropbear process counts for managed users and does not claim a unique device count. Xray live-session display is not implemented; Xray traffic counters are not proof that a user is currently connected.

The view uses managed account identities and does not print client credentials. A monitor failure must not alter or delete an account. This is status reporting, not session-killing or quota enforcement.

Before choosing the exact session detector, validate it on each target distro with: one interactive SSH login, an SSH tunnel with no shell, two concurrent logins for one account, disconnect/reconnect, Dropbear if enabled, and a monitor restart. Compare the displayed count with the actual established sessions. If it cannot reliably count connections, label the UI as approximate and do not describe counts as devices.

Extending the view to Trojan/VLESS live sessions is a separate follow-up. Xray traffic counters are not themselves proof that a user is currently connected; select and test an explicit active-session source before showing Xray users as online.

## References

[1]: https://raw.githubusercontent.com/dssess/nginxbandits/main/menus/main_menu.sh "Nginx Bandit SSH menu and online-user display"
[2]: https://raw.githubusercontent.com/dssess/nginxbandits/main/bin/nginxbandits "Nginx Bandit sys online command"
[3]: https://raw.githubusercontent.com/dssess/nginxbandits/main/services/monitor/daemon.py "Nginx Bandit monitoring daemon"
