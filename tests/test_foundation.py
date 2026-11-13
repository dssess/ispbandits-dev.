import sqlite3
import tempfile
import unittest
import datetime as dt
from pathlib import Path
import os
import pwd
from unittest.mock import patch

from ispbandits import backup, collector, database, sessions, provisioning


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "db.sqlite"
        self.backups = self.root / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_new_db_add_user_and_link(self):
        database.initialize(self.db)
        account_id = database.add_user(self.db, "alice", "2027-02-01")
        database.link_protocol(self.db, "alice", "vless", "alice-vless@example.invalid")
        rows = database.list_usage(self.db)
        self.assertEqual(account_id, rows[0]["id"])
        self.assertEqual("alice", rows[0]["username"])
        self.assertEqual(0, rows[0]["total_bytes"])

    def test_migrate_existing_nginxbandit_users_and_keep_legacy_usage(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, data_usage INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO users(id,username,data_usage) VALUES(7,'olduser',5120)")
        conn.commit(); conn.close()
        database.initialize(self.db)
        database.initialize(self.db)  # idempotent migration
        rows = database.list_usage(self.db)
        self.assertEqual(5120, rows[0]["legacy_bytes"])
        self.assertEqual(5120, rows[0]["total_bytes"])
        conn = sqlite3.connect(self.db)
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM traffic_totals WHERE protocol='legacy'").fetchone()[0])
        conn.close()

    def test_backup_and_restore_are_consistent_and_atomic(self):
        database.initialize(self.db)
        database.add_user(self.db, "alice", "2027-02-01")
        archive = backup.create_backup(self.db, self.backups)
        self.assertEqual(0o600, archive.stat().st_mode & 0o777)
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM users")
        conn.commit(); conn.close()
        rollback = backup.restore_backup(self.db, archive, self.backups)
        self.assertTrue(rollback.exists())
        conn = sqlite3.connect(self.db)
        self.assertEqual("alice", conn.execute("SELECT username FROM users").fetchone()[0])
        self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        conn.close()

    def test_reject_bad_username(self):
        database.initialize(self.db)
        with self.assertRaises(ValueError):
            database.add_user(self.db, "../root", "2027-02-01")

    def test_initialize_preserves_existing_parent_directory_mode(self):
        parent = self.root / "existing-parent"
        parent.mkdir(mode=0o755)
        self.db = parent / "db.sqlite"
        database.initialize(self.db)
        self.assertEqual(0o755, parent.stat().st_mode & 0o777)

    def test_backup_rejects_public_existing_directory_without_chmod(self):
        database.initialize(self.db)
        public_dir = self.root / "public-backups"
        public_dir.mkdir(mode=0o755)
        with self.assertRaisesRegex(PermissionError, "must be private"):
            backup.create_backup(self.db, public_dir)
        self.assertEqual(0o755, public_dir.stat().st_mode & 0o777)

    def test_collector_adds_counter_deltas_once(self):
        database.initialize(self.db)
        database.add_user(self.db, "alice", "2027-02-01")
        database.link_protocol(self.db, "alice", "openssh", "alice")
        database.link_protocol(self.db, "alice", "vless", "alice-vless@example.invalid")
        values = {"xup": 100, "xdown": 200, "sup": 50, "sdown": 75}
        xray = lambda: [collector.CounterSample("xray", "alice-vless@example.invalid", "boot:1", values["xup"], values["xdown"])]
        ssh = lambda: [collector.CounterSample("ssh", "alice", "meter:1", values["sup"], values["sdown"], "openssh")]
        collector.DB_PATH = self.db
        with patch.object(collector, "read_xray_samples", xray), patch.object(collector, "read_ssh_samples", ssh):
            self.assertEqual(0, collector.collect()[1])  # initial values are baseline only
            values.update(xup=400, xdown=800, sup=150, sdown=275)
            self.assertEqual(1200, collector.collect()[1])
            self.assertEqual(0, collector.collect()[1])  # repeat poll adds no bytes
        conn = sqlite3.connect(self.db)
        rows = dict((p, (u, d)) for p, u, d in conn.execute("SELECT protocol,upload_bytes,download_bytes FROM traffic_totals"))
        self.assertEqual((300, 600), rows["vless"])
        self.assertEqual((100, 200), rows["openssh"])
        conn.close()

    def test_xray_only_collection_does_not_claim_ssh_usage(self):
        database.initialize(self.db)
        database.add_user(self.db, "alice", "2999-01-01")
        database.link_protocol(self.db, "alice", "vless", "alice-vless@example.invalid")
        current = {"up": 1000, "down": 2000}
        xray = lambda: [collector.CounterSample("xray", "alice-vless@example.invalid",
                                                "boot:2", current["up"], current["down"])]
        collector.DB_PATH = self.db
        with patch.object(collector, "read_xray_samples", xray), \
             patch.object(collector, "read_ssh_samples", side_effect=AssertionError("SSH source must not be read")):
            self.assertEqual(0, collector.collect(xray_only=True)[1])
            current.update(up=1250, down=2600)
            self.assertEqual(850, collector.collect(xray_only=True)[1])
        rows = database.list_usage(self.db)
        self.assertEqual(1, len(rows))
        self.assertEqual("alice", rows[0]["username"])
        self.assertEqual(850, rows[0]["total_bytes"])
        conn = sqlite3.connect(self.db)
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM traffic_totals WHERE protocol IN ('openssh','dropbear')").fetchone()[0])
        conn.close()

    def test_online_view_counts_only_managed_protocol_processes(self):
        account = pwd.getpwuid(os.getuid()) if os.getuid() != 0 else pwd.getpwnam("nobody")
        username = account.pw_name
        database.initialize(self.db)
        database.add_user(self.db, username, "2999-01-01")
        database.link_protocol(self.db, username, "openssh", username)
        proc = self.root / "proc"
        for pid in ("101", "102", "103"):
            proc_pid = proc / pid
            proc_pid.mkdir(parents=True)
            (proc_pid / "comm").write_text("sshd\n", encoding="utf-8")
            (proc_pid / "status").write_text(f"Name:\tsshd\nUid:\t{account.pw_uid}\t{account.pw_uid}\t{account.pw_uid}\t{account.pw_uid}\n", encoding="utf-8")
        with patch.object(sessions.os, "geteuid", return_value=0):
            rows = sessions.online_ssh_processes(self.db, proc)
        self.assertEqual(1, len(rows))
        self.assertEqual(username, rows[0]["username"])
        self.assertEqual("openssh", rows[0]["protocol"])
        self.assertEqual(3, rows[0]["observed_processes"])

    def test_xray_credentials_are_created_once_and_rotated_explicitly(self):
        database.initialize(self.db)
        database.add_user(self.db, "alice", "2999-01-01")
        vless = database.create_xray_protocol(self.db, "alice", "vless")
        trojan = database.create_xray_protocol(self.db, "alice", "trojan")
        self.assertNotEqual(vless["credential"], trojan["credential"])
        self.assertNotEqual(vless["identity"], trojan["identity"])
        with self.assertRaises(ValueError):
            database.create_xray_protocol(self.db, "alice", "vless")
        rotated = database.create_xray_protocol(self.db, "alice", "trojan", rotate=True)
        self.assertNotEqual(trojan["credential"], rotated["credential"])
        conn = sqlite3.connect(self.db)
        saved = dict(conn.execute("SELECT protocol,secret FROM protocol_secrets"))
        conn.close()
        self.assertEqual(vless["credential"], saved["vless"])
        self.assertEqual(rotated["credential"], saved["trojan"])

    def test_managed_account_creation_and_rollback(self):
        database.initialize(self.db)
        with patch.object(provisioning, "create_linux_ssh_account", return_value="temporary-secret") as create_ssh:
            result = provisioning.create_managed_account(self.db, "alice", "2999-01-01",
                                                          protocols=("openssh", "vless", "trojan"))
        create_ssh.assert_called_once()
        self.assertEqual("temporary-secret", result["ssh_password"])
        self.assertEqual({"vless", "trojan"}, {c["protocol"] for c in result["xray_credentials"]})
        conn = sqlite3.connect(self.db)
        self.assertEqual({"openssh", "vless", "trojan"},
                         {row[0] for row in conn.execute("SELECT protocol FROM protocol_accounts")})
        conn.close()

        database.delete_user_record(self.db, "alice")
        with patch.object(provisioning, "create_linux_ssh_account", return_value="temporary-secret"), \
             patch.object(provisioning, "delete_linux_ssh_account") as delete_ssh, \
             patch.object(database, "create_xray_protocol", side_effect=ValueError("injected failure")):
            with self.assertRaises(ValueError):
                provisioning.create_managed_account(self.db, "bob", "2999-01-01",
                                                      protocols=("openssh", "vless"))
        delete_ssh.assert_called_once_with("bob")
        self.assertEqual([], [row for row in database.list_usage(self.db) if row["username"] == "bob"])

    def test_expiry_marks_only_due_active_users_and_is_idempotent(self):
        database.initialize(self.db)
        database.add_user(self.db, "alice", "2026-11-12")
        database.link_protocol(self.db, "alice", "openssh", "alice")
        database.link_protocol(self.db, "alice", "vless", "alice-vless@example.invalid")
        database.add_user(self.db, "bob", "2026-11-14")
        today = dt.date(2026, 11, 13)
        self.assertEqual(["alice"], database.expire_users(self.db, today))
        self.assertEqual([], database.expire_users(self.db, today))
        statuses = {row["username"]: row["status"] for row in database.list_usage(self.db)}
        self.assertEqual("EXPIRED", statuses["alice"])
        self.assertEqual("ACTIVE", statuses["bob"])
        self.assertEqual([], database.list_enabled_protocols(self.db, "alice"))


if __name__ == "__main__":
    unittest.main()
