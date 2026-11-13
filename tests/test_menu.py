import io
import unittest
from unittest.mock import patch

from ispbandits import cli


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

    def test_no_arguments_print_help_without_terminal(self):
        output = io.StringIO()
        with patch("ispbandits.menu.sys.stdin", io.StringIO()), \
             patch("sys.stdout", output):
            self.assertEqual(cli.main([]), 2)
        self.assertIn("usage: ispbandits", output.getvalue())


if __name__ == "__main__":
    unittest.main()
