"""DND modes, verified changes, CLI routing, and MCP forwarding."""
import io
import json
import unittest
from unittest.mock import patch

from scripts import android_core as core
from scripts import android_client as client
from scripts import android_dnd as dnd


def dump(mode="off", ringer="NORMAL"):
    return f"Ringer mode:\n- mode (internal) = {ringer}\n- mode (external) = {ringer}\n- zen mode:{dnd.DND_MODES.get(mode, mode)}\n"


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeAdb:
    timeout = 0.6

    def __init__(self, mode="off", supported=True, ignore=False):
        self.mode, self.supported, self.ignore = mode, supported, ignore
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args == ["devices", "-l"]:
            return b"List of devices attached\nphone device\n"
        if args == ["shell", "cmd notification 2>&1 || true"]:
            return b"  set_dnd [off|priority|alarms|none]\n" if self.supported else b"unknown service"
        if args == ["shell", "dumpsys", "audio"]:
            return dump(self.mode).encode()
        if args[:4] == ["shell", "cmd", "notification", "set_dnd"]:
            if not self.ignore:
                self.mode = args[4]
            return b""
        raise AssertionError(args)

    def mutations(self):
        return [args for args, _ in self.calls if args[:4] == ["shell", "cmd", "notification", "set_dnd"]]


class AndroidDNDTests(unittest.TestCase):
    def test_parse_all_modes_and_preserve_unknown_values(self):
        for mode, raw in dnd.DND_MODES.items():
            state = dnd.parse_dnd_state(dump(mode, "VIBRATE"))
            self.assertEqual((state["mode"], state["zen_mode"]), (mode, raw))
            self.assertEqual(state["ringer_mode"], "vibrate")
        state = dnd.parse_dnd_state(dump("ZEN_MODE_NEW"))
        self.assertIsNone(state["mode"])
        self.assertEqual(state["zen_mode"], "ZEN_MODE_NEW")
        self.assertIsNone(dnd.parse_dnd_state("")["ringer_mode"])

    def test_get_reports_support_and_explicit_read_errors(self):
        result = dnd.get_dnd(FakeAdb(), "phone")
        self.assertEqual(result, {"target": "phone", "mode": "off", "zen_mode": "ZEN_MODE_OFF",
                                  "ringer_mode": "normal", "ringer_mode_external": "normal",
                                  "supported": True, "errors": []})
        self.assertFalse(dnd.get_dnd(FakeAdb(supported=False), "phone")["supported"])
        with patch("scripts.android_dnd.shell", side_effect=core.ControlError("offline")):
            result = dnd.get_dnd(FakeAdb(), "phone")
        self.assertIsNone(result["mode"])
        self.assertIsNone(result["supported"])
        self.assertTrue(result["errors"])

    def test_each_mode_is_sent_once_and_verified(self):
        for mode in dnd.DND_MODES:
            adb = FakeAdb()
            result = dnd.set_dnd(adb, "phone", mode)
            self.assertTrue(result["verified"])
            self.assertEqual(result["requested"], {"mode": mode})
            self.assertEqual(result["mode"], mode)
            self.assertEqual(adb.mutations(), [["shell", "cmd", "notification", "set_dnd", mode]])
            self.assertTrue(all(kwargs["serial"] == "phone" for args, kwargs in adb.calls))

    def test_invalid_or_unsupported_requests_send_no_mutation(self):
        adb = FakeAdb()
        with self.assertRaisesRegex(core.ControlError, "DND mode"):
            dnd.set_dnd(adb, "phone", "on")
        self.assertEqual(adb.calls, [])
        adb = FakeAdb(supported=False)
        with self.assertRaisesRegex(core.ControlError, "does not advertise"):
            dnd.set_dnd(adb, "phone", "priority")
        self.assertEqual(adb.mutations(), [])
        adb = FakeAdb(mode="UNKNOWN")
        with self.assertRaisesRegex(core.ControlError, "no change was sent"):
            dnd.set_dnd(adb, "phone", "priority")
        self.assertEqual(adb.mutations(), [])

    def test_success_exit_without_change_fails_verification(self):
        adb = FakeAdb(ignore=True)
        with patch("scripts.android_dnd.time", Clock()), \
             self.assertRaisesRegex(core.ControlError, "sent but not verified"):
            dnd.set_dnd(adb, "phone", "priority")
        self.assertEqual(len(adb.mutations()), 1)

    def test_delayed_readback_recovers_and_late_readback_fails(self):
        adb = FakeAdb()
        original = dnd._read_state
        reads = 0
        def delayed(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise core.ControlError("temporary read failure")
            return original(*args, **kwargs)
        with patch("scripts.android_dnd.time", Clock()), patch("scripts.android_dnd._read_state", side_effect=delayed):
            self.assertTrue(dnd.set_dnd(adb, "phone", "priority")["verified"])
        self.assertEqual(len(adb.mutations()), 1)
        clock = Clock()
        reads = 0
        def late(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads > 1:
                clock.now = 100
            return original(*args, **kwargs)
        with patch("scripts.android_dnd.time", clock), patch("scripts.android_dnd._read_state", side_effect=late), \
             self.assertRaisesRegex(core.ControlError, "sent but not verified"):
            dnd.set_dnd(FakeAdb(), "phone", "priority")

    def test_cli_routing_validation_and_forwarding_mode_name(self):
        for action, values in [("set-dnd", {}), ("get-dnd", {"dnd_mode": "off"}),
                               ("status", {"dnd_mode": "off"})]:
            with self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args(action, **values))
        adb = FakeAdb()
        args = core.action_args("set-dnd", dnd_mode="alarms", target="phone")
        core.prepare_action(args)
        self.assertEqual(core.device_action(args, adb)["mode"], "alarms")
        response = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": {"verified": True}}}).encode())
        with patch("scripts.android_client.urlrequest.urlopen", return_value=response) as request:
            client.execute_via_server(args, "http://127.0.0.1:8000")
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent, {"name": "set_dnd", "arguments": {"mode": "alarms", "target": "phone"}})
        self.assertGreater(request.call_args.kwargs["timeout"], args.timeout)


if __name__ == "__main__":
    unittest.main()
