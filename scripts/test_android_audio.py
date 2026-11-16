"""Audio control tests: real dump shapes, device ranges, and verified mutations."""
import io
import json
import unittest
from unittest.mock import patch

from scripts import android_audio as audio
from scripts import android_client as client
from scripts import android_core as core


HELP = "Audio manager commands:\n" + "\n".join("  " + name + " ARGUMENTS" for name in audio.CONTROL_COMMANDS)


def dump(level=4, muted=False, ringer="NORMAL"):
    return f"""Events log: volume changes
11-15 20:00:00 STREAM_MUSIC something old
- STREAM_MUSIC:
   Muted: {str(muted).lower()}
   Muted Internally: false
   Min: 0
   Max: 30
   streamVolume:{level}
   Current: 2 (speaker): {level}, 80 (bt_a2dp): 9
   Devices: speaker(2)
   Volume Group: AUDIO_STREAM_MUSIC

- STREAM_RING:
   Muted: false
   Devices: speaker(2)

- STREAM_ALARM:
   Muted: {str(muted).lower()}
   Devices: speaker(2)

Ringer mode:
- mode (internal) = {ringer}
- mode (external) = {ringer}
- zen mode:ZEN_MODE_OFF
Other state:
  mic mute FromSwitch=false FromRestrictions=false FromApi=false from system=false
"""


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeAdb:
    timeout = 0.6

    def __init__(self, level=4, minimum=0, maximum=30, muted=False,
                 ignore=False, commands=HELP):
        self.level, self.minimum, self.maximum = level, minimum, maximum
        self.muted, self.ignore, self.help = muted, ignore, commands
        self.ringer = "NORMAL"
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args == ["devices", "-l"]:
            return b"List of devices attached\nphone device\n"
        if args == ["shell", "cmd audio 2>&1 || true"]:
            return self.help.encode()
        if args == ["shell", "cmd notification 2>&1 || true"]:
            return b"  set_dnd [off|priority|alarms|none]\n"
        if args == ["shell", "dumpsys", "audio"]:
            return dump(self.level, self.muted, self.ringer).encode()
        if args[:4] == ["shell", "cmd", "media_session", "volume"]:
            assert args[-1] == "--get", "media_session must never be used for mutations"
            effective = 0 if self.muted else self.level
            return f"[V] volume is {effective} in range [{self.minimum}..{self.maximum}]\n".encode()
        if args[:3] == ["shell", "cmd", "audio"]:
            if self.ignore:
                return b""
            command = args[3]
            if command == "set-volume":
                self.level = int(args[5])
                self.muted = self.level == 0
            elif command == "adj-volume":
                delta = 1 if args[5] == "RAISE" else -1
                self.level = max(self.minimum, min(self.maximum, self.level + delta))
                self.muted = self.level == 0
            elif command in {"adj-mute", "adj-unmute"}:
                self.muted = command == "adj-mute"
            elif command == "set-ringer-mode":
                self.ringer = args[4]
            else:
                raise AssertionError(args)
            return b""
        if args[:3] == ["shell", "input", "keyevent"]:
            return b""
        raise AssertionError(args)

    def mutations(self):
        return [args for args, _ in self.calls if args[:3] == ["shell", "cmd", "audio"]]


