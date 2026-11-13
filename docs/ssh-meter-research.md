# SSH Meter Design References

Research notes saved 2026-11-13 for ISPbandits Dev's code-first SSH metering/quota work. This note records external sources used; it does not claim that a live SSH meter has been validated.

## Meter attribution and packet counters

- OpenSSH's upstream privilege-separation description says that after authentication, a post-authentication `sshd-session` child runs as the authenticated user and continues handling the network socket. This supports testing a process-FD-to-socket-inode mapping for OpenSSH, but it is not proof that every distro build and connection mode behaves identically: [OpenSSH `README.privsep`](https://github.com/openssh/openssh-portable/blob/master/README.privsep).
- Linux documents that `/proc/<pid>/fd` socket symlinks expose a socket inode that can be correlated with information under `/proc/net`; permissions and races still need handling: [`proc_pid_fd(5)`](https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html).
- nftables counters count packets and bytes, and its verdict semantics matter when coexisting with other firewall chains. Separate non-verdict counter chains can be ordered by hook priority; an `accept` in one base chain does not override a later chain's `drop`: [nftables manual](https://www.netfilter.org/projects/nftables/manpage.html) and [Netfilter hook priorities](https://wiki.nftables.org/wiki-nftables/index.php/Netfilter_hooks).
- These references do not establish the required end-to-end mapping on Ubuntu 22/24 or Debian, nor quota overshoot bounds. The OpenSSH producer is therefore experimental until validated with live sessions and known byte transfers. Dropbear is not assumed to share OpenSSH's process model.

## Menu visual references

- Nevermore's standalone [`menu4.sh`](https://raw.githubusercontent.com/NevermoreSSH/Blueblue/main/menu4.sh) shows a cyan-framed server-information panel, colored service indicators, and bright numbered protocol/actions. ISPbandits may borrow these visual conventions only.
- Nginx Bandits' [`main_menu.sh`](https://raw.githubusercontent.com/dssess/nginxbandits/main/menus/main_menu.sh) uses a cyan dashboard with server/system metrics, service state and account counts, then category panels and submenus.
- The Nevermore installer [`setup.sh`](https://raw.githubusercontent.com/NevermoreSSH/Blueblue/main/setup.sh) is not an implementation dependency and must never be run as part of this project; only the menu presentation was inspected.
