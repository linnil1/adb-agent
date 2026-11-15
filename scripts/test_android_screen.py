"""Offline screen state, command, and verification tests."""
import io
import json
import unittest
from unittest.mock import ANY, patch

from scripts import android_client as client
from scripts import android_core as core
from scripts import android_screen as screen


DISPLAY = """
  mInteractive=true
  mDisplayId=0
  mBaseDisplayInfo=DisplayInfo{"Built-in", displayId 0, real 1080 x 2520, rotation 0, state ON}
  mOverrideDisplayInfo=DisplayInfo{"Built-in", displayId 0, real 2520 x 1080, rotation 1, state ON}
  mDisplayId=1
  mOverrideDisplayInfo=DisplayInfo{"External", displayId 1, real 1920 x 1080, rotation 2, state OFF}
"""


class FakeAdb:
    timeout = 0.05

    def __init__(self, power="mWakefulness=Awake", display=DISPLAY, rotation="free"):
        self.values = {
            ("dumpsys", "power"): power,
            ("dumpsys", "display"): display,
            ("wm", "user-rotation"): rotation,
        }
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command == ["devices", "-l"]:
            return b"List of devices attached\nphone device\n"
        args = tuple(command[1:])
        value = self.values.get(args, "")
        if isinstance(value, Exception):
            raise value
        return value.encode()


