"""ADB host actions: no connected target required, explicit endpoints, secret handling."""
import io
import json
import sys
import unittest
from unittest.mock import patch

from scripts import android_core as core
from scripts import android_control as cli
from scripts import android_connection as connection
from scripts import android_client as client


class FakeAdb:
    timeout = 0.001

    def __init__(self, reply=b"connected to phone.local:5555", state="device", pairing=b"Successfully paired to phone.local:5556"):
        self.reply, self.state, self.pairing = reply, state, pairing
        self.commands, self.inputs = [], []

    def run(self, args, **kwargs):
        self.commands.append(args)
        if args == ["devices", "-l"]:
            return ("List of devices attached\n" +
                    (f"phone.local:5555 {self.state}\n" if self.state else "")).encode()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def run_input(self, args, data):
        self.commands.append(args)
        self.inputs.append(data)
        if isinstance(self.pairing, Exception):
            raise self.pairing
        return self.pairing


class AndroidConnectionTests(unittest.TestCase):
    def test_endpoint_validation(self):
        for address, expected in (
            ("PHONE.LOCAL:5555", "phone.local:5555"), ("192.168.1.10:5555", "192.168.1.10:5555"),
            ("phone.local:12345", "phone.local:12345"), ("[::1]:5555", "[::1]:5555"),
        ):
            self.assertEqual(connection.validate_address(address), expected)
        for address in ("", "localhost", "-host:5555", "http://host:5555", "host:0", "host:65536",
                        "host:5555;id", "host:5555\n", "user@host:5555", "::1:5555",
                        "[broken]:5555", "999.1.1.1:5555", "host..local:5555", "host:-1"):
            with self.subTest(address=address), self.assertRaises(core.ControlError):
                connection.validate_address(address)
        hostname = ".".join(["a" * 63] * 3 + ["b" * 61])
        core.validate_serial(connection.validate_address(hostname + ":65535"))
        with self.assertRaises(core.ControlError):
            core.prepare_action(core.action_args("connect", address="--bad-option"))

    def test_connection_actions_do_not_resolve_a_connected_target(self):
        args = core.action_args("connect", address="phone.local:5555")
        adb = FakeAdb()
        with patch("scripts.android_core.resolve_adb", return_value="adb"), \
             patch("scripts.android_core.Adb", return_value=adb), \
             patch("scripts.android_core.resolve_serial", side_effect=AssertionError("must not select target")):
            result = core.execute(args)
        self.assertTrue(result["ready"])
        self.assertEqual(adb.commands[0], ["connect", "phone.local:5555"])

    def test_connect_already_connected_and_not_ready(self):
        adb = FakeAdb(reply=b"already connected to phone.local:5555")
        self.assertTrue(connection.connection_action(adb, "connect", "phone.local:5555")["ready"])
        for state in ("offline", "unauthorized", ""):
            with self.subTest(state=state), self.assertRaisesRegex(core.ControlError, "not ready"):
                connection.connection_action(FakeAdb(state=state), "connect", "phone.local:5555")
        with self.assertRaisesRegex(core.ControlError, "connection failed"):
            connection.connection_action(FakeAdb(reply=b"failed to connect: refused"), "connect", "phone.local:5555")

    def test_connect_waits_for_device_and_bracketed_serials_parse(self):
        clock = type("Clock", (), {"monotonic": lambda self: self.now,
                                  "sleep": lambda self, duration: setattr(self, "now", self.now + duration),
                                  "now": 0.0})()
        adb = FakeAdb()
        adb.timeout = 3
        with patch("scripts.android_connection.time", clock), \
             patch("scripts.android_connection.list_targets", side_effect=[
                 [{"target": "phone.local:5555", "state": "offline"}],
                 [{"target": "phone.local:5555", "state": "device"}],
             ]):
            self.assertTrue(connection.connection_action(adb, "connect", "phone.local:5555")["ready"])
        targets = core.parse_devices("List of devices attached\n[::1]:5555 device\n")
        self.assertEqual(targets[0]["target"], "[::1]:5555")
        core.validate_serial("[::1]:5555")

    def test_disconnect_requires_address_and_never_disconnects_all(self):
        for action in ("connect", "disconnect", "pair"):
            with self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args(action))
        adb = FakeAdb(reply=b"disconnected phone.local:5555", state="")
        self.assertTrue(connection.connection_action(adb, "disconnect", "phone.local:5555")["disconnected"])
        self.assertEqual(adb.commands[0], ["disconnect", "phone.local:5555"])
        with self.assertRaisesRegex(core.ControlError, "still listed"):
            connection.connection_action(FakeAdb(), "disconnect", "phone.local:5555")
        already = FakeAdb(reply=core.ControlError("no such device"), state="")
        self.assertTrue(connection.connection_action(already, "disconnect", "phone.local:5555")["already_disconnected"])

    def test_pair_code_stdin_and_error_redaction(self):
        secret = "654321"
        adb = FakeAdb()
        result = connection.connection_action(adb, "pair", "phone.local:5556", secret)
        self.assertTrue(result["paired"])
        self.assertEqual(adb.commands, [["pair", "phone.local:5556"]])
        self.assertEqual(adb.inputs, [b"654321\n"])
        self.assertNotIn(secret, json.dumps(result))
        for pairing in (core.ControlError("failed with " + secret), ("invalid code " + secret).encode()):
            with self.assertRaises(core.ControlError) as caught:
                connection.connection_action(FakeAdb(pairing=pairing), "pair", "phone.local:5556", secret)
            self.assertNotIn(secret, str(caught.exception))
        for code in ("", "12345", "1234567", "123abc"):
            with self.assertRaises(core.ControlError) as caught:
                core.prepare_action(core.action_args("pair", address="phone.local:5556", pair_code=code))
            if code:
                self.assertNotIn(code, str(caught.exception))

    def test_pair_prompt_and_unrelated_argument_rejection(self):
        with patch("scripts.android_control.sys.stdin.isatty", return_value=True), \
             patch("scripts.android_control.getpass.getpass", return_value="654321") as prompt, \
             patch("scripts.android_control.discover_server", return_value=None), \
             patch("scripts.android_control.execute", return_value={"paired": True}) as execute, \
             patch("scripts.android_control.emit"):
            cli.main(["--action", "pair", "--address", "phone.local:5556"])
        prompt.assert_called_once()
        self.assertEqual(execute.call_args.args[0].pair_code, "654321")
        with patch("scripts.android_control.sys.stdin.isatty", return_value=False), \
             self.assertRaisesRegex(core.ControlError, "non-interactive"):
            cli.main(["--action", "pair", "--address", "phone.local:5556"])
        for action, values in (("connect", {"target": "phone"}),
                               ("connect", {"pair_code": "654321"}),
                               ("disconnect", {"target": "phone"}),
                               ("status", {"address": "phone.local:5555"})):
            with self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args(action, **({"address": "phone.local:5555"} | values)))

    def test_forwarding_host_action_has_no_target(self):
        response = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": {"ready": True}}}).encode())
        with patch("scripts.android_client.urlrequest.urlopen", return_value=response) as request:
            self.assertTrue(client.execute_via_server(
                core.action_args("connect", address="phone.local:5555"), "http://127.0.0.1:8000")["ready"])
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent, {"name": "connect", "arguments": {"address": "phone.local:5555"}})

    def test_subprocess_input_is_not_in_argv(self):
        result = core.run_process([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                                  3, input_data=b"secret-test\n")
        self.assertEqual(result, b"secret-test\n")


if __name__ == "__main__":
    unittest.main()
