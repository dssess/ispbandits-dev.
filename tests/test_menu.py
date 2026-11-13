import io
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from ispbandits import cli, database, menu


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


class MenuTests(unittest.TestCase):
    def test_explicit_menu_opens_and_exits(self):
        with patch("ispbandits.menu.sys.stdin", _FakeTTY()), \
             patch("builtins.input", return_value="0"):
            self.assertEqual(cli.main(["menu"]), 0)

    def test_no_arguments_opens_menu_in_terminal(self):
        with patch("ispbandits.menu.sys.stdin", _FakeTTY()), \
             patch("builtins.input", return_value="0"):
            self.assertEqual(cli.main([]), 0)

    def test_usage_menu_choice_dispatches_cli_action(self):
        answers = iter(("2", "", "0"))
        with patch("ispbandits.menu.sys.stdin", _FakeTTY()), \
             patch("builtins.input", side_effect=lambda _prompt="": next(answers)), \
             patch("ispbandits.menu._call_cli", return_value=0) as run_action:
            self.assertEqual(cli.main(["menu"]), 0)
        run_action.assert_called_once()
        self.assertEqual(run_action.call_args.args[1], "usage")

    def test_dashboard_displays_framed_server_summary_and_quota_action(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "db.sqlite"
            database.initialize(db_path)
            output = io.StringIO()
            with patch("sys.stdout", output), patch.object(menu, "_service_state", return_value="active"):
                menu._show_dashboard(db_path)
        screen = output.getvalue()
        self.assertIn("┌", screen)
        self.assertIn("Neverbandits — VPS Administration", screen)
        self.assertIn("SSH-WS", screen)
        self.assertIn("Measured traffic:", screen)
        self.assertIn("[08]  SET LIFETIME QUOTA", screen)
        self.assertIn("© 2026 ISPbandits Dev. All rights reserved.", screen)

    def test_no_arguments_print_help_without_terminal(self):
        output = io.StringIO()
        with patch("ispbandits.menu.sys.stdin", io.StringIO()), \
             patch("sys.stdout", output):
            self.assertEqual(cli.main([]), 2)
        self.assertIn("usage: neverbandits", output.getvalue())


if __name__ == "__main__":
    unittest.main()
