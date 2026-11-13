from __future__ import annotations

import datetime as dt
import io
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import patch

from ispbandits import cli, collector, database, provisioning


class LifetimeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ispbandits.db"
        database.initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _add_user(self, username: str, limit: int = 0) -> int:
        user_id = database.add_user(self.db, username, "2999-12-31", data_limit=limit)
        database.link_protocol(self.db, username, "openssh", username)
        database.link_protocol(self.db, username, "vless", f"{username}-vless@xray.invalid")
        return user_id

    def _add_traffic(self, user_id: int, protocol: str, up: int, down: int, legacy: int = 0):
        conn = sqlite3.connect(self.db)
        conn.execute("""INSERT INTO traffic_totals
            (user_id,protocol,upload_bytes,download_bytes,legacy_bytes)
            VALUES(?,?,?,?,?)""", (user_id, protocol, up, down, legacy))
        conn.commit()
        conn.close()

    def test_quota_uses_combined_measured_ssh_and_xray_bytes(self):
        user_id = self._add_user("alice", 1000)
        self._add_traffic(user_id, "openssh", 200, 300)
        self._add_traffic(user_id, "vless", 250, 250)
        self.assertEqual(["alice"], database.enforce_data_quotas(self.db))
        row = database.list_usage(self.db)[0]
        self.assertEqual("QUOTA_EXCEEDED", row["status"])
        self.assertEqual(1000, row["measured_bytes"])
        self.assertEqual(1000, row["data_limit"])
        self.assertEqual([], database.list_enabled_protocols(self.db, "alice"))
        self.assertEqual([], database.enforce_data_quotas(self.db))

    def test_legacy_imported_usage_does_not_trigger_a_new_measured_quota(self):
        user_id = self._add_user("alice", 100)
        self._add_traffic(user_id, "legacy", 0, 0, legacy=50_000)
        self.assertEqual([], database.enforce_data_quotas(self.db))
        self.assertEqual("ACTIVE", database.list_usage(self.db)[0]["status"])
        self.assertEqual(50_000, database.list_usage(self.db)[0]["legacy_bytes"])
        self.assertEqual(0, database.list_usage(self.db)[0]["measured_bytes"])

    def test_quota_uses_greater_than_or_equal_boundary_and_is_audited_once(self):
        user_id = self._add_user("alice", 100)
        self._add_traffic(user_id, "openssh", 50, 49)
        self.assertEqual([], database.enforce_data_quotas(self.db))
        self._add_traffic(user_id, "vless", 0, 1)
        self.assertEqual(["alice"], database.enforce_data_quotas(self.db))
        self.assertEqual([], database.enforce_data_quotas(self.db))
        self.assertEqual(["alice"], database.list_quota_exceeded_users(self.db))
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='quota-exceeded'").fetchone()[0]
        conn.close()
        self.assertEqual(1, count)

    def test_full_collect_retries_external_revocation_for_existing_quota_lock(self):
        self._add_user("alice", 100)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET status='QUOTA_EXCEEDED' WHERE username='alice'")
        conn.commit()
        conn.close()
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli.collector, "collect", return_value=(1, 0, 0)), \
             patch.object(cli, "_quota_revoke") as revoke:
            result = cli.main(["--db", str(self.db), "collect", "--enforce-quotas"])
        self.assertEqual(0, result)
        revoke.assert_called_once_with(self.db, ["alice"])

    def test_nonzero_quota_is_rejected_without_a_fresh_active_meter(self):
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_ssh_meter_ready", return_value=False), \
             patch.object(provisioning, "create_managed_account") as create:
            result = cli.main(["--db", str(self.db), "account-create", "alice",
                               "--expires", "2999-12-31", "--limit-gb", "1"])
        self.assertEqual(2, result)
        create.assert_not_called()

    def test_account_create_seeds_xray_checkpoint_before_disclosing_secret(self):
        output = io.StringIO()
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_install_info", return_value={
                 "full_xray": "1", "domain": "vpn.example.test"}), \
             patch.object(cli, "_xray_service_active", return_value=True), \
             patch.object(cli, "_service_active", return_value=True), \
             patch.object(cli, "_ssh_meter_timer_active", return_value=False), \
             patch.object(cli.xray_config, "build_config", return_value={}), \
             patch.object(cli.xray_config, "apply_config"), \
             patch.object(cli.collector, "read_xray_samples", return_value=[]), \
             patch.object(cli.collector, "xray_generation", return_value="xray-generation"), \
             patch("sys.stdout", output):
            result = cli.main(["--db", str(self.db), "account-create", "alice",
                               "--expires", "2999-12-31", "--protocols", "vless"])
        self.assertEqual(0, result)
        conn = sqlite3.connect(self.db)
        checkpoints = conn.execute("SELECT protocol,generation,last_counter_bytes FROM traffic_checkpoints").fetchall()
        conn.close()
        self.assertEqual([("vless", "xray-generation", 0),
                          ("vless", "xray-generation", 0)], checkpoints)
        self.assertIn("VLESS import link (secret; shown once)", output.getvalue())

    def test_dropbear_provisioning_is_rejected_while_meter_is_active(self):
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_ssh_meter_timer_active", return_value=True), \
             patch.object(provisioning, "create_managed_account") as create:
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--db", str(self.db), "account-create", "alice",
                          "--expires", "2999-12-31", "--protocols", "dropbear"])
        self.assertEqual(2, raised.exception.code)
        create.assert_not_called()

    def test_shared_quota_rejects_an_enabled_unmanaged_protocol(self):
        database.add_user(self.db, "alice", "2999-12-31")
        database.link_protocol(self.db, "alice", "vmess", "alice-vmess@xray.invalid")
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_ssh_meter_ready", return_value=True):
            result = cli.main(["--db", str(self.db), "quota-set", "alice", "--limit-gb", "10"])
        self.assertEqual(2, result)
        self.assertEqual(0, database.list_usage(self.db)[0]["data_limit"])

    def test_quota_lift_does_not_reactivate_disabled_unsupported_protocol(self):
        user_id = database.add_user(self.db, "alice", "2999-12-31", data_limit=100)
        database.link_protocol(self.db, "alice", "vmess", "alice-vmess@xray.invalid")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET status='QUOTA_EXCEEDED' WHERE username='alice'")
        conn.execute("UPDATE protocol_accounts SET enabled=0 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        with patch.object(cli.os, "geteuid", return_value=0):
            result = cli.main(["--db", str(self.db), "quota-set", "alice", "--limit-gb", "0"])
        self.assertEqual(2, result)
        self.assertEqual("QUOTA_EXCEEDED", database.get_account(self.db, "alice")["status"])
        self.assertEqual([], database.list_enabled_protocols(self.db, "alice"))

    def test_xray_only_quota_revoke_does_not_touch_a_same_named_local_user(self):
        database.add_user(self.db, "alice", "2999-12-31")
        database.link_protocol(self.db, "alice", "vless", "alice-vless@xray.invalid")
        inactive = subprocess.CompletedProcess(["systemctl"], 3, stdout="inactive", stderr="")
        with patch.object(cli.subprocess, "run", return_value=inactive), \
             patch.object(provisioning, "expire_linux_ssh_account") as expire_ssh:
            cli._quota_revoke(self.db, ["alice"])
        expire_ssh.assert_not_called()

    def test_new_identity_checkpoints_prevent_first_traffic_sample_loss(self):
        user_id = database.add_user(self.db, "alice", "2999-12-31", ssh_protocols=("openssh",))
        database.link_protocol(self.db, "alice", "vless", "alice-vless@xray.invalid")
        ssh_zero = collector.CounterSample("ssh", "alice", "meter-generation", 0, 0, "openssh")
        seeded = collector.seed_account_checkpoints(
            self.db, "alice", [], [ssh_zero], xray_generation_id="xray-generation")
        self.assertEqual(4, seeded)

        conn = sqlite3.connect(self.db)
        at = collector.utc_iso()
        deltas = []
        for protocol, source, generation, values in (
            ("vless", "xray:alice-vless@xray.invalid", "xray-generation", (17, 23)),
            ("openssh", "ssh:openssh:alice", "meter-generation", (31, 41)),
        ):
            for direction, value in zip(("upload", "download"), values):
                delta, _ = collector.apply_one_counter(
                    conn, user_id, protocol, source, generation, direction, value, at)
                deltas.append(delta)
        conn.commit()
        conn.close()
        self.assertEqual([17, 23, 31, 41], deltas)
        row = database.list_usage(self.db)[0]
        self.assertEqual(112, row["measured_bytes"])

    def test_quota_set_reactivates_only_if_new_limit_is_above_measured_usage(self):
        user_id = self._add_user("alice", 100)
        self._add_traffic(user_id, "openssh", 80, 30)
        self.assertEqual(["alice"], database.enforce_data_quotas(self.db))
        at_limit = database.set_data_limit(self.db, "alice", 110)
        self.assertEqual("QUOTA_EXCEEDED", at_limit["status"])
        self.assertEqual([], database.list_enabled_protocols(self.db, "alice"))
        reopened = database.set_data_limit(self.db, "alice", 111)
        self.assertEqual("ACTIVE", reopened["status"])
        self.assertEqual("QUOTA_EXCEEDED", reopened["old_status"])
        self.assertEqual("2999-12-31", reopened["expiry_date"])
        self.assertEqual({("openssh", "alice"), ("vless", "alice-vless@xray.invalid")},
                         set(database.list_enabled_protocols(self.db, "alice")))
        unlimited = database.set_data_limit(self.db, "alice", 0)
        self.assertEqual("ACTIVE", unlimited["status"])
        self.assertEqual(0, database.list_usage(self.db)[0]["data_limit"])

    def test_quota_set_cli_reactivates_supported_links_after_limit_increase(self):
        user_id = self._add_user("alice", 100)
        self._add_traffic(user_id, "openssh", 80, 30)
        self.assertEqual(["alice"], database.enforce_data_quotas(self.db))
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_ssh_meter_ready", return_value=True), \
             patch.object(cli.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=2000)), \
             patch.object(cli, "_quota_reactivate") as reactivate:
            result = cli.main(["--db", str(self.db), "quota-set", "alice", "--limit-gb", "1"])
        self.assertEqual(0, result)
        self.assertEqual("ACTIVE", database.get_account(self.db, "alice")["status"])
        self.assertTrue(all(p["enabled"] for p in database.get_account(self.db, "alice")["protocols"]))
        reactivate.assert_called_once_with(self.db, "alice", "2999-12-31")

    def test_failed_quota_reactivation_restores_old_cap_and_database_lock(self):
        user_id = self._add_user("alice", 100)
        self._add_traffic(user_id, "openssh", 80, 30)
        self.assertEqual(["alice"], database.enforce_data_quotas(self.db))
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_ssh_meter_ready", return_value=True), \
             patch.object(cli.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=2000)), \
             patch.object(cli, "_quota_reactivate", side_effect=RuntimeError("Xray failed")), \
             patch.object(cli, "_quota_revoke") as revoke:
            result = cli.main(["--db", str(self.db), "quota-set", "alice", "--limit-gb", "1"])
        self.assertEqual(2, result)
        account = database.get_account(self.db, "alice")
        self.assertEqual("QUOTA_EXCEEDED", account["status"])
        self.assertEqual(100, account["data_limit"])
        self.assertTrue(all(not p["enabled"] for p in account["protocols"]))
        revoke.assert_called_once_with(self.db, ["alice"])

    def test_expired_account_is_not_reactivated_by_quota_update(self):
        database.add_user(self.db, "expireduser", "2020-01-01", data_limit=10)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET status='EXPIRED' WHERE username='expireduser'")
        conn.commit()
        conn.close()
        result = database.set_data_limit(self.db, "expireduser", 0)
        self.assertEqual("EXPIRED", result["status"])
        self.assertEqual("EXPIRED", database.list_usage(self.db)[0]["status"])

    def test_account_provisioning_saves_quota_bytes(self):
        user_id = None
        # Avoid changing the host account database; only exercise the database seam.
        result = provisioning.create_managed_account(
            self.db, "alice", "2999-12-31", protocols=("vless",), data_limit=7_000_000_000)
        self.assertEqual("alice", result["username"])
        conn = sqlite3.connect(self.db)
        user_id, limit_bytes = conn.execute(
            "SELECT id,data_limit FROM users WHERE username='alice'").fetchone()
        conn.close()
        self.assertGreater(user_id, 0)
        self.assertEqual(7_000_000_000, limit_bytes)

    def test_quota_reconciliation_skips_relocking_an_already_locked_linux_user(self):
        status = subprocess.CompletedProcess(["passwd"], 0,
                                             stdout="alice L 2026-11-13 0 99999 7 -1\n",
                                             stderr="")
        with patch.object(provisioning.os, "geteuid", return_value=0), \
             patch.object(provisioning.pwd, "getpwnam", return_value=SimpleNamespace(pw_name="alice")), \
             patch.object(provisioning.shutil, "which", side_effect=lambda name: {
                 "passwd": "/usr/bin/passwd", "usermod": "/usr/sbin/usermod",
                 "pkill": "/usr/bin/pkill",
             }.get(name)), \
             patch.object(provisioning, "_run", side_effect=[status, subprocess.CompletedProcess(["pkill"], 0)]) as run:
            provisioning.expire_linux_ssh_account("alice")
        commands = [call.args[0][0] for call in run.call_args_list]
        self.assertEqual(["/usr/bin/passwd", "/usr/bin/pkill"], commands)


if __name__ == "__main__":
    unittest.main()
