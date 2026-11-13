from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ispbandits import backup, cli, database


class AccountLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        database.initialize(self.db)
        self.future = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        database.add_user(self.db, "alice", self.future)
        database.link_protocol(self.db, "alice", "openssh", "alice")
        database.create_xray_protocol(self.db, "alice", "vless")

    def tearDown(self):
        self.tmp.cleanup()

    def test_account_details_are_protocol_aware_but_never_return_secrets(self):
        info = database.get_account(self.db, "alice")
        self.assertEqual("alice", info["username"])
        self.assertEqual({"openssh", "vless"}, {p["protocol"] for p in info["protocols"]})
        self.assertTrue(next(p for p in info["protocols"] if p["protocol"] == "vless")["credential_provisioned"])
        self.assertNotIn("credential", info)
        self.assertNotIn("secret", repr(info))

    def test_renew_does_not_implicitly_reactivate_expired_account(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET status='EXPIRED',expiry_date='2020-01-01' WHERE username='alice'")
        conn.commit()
        conn.close()
        result = database.renew_user(self.db, "alice", self.future)
        self.assertEqual("EXPIRED", result["status"])
        self.assertEqual("EXPIRED", database.get_account(self.db, "alice")["status"])

    def test_enable_requires_future_expiry_and_below_quota_then_reenables_links(self):
        database.disable_user(self.db, "alice")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET data_limit=100,status='DISABLED' WHERE username='alice'")
        conn.execute("UPDATE protocol_accounts SET enabled=0")
        conn.execute("INSERT INTO traffic_totals(user_id,protocol,upload_bytes,download_bytes) VALUES(1,'vless',50,50)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "reached its lifetime quota"):
            database.enable_user(self.db, "alice")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET data_limit=101 WHERE username='alice'")
        conn.commit()
        conn.close()
        result = database.enable_user(self.db, "alice")
        self.assertEqual("ACTIVE", result["status"])
        self.assertTrue(all(p["enabled"] for p in database.get_account(self.db, "alice")["protocols"]))

    def test_expired_account_must_be_renewed_before_enable(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET status='EXPIRED',expiry_date='2020-01-01' WHERE username='alice'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "renew it before enabling"):
            database.enable_user(self.db, "alice")

    def test_disable_is_idempotent_and_audited_without_removing_usage(self):
        database.disable_user(self.db, "alice")
        database.disable_user(self.db, "alice")
        info = database.get_account(self.db, "alice")
        self.assertEqual("DISABLED", info["status"])
        self.assertTrue(all(not p["enabled"] for p in info["protocols"]))
        conn = sqlite3.connect(self.db)
        events = conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='user-disabled'").fetchone()[0]
        conn.close()
        self.assertEqual(1, events)

    def test_cli_account_show_never_prints_stored_credential(self):
        secret = database.get_xray_protocol_state(self.db, "alice", "vless")["secret"]
        with patch("sys.stdout") as output:
            result = cli.main(["--db", str(self.db), "account-show", "alice"])
        self.assertEqual(0, result)
        rendered = "".join(str(call.args[0]) for call in output.write.call_args_list if call.args)
        self.assertNotIn(secret, rendered)
        self.assertIn("identity=alice-vless@xray.ispbandits.invalid", rendered)

    def test_disable_cli_revokes_os_and_reconciles_xray(self):
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli.provisioning, "expire_linux_ssh_account") as lock, \
             patch.object(cli, "_reconcile_xray_if_active", return_value=True) as reconcile:
            result = cli.main(["--db", str(self.db), "disable", "alice"])
        self.assertEqual(0, result)
        lock.assert_called_once_with("alice")
        reconcile.assert_called_once()
        self.assertEqual("DISABLED", database.get_account(self.db, "alice")["status"])

    def test_password_reset_is_root_gated_and_secret_is_not_saved(self):
        existing_vless = database.get_xray_protocol_state(self.db, "alice", "vless")["secret"]
        generated = "one-time-password-with-sufficient-randomness"
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli.provisioning, "reset_linux_ssh_password", return_value=generated):
            result = cli.main(["--db", str(self.db), "ssh-password-reset", "alice"])
        self.assertEqual(0, result)
        self.assertEqual(existing_vless, database.get_xray_protocol_state(self.db, "alice", "vless")["secret"])

    def test_failed_xray_rotation_restores_old_credential_and_identity(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE protocol_accounts SET external_identity='legacy-alice-vless@xray.invalid' WHERE protocol='vless'")
        conn.commit()
        conn.close()
        old_secret = database.get_xray_protocol_state(self.db, "alice", "vless")["secret"]
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_reconcile_xray_if_active", side_effect=[RuntimeError("restart failed"), False]):
            result = cli.main(["--db", str(self.db), "xray-provision", "alice", "vless", "--rotate"])
        self.assertEqual(2, result)
        self.assertEqual(old_secret, database.get_xray_protocol_state(self.db, "alice", "vless")["secret"])
        account = database.get_account(self.db, "alice")
        vless = next(p for p in account["protocols"] if p["protocol"] == "vless")
        self.assertEqual("legacy-alice-vless@xray.invalid", vless["identity"])

    def test_ssh_preflight_requires_password_and_tcp_forwarding(self):
        sshd = "port 2222\npasswordauthentication yes\nallowtcpforwarding yes\ndisableforwarding no\nauthenticationmethods any\n"
        active = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(cli.shutil, "which", return_value="/usr/sbin/sshd"), \
             patch.object(cli.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=0, stdout=sshd, stderr=""), active]):
            self.assertEqual([2222], cli._require_working_ssh_login("alice"))
        blocked = sshd.replace("passwordauthentication yes", "passwordauthentication no")
        with patch.object(cli.shutil, "which", return_value="/usr/sbin/sshd"), \
             patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=blocked, stderr="")):
            with self.assertRaisesRegex(RuntimeError, "PasswordAuthentication"):
                cli._require_working_ssh_login("alice")

    def test_confirmed_delete_revokes_before_deleting_database_records(self):
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli.provisioning, "expire_linux_ssh_account") as lock, \
             patch.object(cli.provisioning, "delete_linux_ssh_account") as remove, \
             patch.object(cli, "_reconcile_xray_if_active", return_value=False):
            result = cli.main(["--db", str(self.db), "account-delete", "alice", "--yes"])
        self.assertEqual(0, result)
        lock.assert_called_once_with("alice")
        remove.assert_called_once_with("alice")
        self.assertEqual([], database.list_accounts(self.db))
        conn = sqlite3.connect(self.db)
        event = conn.execute("SELECT detail FROM audit_events WHERE event_type='user-deleted'").fetchone()
        conn.close()
        self.assertIn("alice", event[0])

    def test_cli_restore_quiesces_and_restores_database(self):
        archive = backup.create_backup(self.db, Path(self.tmp.name) / "backups")
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM users")
        conn.commit()
        conn.close()
        inactive = SimpleNamespace(returncode=3, stdout="inactive", stderr="")
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli.subprocess, "run", return_value=inactive), \
             patch.object(cli, "_reconcile_xray_if_active", return_value=False):
            result = cli.main(["--db", str(self.db), "restore", str(archive),
                               "--dir", str(Path(self.tmp.name) / "backups")])
        self.assertEqual(0, result)
        self.assertEqual("alice", database.list_accounts(self.db)[0]["username"])


if __name__ == "__main__":
    unittest.main()
