from __future__ import annotations

import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ispbandits import cli, database, provisioning, xray_config


ROOT = Path(__file__).resolve().parents[1]


class SSHWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        database.initialize(self.db)
        expiry = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        database.add_user(self.db, "alice", expiry)
        database.create_xray_protocol(self.db, "alice", "vless")
        database.create_xray_protocol(self.db, "alice", "trojan")

    def tearDown(self):
        self.tmp.cleanup()

    def test_xray_config_uses_loopback_vless_ws_and_keeps_trojan_tls(self):
        with patch.object(xray_config, "_validate_tls", return_value=("/cert.pem", "/key.pem")):
            config = xray_config.build_config(self.db, "/cert.pem", "/key.pem")
        inbounds = {item["protocol"]: item for item in config["inbounds"]}
        vless = inbounds["vless"]
        self.assertEqual("127.0.0.1", vless["listen"])
        self.assertEqual(10001, vless["port"])
        self.assertEqual("ws", vless["streamSettings"]["network"])
        self.assertEqual("none", vless["streamSettings"]["security"])
        self.assertEqual("/vless", vless["streamSettings"]["wsSettings"]["path"])
        trojan = inbounds["trojan"]
        self.assertEqual(8443, trojan["port"])
        self.assertEqual("0.0.0.0", trojan["listen"])
        self.assertEqual("tls", trojan["streamSettings"]["security"])
        self.assertEqual("127.0.0.1:10085", config["api"]["listen"])

    def test_connection_sheet_includes_ws_wss_vless_ws_and_trojan(self):
        credentials = [
            {"protocol": "vless", "credential": "11111111-1111-4111-8111-111111111111"},
            {"protocol": "trojan", "credential": "secret-value"},
        ]
        with patch.object(cli, "_install_info", return_value={"domain": "vpn.example.test"}), \
             patch.object(cli, "_sshws_ready", return_value=True), \
             patch.object(cli.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout="203.0.113.42 STREAM vpn.example.test\n", stderr="")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            cli._print_connection_details("alice", credentials, has_ssh=True, ssh_ports=[2222])
        rendered = output.getvalue()
        self.assertIn("Host/domain: vpn.example.test", rendered)
        self.assertIn("203.0.113.42", rendered)
        self.assertIn("Direct SSH: host vpn.example.test; port(s) 2222", rendered)
        self.assertIn("ws://vpn.example.test:80/sshws", rendered)
        self.assertIn("wss://vpn.example.test:443/sshws", rendered)
        self.assertIn("GET /sshws HTTP/1.1", rendered)
        self.assertIn("type=ws", rendered)
        self.assertIn("path=%2Fvless", rendered)
        self.assertIn("trojan://secret-value@vpn.example.test:8443", rendered)
        self.assertEqual(1, rendered.count("secret-value"))
        self.assertNotIn("client credential:", rendered)

    def test_sshws_readiness_checks_recorded_loopback_ssh_port(self):
        with patch.object(cli, "_install_info", return_value={
                "ssh_websocket": "1", "ssh_target_port": "2222"}), \
             patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             patch.object(cli.socket, "create_connection") as connect:
            self.assertTrue(cli._sshws_ready())
        connect.assert_called_once_with(("127.0.0.1", 2222), timeout=2)

        with patch.object(cli, "_install_info", return_value={
                "ssh_websocket": "1", "ssh_target_port": "2222"}), \
             patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             patch.object(cli.socket, "create_connection", side_effect=OSError("closed")):
            self.assertFalse(cli._sshws_ready())

    def test_account_create_hidden_prompt_passes_password_to_provisioning_only(self):
        expiry = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        fake_result = {"username": "alice", "expiry_date": expiry,
                       "ssh_password": "chosen-secret", "xray_credentials": []}
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_ssh_meter_timer_active", return_value=False), \
             patch.object(cli, "_require_working_ssh_login", return_value=[22]), \
             patch.object(cli, "_install_info", return_value={
                 "full_xray": "1", "ssh_websocket": "1", "ssh_target_port": "22"}), \
             patch.object(cli, "_sshws_ready", return_value=True), \
             patch.object(cli.provisioning, "create_managed_account", return_value=fake_result) as create, \
             patch.object(cli, "_xray_service_active", return_value=False), \
             patch.object(cli, "_print_connection_details"), \
             patch.object(cli.getpass, "getpass", side_effect=["chosen-secret", "chosen-secret"]):
            result = cli.main(["--db", str(self.db), "account-create", "alice", "--expires", expiry,
                               "--protocols", "openssh", "--prompt-password"])
        self.assertEqual(0, result)
        self.assertEqual("chosen-secret", create.call_args.kwargs["ssh_password"])
        self.assertEqual(("openssh",), create.call_args.kwargs["protocols"])

    def test_managed_provisioning_passes_chosen_password_to_linux_account_helper(self):
        expiry = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        with patch.object(provisioning, "create_linux_ssh_account", return_value="chosen-secret") as create_linux:
            result = provisioning.create_managed_account(
                self.db, "bob", expiry, protocols=("openssh",), ssh_password="chosen-secret")
        create_linux.assert_called_once_with("bob", expiry, "chosen-secret")
        self.assertEqual("chosen-secret", result["ssh_password"])

    def test_account_creation_refuses_foundation_only_install(self):
        expiry = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_install_info", return_value={"full_xray": "0"}), \
             patch.object(cli.provisioning, "create_managed_account") as create, \
             patch("sys.stderr", new_callable=io.StringIO):
            result = cli.main(["--db", str(self.db), "account-create", "bob", "--expires", expiry,
                               "--protocols", "openssh"])
        self.assertEqual(2, result)
        create.assert_not_called()

    def test_vless_creation_refuses_inactive_xray_before_provisioning(self):
        expiry = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        with patch.object(cli.os, "geteuid", return_value=0), \
             patch.object(cli, "_install_info", return_value={
                 "full_xray": "1", "domain": "vpn.example.test"}), \
             patch.object(cli, "_xray_service_active", return_value=False), \
             patch.object(cli.provisioning, "create_managed_account") as create, \
             patch("sys.stderr", new_callable=io.StringIO):
            result = cli.main(["--db", str(self.db), "account-create", "bob", "--expires", expiry,
                               "--protocols", "vless"])
        self.assertEqual(2, result)
        create.assert_not_called()

    def test_nginx_template_routes_only_managed_paths_to_loopback_services(self):
        template = (ROOT / "nginx" / "neverbandits.conf.in").read_text(encoding="utf-8")
        rendered = template.replace("@DOMAIN@", "vpn.example.test")
        self.assertIn("listen 80;", rendered)
        self.assertIn("listen 443 ssl;", rendered)
        self.assertIn("location ^~ /sshws", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:6080;", rendered)
        self.assertIn("location ^~ /vless", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:10001;", rendered)
        self.assertNotIn("proxy_pass http://127.0.0.1:22", rendered)
        self.assertNotIn("listen 10001", rendered)


if __name__ == "__main__":
    unittest.main()
