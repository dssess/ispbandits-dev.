from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from ispbandits import collector, ssh_meter


class ProcSocketTests(unittest.TestCase):
    def _fake_proc(self, *, local_port: int = 22, comm: str = "sshd", uid: int = 1001):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "net").mkdir()
        (root / "123").mkdir()
        (root / "123" / "fd").mkdir()
        header = "sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode\n"
        line = (f"  0: 0100007F:{local_port:04X} 0200007F:C001 01 "
                "00000000:00000000 00:00000000 00000000 1001 0 123456 1\n")
        (root / "net" / "tcp").write_text(header + line)
        (root / "net" / "tcp6").write_text(header)
        (root / "123" / "comm").write_text(comm + "\n")
        (root / "123" / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (root / "123" / "fd" / "3").symlink_to("socket:[123456]")
        return temporary, root

    def test_proc_ipv4_socket_and_authenticated_worker_map(self):
        temporary, root = self._fake_proc()
        self.addCleanup(temporary.cleanup)
        sockets = ssh_meter.read_proc_tcp(root)
        self.assertEqual(sockets[123456], {
            "family": "ip", "local_ip": "127.0.0.1", "local_port": 22,
            "remote_ip": "127.0.0.2", "remote_port": 49153,
        })
        flows = ssh_meter.discover_flows(root, {1001: "alice"}, {22})
        self.assertEqual(len(flows), 1)
        self.assertEqual(next(iter(flows.values()))["identity"], "alice")

    def test_root_listener_and_non_ssh_port_are_not_attributed(self):
        temp_root, root = self._fake_proc(uid=0)
        self.addCleanup(temp_root.cleanup)
        self.assertEqual(ssh_meter.discover_flows(root, {1001: "alice"}, {22}), {})
        temp_root.cleanup()
        temp_root, root = self._fake_proc(local_port=443)
        self.addCleanup(temp_root.cleanup)
        self.assertEqual(ssh_meter.discover_flows(root, {1001: "alice"}, {22}), {})

    def test_ipv6_proc_address_decoding(self):
        self.assertEqual(ssh_meter._decode_proc_address("00000000000000000000000001000000", 10), "::1")

    def test_duplicate_user_mapping_to_one_socket_fails_closed(self):
        temporary, root = self._fake_proc()
        self.addCleanup(temporary.cleanup)
        # Add a second apparent authenticated worker with a different identity but
        # the same socket inode/tuple; this is ambiguous and must not be counted.
        (root / "124").mkdir()
        (root / "124" / "fd").mkdir()
        (root / "124" / "comm").write_text("sshd\n")
        (root / "124" / "status").write_text("Uid:\t1002\t1002\t1002\t1002\n")
        (root / "124" / "fd" / "3").symlink_to("socket:[123456]")
        with self.assertRaises(ssh_meter.MeterError):
            ssh_meter.discover_flows(root, {1001: "alice", 1002: "bob"}, {22})


class NftAndSnapshotTests(unittest.TestCase):
    def test_missing_state_after_initialization_fails_even_without_nft_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.with_name(state_path.name + ".initialized").write_text("marker")
            with self.assertRaises(ssh_meter.MeterError):
                ssh_meter._read_state(state_path, table_exists=False)

    def test_existing_meter_table_without_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with self.assertRaises(ssh_meter.MeterError):
                ssh_meter._read_state(state_path, table_exists=True)

    def test_flow_rules_match_both_directions_without_a_verdict(self):
        flow = {
            "family": "ip", "local_ip": "192.0.2.10", "local_port": 22,
            "remote_ip": "198.51.100.7", "remote_port": 50000,
        }
        incoming = ssh_meter._flow_rule("abc123", flow, "in")
        outgoing = ssh_meter._flow_rule("abc123", flow, "out")
        self.assertIn("input ip saddr 198.51.100.7 tcp sport 50000 ip daddr 192.0.2.10 tcp dport 22 counter", incoming)
        self.assertIn("output ip saddr 192.0.2.10 tcp sport 22 ip daddr 198.51.100.7 tcp dport 50000 counter", outgoing)
        self.assertNotIn(" drop", incoming + outgoing)
        self.assertNotIn(" accept", incoming + outgoing)

    def test_nft_json_counter_and_rule_handle_parse(self):
        payload = {"nftables": [{"rule": {
            "table": ssh_meter.TABLE, "chain": "input", "handle": 19,
            "expr": [{"counter": {"packets": 3, "bytes": 700}},
                     {"comment": "ispbd-v1-abc-in"}],
        }}]}
        self.assertEqual(ssh_meter._rules_from_json(payload)["ispbd-v1-abc-in"], {
            "chain": "input", "handle": 19, "packets": 3, "bytes": 700,
        })

    def test_snapshot_matches_collector_contract_and_has_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counters.json"
            state = {
                "generation": "gen-1",
                "totals": {"openssh\talice": {"upload_bytes": 123, "download_bytes": 456}},
            }
            ssh_meter._publish_snapshot(path, state, time.time(), {"alice"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            payload = json.loads(path.read_text())
            self.assertIsInstance(payload["sampled_at"], str)
            self.assertEqual(len(payload["counters"]), 1)
            old_path = collector.SSH_COUNTER_FILE
            collector.SSH_COUNTER_FILE = path
            try:
                samples = collector.read_ssh_samples()
            finally:
                collector.SSH_COUNTER_FILE = old_path
            self.assertEqual((samples[0].identity, samples[0].generation,
                              samples[0].upload_bytes, samples[0].download_bytes),
                             ("alice", "gen-1", 123, 456))

    def test_snapshot_omits_deleted_or_unsupported_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counters.json"
            state = {"generation": "gen-1", "totals": {
                "openssh\talice": {"upload_bytes": 1, "download_bytes": 2},
                "openssh\tdeleted": {"upload_bytes": 10, "download_bytes": 20},
                "dropbear\tlegacy": {"upload_bytes": 5, "download_bytes": 5},
            }}
            ssh_meter._publish_snapshot(path, state, time.time(), {"alice"})
            entries = json.loads(path.read_text())["counters"]
            self.assertEqual([entry["external_identity"] for entry in entries], ["alice"])


if __name__ == "__main__":
    unittest.main()