class AndroidScreenTests(unittest.TestCase):
    def test_default_display_uses_logical_override(self):
        state = screen.parse_screen_state("mWakefulness=Awake", DISPLAY, "lock 3")
        self.assertEqual((state["width"], state["height"], state["rotation"]), (2520, 1080, 90))
        self.assertEqual((state["rotation_mode"], state["locked_rotation"]), ("locked", 270))
        self.assertEqual((state["display_state"], state["interactive"]), ("on", True))

    def test_unknown_and_older_dump_formats(self):
        state = screen.parse_screen_state("", "", "Error: unsupported")
        self.assertIsNone(state["interactive"])
        self.assertIsNone(state["rotation"])
        self.assertIsNone(state["width"])
        self.assertEqual(state["rotation_mode"], "unknown")
        display = "mDisplayId=0\nmDisplayInfo=DisplayInfo{real 800 x 1280, rotation 0, state DOZE_SUSPEND}"
        state = screen.parse_screen_state("mWakefulness=Dozing", display, "free")
        self.assertEqual((state["display_state"], state["interactive"]), ("doze-suspend", False))
        self.assertEqual(state["width"], 800)
        # Never borrow an external display's geometry or the physical panel's rotation.
        self.assertIsNone(screen.parse_screen_state("", DISPLAY.split("mDisplayId=1")[1], "")["width"])

    def test_explicit_interactive_state_precedes_wakefulness(self):
        state = screen.parse_screen_state("mWakefulness=Awake\nmInteractive=false", DISPLAY, "free")
        self.assertFalse(state["interactive"])

    def test_override_power_can_be_stale_while_geometry_is_current(self):
        display = DISPLAY.replace("rotation 1, state ON", "rotation 1, state OFF")
        state = screen.parse_screen_state("mWakefulness=Awake", display, "free")
        self.assertEqual(state["display_state"], "on")
        self.assertEqual((state["width"], state["height"]), (2520, 1080))

    def test_status_partial_and_total_failure(self):
        adb = FakeAdb()
        adb.values[("wm", "user-rotation")] = core.ControlError("unsupported")
        result = screen.screen_status(adb, "phone")
        self.assertTrue(result["interactive"])
        self.assertEqual(result["rotation_mode"], "unknown")
        self.assertEqual(len(result["errors"]), 1)
        adb.values = {key: core.ControlError("offline") for key in adb.values}
        with self.assertRaisesRegex(core.ControlError, "cannot read"):
            screen.screen_status(adb, "phone")

    def test_dedicated_power_keys_and_repeated_state_requests(self):
        adb = FakeAdb()
        for _ in range(2):
            result = screen.control_screen(adb, "phone", "set-screen", state="awake")
            self.assertTrue(result["verified"])
            self.assertEqual((result["action"], result["requested_state"], result["interactive"]),
                             ("set-screen", "awake", True))
        self.assertEqual(adb.commands.count(["shell", "input", "keyevent", "KEYCODE_WAKEUP"]), 2)
        adb.values[("dumpsys", "power")] = "mWakefulness=Asleep\nmInteractive=false"
        for _ in range(2):
            result = screen.control_screen(adb, "phone", "set-screen", state="asleep")
            self.assertTrue(result["verified"])
            self.assertEqual((result["requested_state"], result["interactive"]), ("asleep", False))
        self.assertEqual(adb.commands.count(["shell", "input", "keyevent", "KEYCODE_SLEEP"]), 2)

    def test_power_verifies_transition_and_rejects_false_success(self):
        adb = FakeAdb()
        with patch("scripts.android_screen.screen_status", side_effect=[
            {"interactive": False}, {"interactive": True}
        ]), patch("scripts.android_screen.time.sleep"):
            self.assertTrue(screen.control_screen(adb, "phone", "set-screen", state="awake")["verified"])
        adb.timeout = 0.001
        with patch("scripts.android_screen.screen_status", return_value={"interactive": None}):
            with self.assertRaisesRegex(core.ControlError, "not verified"):
                screen.control_screen(adb, "phone", "set-screen", state="awake")

    def test_invalid_screen_state_is_rejected_before_injection(self):
        for state in (None, "present", "gone", "enabled", "on"):
            adb = FakeAdb()
            with self.subTest(state=state), self.assertRaises(core.ControlError):
                screen.control_screen(adb, "phone", "set-screen", state=state)
            self.assertEqual(adb.commands, [])

    def test_rotation_commands_report_requested_and_observed(self):
        adb = FakeAdb(rotation="lock 1")
        result = screen.control_screen(adb, "phone", "rotate-screen", "90")
        self.assertTrue(result["setting_applied"])
        self.assertTrue(result["rotation_matches"])
        self.assertIn(["shell", "wm", "user-rotation", "lock", "1"], adb.commands)
        adb.values[("wm", "user-rotation")] = "free"
        result = screen.control_screen(adb, "phone", "rotate-screen", "auto")
        self.assertIsNone(result["rotation_matches"])
        self.assertIn(["shell", "wm", "user-rotation", "free"], adb.commands)

    def test_rotation_app_constraint_and_rejected_setting(self):
        adb = FakeAdb(rotation="lock 0")
        adb.timeout = 0.001
        result = screen.control_screen(adb, "phone", "rotate-screen", "0")
        self.assertTrue(result["setting_applied"])
        self.assertFalse(result["rotation_matches"])
        adb.values[("wm", "user-rotation")] = "free"
        with self.assertRaisesRegex(core.ControlError, "not verified"):
            screen.control_screen(adb, "phone", "rotate-screen", "0")
        adb.values[("wm", "user-rotation", "lock", "0")] = "Error: denied"
        with self.assertRaisesRegex(core.ControlError, "denied"):
            screen.control_screen(adb, "phone", "rotate-screen", "0")

    def test_verification_deadline_preserves_last_confirmed_rotation_setting(self):
        adb = FakeAdb()
        observed = {"rotation_mode": "locked", "locked_rotation": 90, "rotation": 0}
        with patch("scripts.android_screen.screen_status",
                   side_effect=[observed, core.ControlError("deadline")]), \
             patch("scripts.android_screen.time.sleep"):
            result = screen.control_screen(adb, "phone", "rotate-screen", "90")
        self.assertTrue(result["setting_applied"])
        self.assertFalse(result["rotation_matches"])

    def test_cli_routing_and_rotation_validation(self):
        for action, values in (("screen-status", {}), ("set-screen", {"state": "awake"}),
                               ("set-screen", {"state": "asleep"}), ("rotate-screen", {"rotation": "90"})):
            args = core.action_args(action, target="phone", **values)
            core.prepare_action(args)
            with patch("scripts.android_screen.screen_status", return_value={"target": "phone"}) as status, \
                 patch("scripts.android_screen.control_screen", return_value={"action": action}) as control:
                core.device_action(args, FakeAdb())
                if action == "screen-status":
                    status.assert_called_once_with(ANY, "phone")
                    control.assert_not_called()
                else:
                    control.assert_called_once_with(ANY, "phone", action,
                                                    values.get("rotation"), state=values.get("state"))
                    status.assert_not_called()
        with self.assertRaises(core.ControlError):
            core.prepare_action(core.action_args("rotate-screen"))
        with self.assertRaises(core.ControlError):
            core.prepare_action(core.action_args("set-screen", state="awake", rotation="90"))
        for state in (None, "present", "gone", "enabled"):
            with self.subTest(state=state), self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args("set-screen", state=state))

    def test_forwarded_set_screen_preserves_explicit_state(self):
        for state in ("awake", "asleep"):
            args = core.build_parser().parse_args([
                "--action", "set-screen", "--state", state, "--target", "phone",
            ])
            response = io.BytesIO(json.dumps({
                "ok": True, "result": {"structuredContent": {
                    "action": "set-screen", "requested_state": state, "verified": True,
                }},
            }).encode())
            with patch("scripts.android_client.urlrequest.urlopen", return_value=response) as request:
                result = client.execute_via_server(args, "http://127.0.0.1:8000")
            self.assertEqual(result["requested_state"], state)
            self.assertEqual(json.loads(request.call_args.args[0].data), {
                "name": "set_screen", "arguments": {"state": state, "target": "phone"},
            })


if __name__ == "__main__":
    unittest.main()
