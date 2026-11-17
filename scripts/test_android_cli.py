"""CLI routing, startup rules, and the executable's public output contract."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import android_control as cli
from scripts.test_support import python_command


class AndroidCLITests(unittest.TestCase):
    def test_execution_routing_and_failed_forward_is_not_retried(self):
        for flags, discovered, forwarded in (
            ([], None, False),
            ([], "http://localhost:8000", True),
            (["--direct"], "http://localhost:8000", False),
            (["--adb", "/fake/adb"], "http://localhost:8000", False),
        ):
            with self.subTest(flags=flags, discovered=discovered), \
                 patch.object(cli, "discover_server", return_value=discovered) as discover, \
                 patch.object(cli, "execute", return_value={"route": "direct"}) as direct, \
                 patch.object(cli, "execute_via_server", return_value={"route": "server"}) as server, \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--action", "status", *flags]), 0)
                self.assertEqual(json.loads(output.getvalue()), {"route": "server" if forwarded else "direct"})
                self.assertEqual(discover.call_count, 0 if flags else 1)
                self.assertEqual(direct.call_count, int(not forwarded))
                self.assertEqual(server.call_count, int(forwarded))
                if forwarded:
                    self.assertEqual(server.call_args.args[1], discovered)
                selected = server if forwarded else direct
                self.assertEqual(selected.call_args.args[0].adb, "/fake/adb" if "--adb" in flags else None)

        with patch.object(cli, "discover_server", return_value="http://localhost:8000"), \
             patch.object(cli, "execute_via_server", side_effect=cli.ControlError("daemon unavailable")) as server, \
             patch.object(cli, "execute") as direct:
            with self.assertRaisesRegex(cli.ControlError, "daemon unavailable"):
                cli.main(["--action", "tap", "--x", "1", "--y", "2"])
            server.assert_called_once()
            direct.assert_not_called()

    def test_daemon_startup_validation_and_pairing_prompt(self):
        for flags, expected, interrupt in (
            ([], ("127.0.0.1", 8000, None, 30, None), False),
            (["--host", "localhost", "--port", "9123", "--adb", "/fake/adb", "--timeout", "2"],
             ("localhost", 9123, "/fake/adb", 2, None), True),
            (["--public-base-url", "https://review.example:40000/phone/"],
             ("127.0.0.1", 8000, None, 30, "https://review.example:40000/phone"), False),
        ):
            with self.subTest(flags=flags), patch.object(
                cli, "run_mcp", side_effect=KeyboardInterrupt if interrupt else None,
            ) as daemon:
                self.assertEqual(cli.main(["--mcp", *flags]), 0)
                daemon.assert_called_once_with(*expected)

        for flags in (
            ["--mcp", "--direct"], ["--mcp", "--target", "phone"],
            ["--mcp", "--name", "example"], ["--mcp", "--port", "-1"],
            ["--mcp", "--port", "65536"],
            ["--action", "status", "--host", "localhost"],
            ["--action", "status", "--port", "9123"],
            ["--action", "status", "--public-base-url", "https://example.com"],
            *(["--mcp", "--public-base-url", url] for url in (
                "relative", "file:///tmp", "http://user:password@example.com", "https://example.com/?x=1",
                "https://example.com/#fragment", "http://example.com:0", "http://example.com:99999",
                "https://example.com/with space",
            )),
        ):
            with self.subTest(flags=flags), patch.object(cli, "run_mcp") as daemon, \
                 patch.object(cli, "execute") as direct:
                with self.assertRaises(cli.ControlError):
                    cli.main(flags)
                daemon.assert_not_called()
                direct.assert_not_called()

        pair = ["--action", "pair", "--address", "phone.local:5556", "--direct"]
        for tty, supplied in ((True, False), (False, False), (False, True)):
            with self.subTest(tty=tty, supplied=supplied), \
                 patch.object(cli.sys.stdin, "isatty", return_value=tty), \
                 patch.object(cli.getpass, "getpass", return_value="654321") as prompt, \
                 patch.object(cli, "execute", return_value={"paired": True}) as execute, \
                 redirect_stdout(io.StringIO()) as output:
                flags = [*pair, *(["--pair-code", "654321"] if supplied else [])]
                if not tty and not supplied:
                    with self.assertRaisesRegex(cli.ControlError, "non-interactive pairing"):
                        cli.main(flags)
                    execute.assert_not_called()
                else:
                    self.assertEqual(cli.main(flags), 0)
                    self.assertEqual(execute.call_args.args[0].pair_code, "654321")
                    self.assertNotIn("654321", output.getvalue())
                self.assertEqual(prompt.call_count, int(tty and not supplied))

    def test_executable_json_errors_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adb = root / "adb"
            adb.write_text("#!/usr/bin/env python3\nimport os\nprint(os.environ['TEST_ADB_DEVICES'])\n")
            adb.chmod(0o755)
            env = dict(os.environ, ANDROID_CONTROL_STATE_FILE=str(root / "state.json"),
                       ANDROID_CONTROL_SERVER_FILE=str(root / "server.json"))
            env.pop("ANDROID_SERIAL", None)
            for action, devices, expected_code, message in (
                ("list-targets", "phone device", 0, None),
                ("status", "", 1, "error:"),
                ("status", "phone device\nother device", 1, "warning:"),
                ("invalid-action", "", 2, "invalid choice"),
            ):
                with self.subTest(action=action, devices=devices):
                    result = subprocess.run(
                        python_command(Path(__file__).with_name("android_control.py"),
                                       "--action", action, "--adb", adb),
                        env=dict(env, TEST_ADB_DEVICES="List of devices attached\n" + devices),
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, expected_code, result.stderr)
                    if expected_code == 0:
                        self.assertEqual(json.loads(result.stdout)[0]["target"], "phone")
                        self.assertEqual(result.stderr, "")
                    else:
                        self.assertEqual(result.stdout, "")
                        self.assertIn(message, result.stderr)