class AndroidAudioTests(unittest.TestCase):
    def test_get_preserves_full_status_and_filters_only_streams(self):
        result = audio.get_volume(FakeAdb(), "phone")
        self.assertEqual(set(result["streams"]), set(core.AUDIO_STREAMS))
        self.assertEqual(set(result), {"target", "streams", "ringer_mode", "ringer_mode_external",
                                       "zen_mode", "microphone_muted", "supported_commands", "errors"})
        result = audio.get_volume(FakeAdb(level=9, muted=True), "phone", "media")
        self.assertEqual(set(result["streams"]), {"media"})
        self.assertEqual(result["streams"]["media"]["level"], 0)
        self.assertTrue(result["streams"]["media"]["muted"])
        self.assertEqual(result["streams"]["media"]["max"], 30)
        self.assertEqual(result["ringer_mode"], "normal")
        self.assertEqual(result["zen_mode"], "ZEN_MODE_OFF")
        self.assertFalse(result["microphone_muted"])
        self.assertEqual(result["supported_commands"], [*audio.CONTROL_COMMANDS, "set_dnd"])
        self.assertEqual(result["errors"], [])

    def test_dump_parser_ignores_event_logs_and_other_device_indices(self):
        state = audio.parse_audio_dump(dump())
        self.assertEqual(state["streams"]["media"]["device"], "speaker(2)")
        self.assertFalse(state["streams"]["media"]["muted_internally"])
        self.assertIsNone(state["streams"]["notification"]["muted"])
        state = audio.parse_audio_dump("Events log\nSTREAM_MUSIC Muted: true\n")
        self.assertIsNone(state["streams"]["media"]["muted"])
        self.assertIsNone(state["ringer_mode"])

    def test_partial_read_failures_are_explicit(self):
        adb = FakeAdb()
        original = adb.run
        def run(args, **kwargs):
            if args[:3] == ["shell", "cmd", "media_session"]:
                return b"unknown command"
            return original(args, **kwargs)
        adb.run = run
        result = audio.get_volume(adb, "phone", "media")
        self.assertIsNone(result["streams"]["media"]["level"])
        self.assertTrue(result["errors"])
        adb.run = lambda *args, **kwargs: b""
        with self.assertRaisesRegex(core.ControlError, "cannot read audio state"):
            audio.get_volume(adb, "phone")

    def test_default_media_set_returns_full_status_with_mute_state(self):
        adb = FakeAdb()
        result = audio.set_volume(adb, "phone", level=0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["requested"], {"stream": "media", "level": 0})
        self.assertEqual(result["streams"]["media"]["level"], 0)
        self.assertTrue(result["streams"]["media"]["muted"])
        self.assertEqual(result["ringer_mode_external"], "normal")
        self.assertEqual(adb.mutations(), [["shell", "cmd", "audio", "set-volume", "3", "0"]])

    def test_device_ranges_are_checked_before_any_mutation(self):
        for minimum, maximum, level in [(1, 8, 0), (0, 7, 8), (0, 30, 31)]:
            adb = FakeAdb(minimum=minimum, maximum=maximum)
            with self.subTest(level=level), self.assertRaisesRegex(core.ControlError, "must be between"):
                audio.set_volume(adb, "phone", stream="alarm", level=level, ringer_mode="vibrate")
            self.assertEqual(adb.mutations(), [])

    def test_muted_stream_can_report_zero_below_settable_minimum(self):
        adb = FakeAdb(level=4, minimum=1, maximum=8, muted=True)
        self.assertEqual(audio.read_volume(adb, "phone", "alarm"), {"level": 0, "min": 1, "max": 8})
        result = audio.set_volume(adb, "phone", stream="alarm", muted=False)
        self.assertFalse(result["streams"]["alarm"]["muted"])

    def test_combined_level_then_mute_retains_verified_level_for_unmute(self):
        adb = FakeAdb()
        result = audio.set_volume(adb, "phone", level=5, muted=True)
        self.assertEqual(result["streams"]["media"]["level"], 0)
        self.assertTrue(result["streams"]["media"]["muted"])
        self.assertEqual(adb.level, 5)
        self.assertEqual([command[3] for command in adb.mutations()], ["set-volume", "adj-mute"])
        result = audio.set_volume(adb, "phone", muted=False)
        self.assertFalse(result["streams"]["media"]["muted"])
        self.assertEqual(result["streams"]["media"]["level"], 5)

    def test_adjust_verifies_direction_and_range_boundaries(self):
        for level, direction, expected in [(4, "raise", 5), (4, "lower", 3),
                                            (30, "raise", 30), (0, "lower", 0)]:
            adb = FakeAdb(level=level)
            result = audio.set_volume(adb, "phone", direction=direction)
            self.assertTrue(result["verified"])
            self.assertEqual(result["streams"]["media"]["level"], expected)
            self.assertEqual(len(adb.mutations()), 1)

    def test_ringer_only_request_leaves_media_untouched_and_returns_all_streams(self):
        adb = FakeAdb(level=7, muted=True)
        for mode in ["vibrate", "silent", "normal"]:
            result = audio.set_volume(adb, "phone", ringer_mode=mode)
            self.assertEqual(result["requested"], {"ringer_mode": mode})
            self.assertEqual(result["ringer_mode"], mode)
            self.assertEqual(set(result["streams"]), set(core.AUDIO_STREAMS))
            self.assertEqual((adb.level, adb.muted), (7, True))
        self.assertTrue(all(command[3] == "set-ringer-mode" for command in adb.mutations()))

    def test_invalid_requests_and_missing_commands_do_not_partially_apply(self):
        for values in [{}, {"level": 5, "direction": "raise"}, {"level": -1},
                       {"muted": "false"}, {"ringer_mode": "unknown"}, {"direction": "up"}]:
            adb = FakeAdb()
            with self.subTest(values=values), self.assertRaises(core.ControlError):
                audio.set_volume(adb, "phone", **values)
            self.assertEqual(adb.calls, [])
        adb = FakeAdb(commands="  set-volume STREAM LEVEL\n  set-ringer-mode MODE\n")
        with self.assertRaisesRegex(core.ControlError, "does not advertise"):
            audio.set_volume(adb, "phone", level=5, muted=True, ringer_mode="vibrate")
        self.assertEqual(adb.mutations(), [])

    def test_success_exit_without_state_change_fails_without_resending(self):
        for values in [{"level": 5}, {"direction": "raise"}, {"muted": True}, {"ringer_mode": "vibrate"}]:
            adb = FakeAdb(ignore=True)
            with self.subTest(values=values), patch("scripts.android_audio.time", Clock()), \
                 self.assertRaisesRegex(core.ControlError, "sent but not verified"):
                audio.set_volume(adb, "phone", **values)
            self.assertEqual(len(adb.mutations()), 1)

    def test_later_failure_identifies_already_applied_step(self):
        adb = FakeAdb()
        original = adb.run
        def run(args, **kwargs):
            if args[:4] == ["shell", "cmd", "audio", "adj-mute"]:
                adb.calls.append((args, kwargs))
                return b""
            return original(args, **kwargs)
        adb.run = run
        with patch("scripts.android_audio.time", Clock()), \
             self.assertRaisesRegex(core.ControlError, "failed at muted; verified steps: level"):
            audio.set_volume(adb, "phone", level=5, muted=True)
        self.assertEqual(adb.level, 5)
        self.assertFalse(adb.muted)
        self.assertEqual(len(adb.mutations()), 2)

    def test_final_readback_detects_linked_setting_conflict(self):
        adb = FakeAdb()
        original = adb.run
        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[:4] == ["shell", "cmd", "audio", "set-volume"]:
                adb.ringer = "NORMAL"
            return result
        adb.run = run
        with self.assertRaisesRegex(core.ControlError, "final state was not verified for ringer_mode"):
            audio.set_volume(adb, "phone", level=5, ringer_mode="silent")

    def test_delayed_readback_recovers_and_late_readback_fails(self):
        adb = FakeAdb()
        real_read = audio.read_volume
        reads = 0
        def delayed(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 3:
                raise core.ControlError("temporary read failure")
            return real_read(*args, **kwargs)
        with patch("scripts.android_audio.time", Clock()), patch("scripts.android_audio.read_volume", side_effect=delayed):
            self.assertTrue(audio.set_volume(adb, "phone", level=5)["verified"])
        self.assertEqual(len(adb.mutations()), 1)
        clock = Clock()
        reads = 0
        def late(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads > 2:
                clock.now = 100
            return real_read(*args, **kwargs)
        with patch("scripts.android_audio.time", clock), patch("scripts.android_audio.read_volume", side_effect=late), \
             self.assertRaisesRegex(core.ControlError, "sent but not verified"):
            audio.set_volume(FakeAdb(), "phone", level=5)

    def test_cli_and_direct_defaults_validation_and_forwarded_false(self):
        for muted, stream in ((True, None), (False, None), ("true", "media"), ("false", "media")):
            with self.subTest(direct_muted=muted, stream=stream):
                args = core.action_args("set-volume", target="phone", stream=stream)
                args.muted = muted
                expected = muted is True or muted == "true"
                adb = FakeAdb(muted=not expected)
                # No prepare_action/execute call: direct dispatch must normalize too.
                result = core.device_action(args, adb)
                self.assertTrue(result["verified"])
                self.assertIs(args.muted, expected)
                self.assertEqual(args.stream, "media")
                self.assertIs(result["streams"]["media"]["muted"], expected)
                self.assertEqual(adb.mutations(), [["shell", "cmd", "audio",
                                                  "adj-mute" if expected else "adj-unmute", "3"]])
        for invalid in ("invalid", 0, 1):
            with self.subTest(direct_muted=invalid):
                args = core.action_args("set-volume", target="phone")
                args.muted = invalid
                adb = FakeAdb()
                with self.assertRaisesRegex(core.ControlError, "muted must be true or false"):
                    core.device_action(args, adb)
                self.assertEqual(adb.calls, [])
        args = core.action_args("set-volume", muted=False, target="phone")
        core.prepare_action(args)
        self.assertEqual(args.stream, "media")
        self.assertIs(args.muted, False)
        core.prepare_action(args)
        self.assertIs(args.muted, False)
        for action, values in [("set-volume", {}), ("set-volume", {"level": 5, "direction": "raise"}),
                               ("get-volume", {"level": 1}), ("status", {"stream": "media"})]:
            with self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args(action, **values))
        adb = FakeAdb(muted=True)
        self.assertFalse(core.device_action(args, adb)["streams"]["media"]["muted"])
        response = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": {"verified": True}}}).encode())
        with patch("scripts.android_client.urlrequest.urlopen", return_value=response) as request:
            client.execute_via_server(args, "http://127.0.0.1:8000")
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent, {"name": "set_volume", "arguments": {"stream": "media", "muted": False, "target": "phone"}})
        self.assertGreater(request.call_args.kwargs["timeout"], args.timeout)


if __name__ == "__main__":
    unittest.main()
