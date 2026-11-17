#!/usr/bin/env python3
"""Offline unit tests for android_control.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from contextlib import chdir, redirect_stderr
from pathlib import Path
from unittest.mock import patch


from scripts import android_control as android
from scripts.test_support import python_command


MODULE_PATH = Path(__file__).with_name("android_control.py")
DISPLAY = b'mDisplayId=0\nmOverrideDisplayInfo=DisplayInfo{real 1080 x 1920, rotation 0, state ON}\n'


class AndroidControlTests(unittest.TestCase):
    def setUp(self):
        android._package_cache.clear()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "state.json"
        self.server_path = Path(self.temporary_directory.name) / "mcp-server.json"
        self.state_override = patch.dict(
            os.environ, {
                android.STATE_FILE_ENV: str(self.state_path),
                android.SERVER_FILE_ENV: str(self.server_path),
            }, clear=False
        )
        self.state_override.start()

    def tearDown(self):
        self.state_override.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def fake_adb(*targets):
        class FakeAdb:
            def run(self, args, **_kwargs):
                if args != ["devices", "-l"]:
                    raise AssertionError(args)
                lines = ["List of devices attached"]
                lines.extend(f"{target} {state}" for target, state in targets)
                return ("\n".join(lines) + "\n").encode()
        return FakeAdb()

    def test_shell_quote(self):
        self.assertEqual(android.shell_quote("it's"), "'it'\\''s'")
        self.assertEqual(android.shell_quote("a&b"), "'a&b'")

    def test_multi_touch_validation_and_device_command(self):
        fingers = [
            {"x1": 100, "y1": 200, "x2": 50, "y2": 200},
            {"x1": 300, "y1": 200, "x2": 350, "y2": 200},
        ]
        args = android.action_args("multi-touch", fingers=fingers, target="device-a")
        adb = self.fake_adb(("device-a", "device"))
        commands = []
        cached = set()
        legacy_present = True
        fail_push = False
        original = adb.run

        def run(command, **kwargs):
            nonlocal legacy_present
            if command == ["devices", "-l"]:
                return original(command, **kwargs)
            commands.append((command, kwargs))
            if command == ["shell", "dumpsys", "display"]:
                return DISPLAY
            if command[0] == "shell" and command[1].startswith("[ -s "):
                return b"present" if command[1].split()[2] in cached else b""
            if command[0] == "push":
                if fail_push:
                    raise android.ControlError("upload failed")
                cached.add(command[2])
            if command == ["shell", "rm", "-f", "/data/local/tmp/android-control-multitouch.dex"]:
                self.assertTrue(cached)
                legacy_present = False
            return b""
        adb.run = run
        result = android.device_action(args, adb)
        self.assertEqual(args.duration, 300)
        self.assertEqual(args.fingers, fingers)
        self.assertEqual(result["fingers"], fingers)
        pushes = [command for command, _ in commands if command[0] == "push"]
        self.assertEqual(len(pushes), 1)
        remote = pushes[0][2]
        cleanup = ["shell", "rm", "-f", "/data/local/tmp/android-control-multitouch.dex"]
        self.assertGreater([command for command, _ in commands].index(cleanup),
                           [command for command, _ in commands].index(pushes[0]))
        self.assertFalse(legacy_present)
        self.assertEqual(commands[-1][0], [
            "shell", f"CLASSPATH={remote}",
            "app_process", "/", "MultiTouch", "300",
            "100", "200", "50", "200", "300", "200", "350", "200",
        ])
        self.assertTrue(all(kwargs["serial"] == "device-a" for _, kwargs in commands))
        legacy_present = True
        android.device_action(args, adb)
        self.assertFalse(legacy_present)
        self.assertEqual(sum(command[0] == "push" for command, _ in commands), 1)
        helper_root = Path(self.temporary_directory.name)
        (helper_root / "multitouch").mkdir()
        (helper_root / "multitouch" / "multitouch.dex").write_bytes(b"rebuilt helper")
        with patch("scripts.android_core.__file__", str(helper_root / "android_core.py")):
            android.device_action(args, adb)
        self.assertEqual(len(cached), 2)
        args.fingers[0]["x1"] = 5000
        with self.assertRaisesRegex(android.ControlError, "exceed.*display"):
            android.device_action(args, adb)
        self.assertEqual(sum(command[0] == "push" for command, _ in commands), 2)
        # Preserve the legacy helper when the replacement could not be uploaded.
        args.fingers[0]["x1"] = 100
        (helper_root / "multitouch" / "multitouch.dex").write_bytes(b"another rebuild")
        fail_push = True
        legacy_present = True
        before = len(commands)
        with patch("scripts.android_core.__file__", str(helper_root / "android_core.py")), \
             self.assertRaisesRegex(android.ControlError, "upload failed"):
            android.device_action(args, adb)
        self.assertTrue(legacy_present)
        self.assertNotIn(cleanup, [command for command, _ in commands[before:]])

    def test_multi_touch_rejects_invalid_paths(self):
        valid = {"x1": 0, "y1": 0, "x2": 100, "y2": 100}
        for fingers in ("no json", [], [valid], [valid] * 11,
                        [valid, {"x1": 0}], [valid, dict(valid, x1=-1)],
                        [valid, dict(valid, x1=True)], [valid, dict(valid, x1=1.5)],
                        [valid, dict(valid, x1=65536)]):
            with self.subTest(fingers=fingers), self.assertRaises(android.ControlError):
                android.prepare_action(android.action_args("multi-touch", fingers=fingers))
        for duration in (0, 3001):
            with self.assertRaises(android.ControlError):
                android.prepare_action(android.action_args("multi-touch", fingers=[valid, valid], duration=duration))

    def test_zoom_distance_and_reverse_paths(self):
        inward = android.zoom_paths("in", 500, 900, 150, 1080, 1920)
        self.assertEqual(inward, [
            {"x1": 400, "y1": 900, "x2": 250, "y2": 900},
            {"x1": 600, "y1": 900, "x2": 750, "y2": 900},
        ])
        outward = android.zoom_paths("out", 500, 900, 150, 1080, 1920)
        for start, reverse in zip(inward, outward):
            self.assertEqual(start["x1"], reverse["x2"])
            self.assertEqual(start["x2"], reverse["x1"])
        for direction in ("in", "out"):
            args = android.action_args("zoom", direction=direction, x=500, y=900)
            android.prepare_action(args)
            self.assertEqual((args.distance, args.duration), (150, 500))
        for x, y, distance in ((249, 900, 150), (830, 900, 150), (500, 1920, 150), (500, 900, 500)):
            with self.assertRaises(android.ControlError):
                android.zoom_paths("in", x, y, distance, 1080, 1920)

    def test_zoom_validation_and_bounds_before_injection(self):
        for values in ({"distance": 0}, {"distance": -1}, {"duration": 0},
                       {"duration": 3001}, {"x": 65536}, {"y": -1},
                       {"direction": None}, {"direction": "raise"}, {"direction": "lower"}):
            with self.subTest(values=values), self.assertRaises(android.ControlError):
                args = android.action_args("zoom", **({"x": 500, "y": 900, "direction": "in"} | values))
                android.prepare_action(args)
        args = android.action_args("zoom", direction="in", x=500, y=900, target="device-a")
        adb = self.fake_adb(("device-a", "device"))
        with patch("scripts.android_core.logical_display_size", return_value=(1080, 1920)), \
             patch("scripts.android_core.inject_multi_touch") as inject:
            result = android.device_action(args, adb)
            inject.assert_called_once_with(adb, "device-a", result["fingers"], 500)
            self.assertEqual(result["action"], "zoom")
            self.assertEqual(result["direction"], "in")
        args.direction = "out"
        with patch("scripts.android_core.logical_display_size", return_value=(1080, 1920)), \
             patch("scripts.android_core.inject_multi_touch") as inject:
            result = android.device_action(args, adb)
            self.assertEqual(result["direction"], "out")
            self.assertEqual(result["fingers"], android.zoom_paths("out", 500, 900, 150, 1080, 1920))
            inject.assert_called_once_with(adb, "device-a", result["fingers"], 500)
        args.x = 50
        with patch("scripts.android_core.logical_display_size", return_value=(1080, 1920)), \
             patch("scripts.android_core.inject_multi_touch") as inject:
            with self.assertRaises(android.ControlError):
                android.device_action(args, adb)
            inject.assert_not_called()
        # Logical geometry follows an override/rotation, without calling screencap.
        with patch.object(adb, "run", return_value=DISPLAY.replace(b"1080 x 1920", b"1920 x 1080")) as run:
            self.assertEqual(android.logical_display_size(adb, "device-a"), (1920, 1080))
            self.assertEqual(run.call_args.args[0], ["shell", "dumpsys", "display"])

    def test_zoom_forwarding_and_direction_validation(self):
        for direction in ("in", "out"):
            args = android.build_parser().parse_args([
                "--action", "zoom", "--direction", direction, "--x", "500", "--y", "900",
                "--target", "device-a",
            ])
            response = io.BytesIO(json.dumps({
                "ok": True, "result": {"structuredContent": {"action": "zoom", "direction": direction}},
            }).encode())
            with patch.object(android.urlrequest, "urlopen", return_value=response) as request:
                result = android.execute_via_server(args, "http://127.0.0.1:8000")
            self.assertEqual(result["direction"], direction)
            self.assertEqual(json.loads(request.call_args.args[0].data), {
                "name": "zoom", "arguments": {"x": 500, "y": 900, "direction": direction,
                                               "distance": 150, "duration": 500, "target": "device-a"},
            })
            with self.assertRaises(android.ControlError):
                android.prepare_action(android.action_args("set-volume", direction=direction))

    def test_volume_and_playback_keys_dispatch_without_claiming_verification(self):
        for key, code in [("volume-up", 24), ("volume-down", 25), ("volume-mute", 164),
                          ("media-play", 126), ("media-pause", 127), ("media-play-pause", 85),
                          ("media-next", 87), ("media-previous", 88)]:
            adb = self.fake_adb(("phone", "device"))
            commands = []
            original = adb.run

            def run(args, **kwargs):
                if args[0] == "shell":
                    commands.append((args, kwargs))
                    return b""
                return original(args, **kwargs)

            adb.run = run
            args = android.action_args("press-key", key=key, target="phone")
            android.prepare_action(args)
            result = android.device_action(args, adb)
            self.assertNotIn("verified", result)
            self.assertEqual(commands, [(["shell", "input", "keyevent", str(code)],
                                         {"serial": "phone", "timeout": None})])

    def test_direct_cli_entrypoint_loads_split_modules(self):
        result = subprocess.run(
            python_command(MODULE_PATH, "--help"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--action", result.stdout)
        self.assertIn("--mcp", result.stdout)

    def test_parse_devices(self):
        output = (
            "List of devices attached\n"
            "127.0.0.1:5555 device product:p model:Pixel_8 device:shiba transport_id:1\n"
        )
        result = android.parse_devices(output)
        self.assertEqual(result[0]["target"], "127.0.0.1:5555")
        self.assertNotIn("serial", result[0])
        self.assertEqual(result[0]["model"], "Pixel_8")

    def test_parse_packages(self):
        output = "package:com.android.settings\nnoise\npackage:com.example.transit\n"
        self.assertEqual(android.parse_packages(output), [
            "com.android.settings", "com.example.transit",
        ])

    def test_parse_current_focus_prefers_current_window(self):
        output = (
            "  mFocusedApp=ActivityRecord{123 u0 old.example/.OldActivity t1}\n"
            "  mCurrentFocus=Window{456 u0 com.example.transit/.MainActivity}\n"
        )
        self.assertEqual(android.parse_current_focus(output), {
            "component": "com.example.transit/.MainActivity",
            "package": "com.example.transit",
            "activity": "com.example.transit.MainActivity",
            "source": "mCurrentFocus",
        })

    def test_parse_current_focus_falls_back_to_focused_app(self):
        output = "mCurrentFocus=null\nmFocusedApp=ActivityRecord{abc u0 com.example/com.example.Home t2}\n"
        focused = android.parse_current_focus(output)
        self.assertEqual(focused["package"], "com.example")
        self.assertEqual(focused["activity"], "com.example.Home")
        self.assertEqual(focused["source"], "mFocusedApp")

    def test_parse_ui_xml_and_entities(self):
        xml = (
            '<hierarchy><node text="A &amp; B > C" content-desc="" '
            'class="android.widget.TextView" package="example.app" '
            'clickable="true" bounds="[0,10][100,50]"/></hierarchy>'
        )
        elements = android.parse_ui_xml(xml)
        self.assertEqual(elements[0]["label"], "A & B > C")
        self.assertEqual(elements[0]["center"], [50, 30])
        quoted = '''<hierarchy><node text='say "hi" >x' content-desc="it's &amp; fine"
                    bounds='[0,0][100,40]'/></hierarchy>'''
        elements = android.parse_ui_xml(quoted)
        self.assertEqual(elements[0]["text"], 'say "hi" >x')
        self.assertEqual(elements[0]["description"], "it's & fine")

    def test_parse_ui_xml_preserves_accessibility_state(self):
        xml = (
            '<hierarchy>'
            '<node text="" content-desc="More" class="android.widget.Button" '
            'clickable="true" selected="false" scrollable="false" enabled="true" '
            'checked="false" focusable="true" bounds="[0,0][40,40]"/>'
            '<node text="" content-desc="Transit Pass&#10;Tab 1 of 2" '
            'class="android.view.View" clickable="true" selected="true" '
            'scrollable="false" enabled="true" checked="true" focusable="true" '
            'bounds="[0,40][100,80]"/>'
            '<node text="" content-desc="" class="androidx.recyclerview.widget.RecyclerView" '
            'clickable="false" selected="false" scrollable="true" enabled="true" '
            'checked="false" focusable="false" bounds="[0,80][100,300]"/>'
            '</hierarchy>'
        )

        elements = android.parse_ui_xml(xml)

        self.assertEqual([item["label"] for item in elements], [
            "More", "Transit Pass\nTab 1 of 2", "RecyclerView",
        ])
        self.assertEqual(elements[1]["description"], "Transit Pass\nTab 1 of 2")
        self.assertEqual(elements[1]["states"], [
            "clickable", "selected", "enabled", "checked", "focusable",
        ])
        self.assertEqual(elements[2]["states"], ["scrollable", "enabled"])
        self.assertNotIn("content_description", elements[1])
        self.assertNotIn("clickable", elements[1])

    def test_describe_ui_supports_json_and_original_xml(self):
        xml = '<hierarchy><node text="OK" clickable="true" bounds="[0,0][10,20]"/></hierarchy>'
        parsed = android.describe_ui("device-1", xml, "json")
        self.assertEqual(parsed["target"], "device-1")
        self.assertNotIn("serial", parsed)
        self.assertEqual(parsed["format"], "json")
        self.assertEqual(parsed["elements"][0]["center"], [5, 10])

        original = android.describe_ui("device-1", xml, "original")
        self.assertEqual(original, {
            "target": "device-1", "format": "original", "xml": xml,
        })
        self.assertEqual(
            android.description_elements(original)[0]["label"], "OK",
        )
        self.assertEqual(
            android.description_elements(parsed), parsed["elements"],
        )

    def test_truncated_xml_keeps_complete_nodes(self):
        xml = (
            '<node text="OK" clickable="true" bounds="[0,0][10,10]"/>'
            '<node text="broken" clickable="true" bounds="[0,0]'
        )
        self.assertEqual([e["label"] for e in android.parse_ui_xml(xml)], ["OK"])

    def test_named_argument_schema(self):
        args = android.build_parser().parse_args([
            "--action", "tap", "--target", "device-1", "--x", "1", "--y", "2"
        ])
        self.assertEqual(args.target, "device-1")
        android.validate_action_args(args)
        bad = android.build_parser().parse_args(["--action", "status", "--x", "1"])
        with self.assertRaises(android.ControlError):
            android.validate_action_args(bad)
        describe = android.build_parser().parse_args([
            "--action", "describe-screen", "--format", "original",
        ])
        android.validate_action_args(describe)
        force_stop = android.build_parser().parse_args([
            "--action", "force-stop-app", "--name", "com.example.transit",
        ])
        android.validate_action_args(force_stop)
        launch_restart = android.build_parser().parse_args([
            "--action", "launch-app", "--name", "com.example.transit",
            "--force-restart",
        ])
        self.assertTrue(launch_restart.force_restart)
        android.validate_action_args(launch_restart)
        wrong_format_action = android.build_parser().parse_args([
            "--action", "status", "--format", "json",
        ])
        with self.assertRaises(android.ControlError):
            android.validate_action_args(wrong_format_action)
        wrong_restart_action = android.build_parser().parse_args([
            "--action", "status", "--force-restart",
        ])
        with self.assertRaises(android.ControlError):
            android.validate_action_args(wrong_restart_action)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            android.build_parser().parse_args(["--action", "status", "--serial", "device-1"])

    def test_press_key_supports_interactive_keyboard_keys(self):
        calls = []

        class FakeAdb:
            def run(self, args, **kwargs):
                calls.append((args, kwargs))
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                if args == ["shell", "input", "keyevent", "67"]:
                    return b""
                raise AssertionError(args)

        result = android.device_action(
            android.action_args("press-key", key="delete", target="device-1"),
            FakeAdb(),
        )

        self.assertEqual(result["key"], "delete")
        self.assertEqual(calls[-1][0], ["shell", "input", "keyevent", "67"])

    def test_navigation_uses_press_key_for_dispatch_and_forwarding(self):
        for key, code in (("home", 3), ("back", 4)):
            with self.subTest(key=key):
                args = android.build_parser().parse_args([
                    "--action", "press-key", "--key", key, "--target", "device-1",
                ])
                android.prepare_action(args)
                adb = self.fake_adb(("device-1", "device"))
                original = adb.run

                def dispatch(command, **kwargs):
                    if command == ["shell", "input", "keyevent", str(code)]:
                        return b""
                    return original(command, **kwargs)

                with patch.object(adb, "run", side_effect=dispatch) as run:
                    result = android.device_action(args, adb)
                self.assertEqual(result, {"target": "device-1", "action": "press-key", "key": key})
                self.assertEqual(run.call_args.args[0], ["shell", "input", "keyevent", str(code)])
                response = io.BytesIO(json.dumps({
                    "ok": True, "result": {"structuredContent": result},
                }).encode())
                with patch.object(android.urlrequest, "urlopen", return_value=response) as request:
                    self.assertEqual(android.execute_via_server(args, "http://127.0.0.1:8000"), result)
                self.assertEqual(json.loads(request.call_args.args[0].data), {
                    "name": "press_key", "arguments": {"key": key, "target": "device-1"},
                })
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    android.build_parser().parse_args(["--action", key])

    def test_force_stop_resolves_and_uses_argument_array(self):
        calls = []

        class FakeAdb:
            def run(self, args, **kwargs):
                calls.append((args, kwargs))
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                if args == ["shell", "pm", "list", "packages"]:
                    return b"package:com.example.transit\n"
                if args == ["shell", "am", "force-stop", "com.example.transit"]:
                    return b""
                raise AssertionError(args)

        args = android.build_parser().parse_args([
            "--action", "force-stop-app", "--name", "transit",
        ])
        result = android.device_action(args, FakeAdb())
        self.assertEqual(result["package"], "com.example.transit")
        self.assertEqual(calls[-1][0], [
            "shell", "am", "force-stop", "com.example.transit",
        ])
        android._package_cache["device-1"] = (time.monotonic(), [
            "com.vendor.transit.overlay", "com.example.transit", "com.vendor.transit.extra",
        ])
        self.assertEqual(android.resolve_package(FakeAdb(), "device-1", "Transit"), "com.example.transit")
        with self.assertRaisesRegex(android.ControlError, "3 matches"):
            android.resolve_package(FakeAdb(), "device-1", "tra")

    def test_launch_force_restart_stops_then_launches(self):
        calls = []
        launcher = b"com.example.transit/.MainActivity\n"
        start_output = b"Status: ok\n"

        class FakeAdb:
            def run(self, args, **kwargs):
                calls.append((args, kwargs))
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                if args == ["shell", "pm", "list", "packages"]:
                    return b"package:com.example.transit\n"
                if args[:4] == ["shell", "cmd", "package", "resolve-activity"]:
                    return launcher
                if args[:3] == ["shell", "am", "start"]:
                    return start_output
                if args == ["shell", "dumpsys", "window"]:
                    return b"mCurrentFocus=Window{123 u0 com.example.transit/.MainActivity}"
                if args in (
                    ["shell", "am", "force-stop", "com.example.transit"],
                ):
                    return b""
                raise AssertionError(args)

        args = android.action_args(
            "launch-app", name="transit", force_restart=True,
        )
        result = android.device_action(args, FakeAdb())
        self.assertTrue(result["force_restarted"])
        self.assertEqual(result["focus"]["package"], "com.example.transit")
        self.assertEqual([call[0] for call in calls[-3:-1]], [
            ["shell", "am", "force-stop", "com.example.transit"],
            ["shell", "am", "start", "-W", "-f", "0x10200000", "-a", "android.intent.action.MAIN", "-c",
             "android.intent.category.LAUNCHER", "-n", "com.example.transit/.MainActivity"],
        ])
        # Missing launchers must fail before force-stop, and never use monkey.
        calls.clear()
        launcher = b"No activity found\n"
        with self.assertRaisesRegex(android.ControlError, "no launcher activity"):
            android.device_action(args, FakeAdb())
        self.assertFalse(any(command[:3] == ["shell", "am", "force-stop"] for command, _ in calls))
        launcher = b"com.example.transit/.MainActivity\n"
        start_output = b"Error: Activity class does not exist\n"
        with self.assertRaisesRegex(android.ControlError, "app launch failed"):
            android.device_action(args, FakeAdb())
        # Prefer the one launchable app when several strongest names match.
        candidates = ["com.android.providers.settings", "com.android.settings", "com.vendor.settings.overlay"]
        android._package_cache["device-1"] = (time.monotonic(), candidates)
        launchable = {"com.android.settings"}
        def launcher_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:4] == ["shell", "cmd", "package", "resolve-activity"]:
                package = command[-1]
                return f"{package}/.MainActivity\n".encode() if package in launchable else b"No activity found\n"
            if command[:3] == ["shell", "am", "start"]:
                return b"Status: ok\n"
            if command == ["shell", "dumpsys", "window"]:
                return b"mCurrentFocus=Window{123 u0 com.android.settings/.MainActivity}"
            raise AssertionError(command)
        adb = FakeAdb()
        with patch.object(adb, "run", side_effect=launcher_run):
            result = android.device_action(android.action_args("launch-app", name="Settings", target="device-1"), adb)
            self.assertEqual(result["package"], "com.android.settings")
            with self.assertRaisesRegex(android.ControlError, "2 matches"):
                android.resolve_package(adb, "device-1", "settings")
            launchable.add("com.android.providers.settings")
            calls.clear()
            with self.assertRaisesRegex(android.ControlError, "2 matches"):
                android.device_action(android.action_args("launch-app", name="settings", target="device-1"), adb)
            self.assertTrue(all(command[:4] == ["shell", "cmd", "package", "resolve-activity"] for command, _ in calls))
            launchable.clear()
            # A full package is always selected directly, regardless of launchability.
            calls.clear()
            self.assertEqual(android.resolve_package(adb, "device-1", "com.android.settings", prefer_launchable=True),
                             "com.android.settings")
            self.assertEqual(calls, [])
        with patch.object(adb, "run", side_effect=android.ControlError("device offline")):
            with self.assertRaisesRegex(android.ControlError, "device offline"):
                android.resolve_package(adb, "device-1", "settings", prefer_launchable=True)

    def test_mcp_is_an_alternative_mode(self):
        args = android.build_parser().parse_args(["--mcp", "--port", "9000"])
        self.assertTrue(args.mcp)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            android.build_parser().parse_args(["--mcp", "--action", "status"])

    def test_server_discovery_requires_matching_identity_and_pid(self):
        url = "http://127.0.0.1:9123"
        android.write_server_file(url)
        valid = io.BytesIO(json.dumps({
            "name": android.SERVER_NAME,
            "api_version": android.SERVER_API_VERSION,
            "pid": os.getpid(),
        }).encode())
        with patch.object(android.urlrequest, "urlopen", return_value=valid):
            self.assertEqual(android.discover_server(), url)

        android.write_server_file(url)
        wrong = io.BytesIO(json.dumps({
            "name": "different-server",
            "api_version": android.SERVER_API_VERSION,
            "pid": os.getpid(),
        }).encode())
        with patch.object(android.urlrequest, "urlopen", return_value=wrong):
            self.assertIsNone(android.discover_server())
        self.assertFalse(self.server_path.exists())

    def test_execute_via_server_returns_structured_result(self):
        response = io.BytesIO(json.dumps({
            "ok": True,
            "result": {
                "structuredContent": {
                    "target": "device-1", "state": "device",
                },
            },
        }).encode())
        args = android.build_parser().parse_args(["--action", "status"])
        with patch.object(android.urlrequest, "urlopen", return_value=response) as request:
            result = android.execute_via_server(args, "http://127.0.0.1:8000")
        self.assertEqual(result["target"], "device-1")
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent, {"name": "status", "arguments": {}})

    def test_execute_via_server_downloads_screenshot(self):
        call = io.BytesIO(json.dumps({
            "ok": True,
            "result": {"content": [{"type": "image", "cachedForViewer": True}]},
            "viewer": {"target": "device-1", "revision": 1},
        }).encode())
        png = android.PNG_MAGIC + b"test"
        output = Path(self.temporary_directory.name) / "screen.png"
        args = android.build_parser().parse_args([
            "--action", "screenshot", "--output", str(output),
        ])
        with patch.object(
            android.urlrequest, "urlopen", side_effect=[call, io.BytesIO(png)]
        ) as urlopen:
            result = android.execute_via_server(args, "http://127.0.0.1:8000")
        self.assertEqual(result["target"], "device-1")
        self.assertEqual(output.read_bytes(), png)
        self.assertEqual(
            urlopen.call_args_list[1].args[0],
            "http://127.0.0.1:8000/api/viewer/screenshot?target=device-1&revision=1",
        )

    def test_execute_via_server_sends_screenshot_region(self):
        call = io.BytesIO(json.dumps({
            "ok": True,
            "result": {"content": [{"type": "image", "cachedForViewer": True}]},
            "viewer": {"target": "device-1", "revision": 2, "region": [1, 2, 3, 4]},
        }).encode())
        output = Path(self.temporary_directory.name) / "crop.png"
        args = android.build_parser().parse_args([
            "--action", "screenshot", "--output", str(output),
            "--x1", "1", "--y1", "2", "--x2", "3", "--y2", "4",
        ])
        with patch.object(
            android.urlrequest, "urlopen",
            side_effect=[call, io.BytesIO(android.PNG_MAGIC + b"crop")],
        ) as urlopen:
            result = android.execute_via_server(args, "http://127.0.0.1:8000")
        self.assertEqual(result["region"], [1, 2, 3, 4])
        sent = json.loads(urlopen.call_args_list[0].args[0].data)
        self.assertEqual(sent, {
            "name": "screenshot", "arguments": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
        })

    def test_screenshot_region_requires_all_ordered_corners(self):
        self.assertIsNone(android.screenshot_region(None, None, None, None))
        self.assertEqual(android.screenshot_region(0, 1, 2, 3), (0, 1, 2, 3))
        for corners in ((0, 1, 2, None), (2, 0, 2, 3), (0, 3, 2, 3)):
            with self.assertRaises(android.ControlError):
                android.screenshot_region(*corners)
        partial = android.action_args("screenshot", x1=0, y1=0)
        with self.assertRaisesRegex(android.ControlError, "all of x1, y1, x2, and y2"):
            android.prepare_action(partial)

    @staticmethod
    def decode_png(png):
        width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        pixels = zlib.decompress(png[41:-16])  # IDAT data: after IHDR, before CRC + IEND
        stride = width * 4 + 1
        return width, height, [pixels[row * stride + 1:(row + 1) * stride] for row in range(height)]

    def test_screenshot_region_transfers_rows_and_crops_columns(self):
        width, height = 4, 3
        screen = [bytes([x, y, 0, 255]) for y in range(height) for x in range(width)]
        rows = [b"".join(screen[y * width:(y + 1) * width]) for y in range(height)]
        calls = []

        class FakeAdb:
            def run(self, args, **kwargs):
                calls.append(args)
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                if args[0] == "exec-out" and "screencap" in args[1]:
                    return b"4 3 1 16\n" + zlib.compress(b"".join(rows[1:3]), wbits=31)
                raise AssertionError(args)

        output = Path(self.temporary_directory.name) / "crop.png"
        args = android.action_args("screenshot", output=str(output), x1=1, y1=1, x2=3, y2=3)
        android.prepare_action(args)
        result = android.device_action(args, FakeAdb())

        self.assertEqual(result["region"], [1, 1, 3, 3])
        self.assertIn("head -c $((2 * w * 4)) | gzip -1", calls[-1][1])
        self.assertIn("[ 3 -le $w ] && [ 3 -le $h ]", calls[-1][1])
        crop_width, crop_height, crop_rows = self.decode_png(output.read_bytes())
        self.assertEqual((crop_width, crop_height), (2, 2))
        self.assertEqual(crop_rows, [row[4:12] for row in rows[1:3]])

    def test_screenshot_region_must_fit_the_screen(self):
        class FakeAdb:
            def run(self, args, **kwargs):
                return b"4 3 1 16\n"

        with self.assertRaisesRegex(android.ControlError, "exceeds the 4x3 screen"):
            android.capture_screen(FakeAdb(), "device-1", (0, 0, 5, 3))

    @staticmethod
    def fake_mp4(timescale=1000, duration=4580):
        def box(kind, body):
            return (len(body) + 8).to_bytes(4, "big") + kind + body
        mvhd = bytes(12) + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big") + bytes(80)
        # A stray "mvhd" in the media data must not be mistaken for the header.
        return box(b"ftyp", b"isom" + bytes(4)) + box(b"mdat", b"..mvhd..") + box(b"moov", box(b"mvhd", mvhd))

    def test_start_screen_record_runs_detached_and_reports_state(self):
        replies = iter([b"started\n", b"running\n", b"failed\nError: encoder rejected size\n", b"started\n"])
        scripts = []
        display = DISPLAY

        class FakeAdb:
            def run(self, args, **kwargs):
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                if args == ["shell", "dumpsys", "display"]:
                    return display
                scripts.append(args[1])
                return next(replies)

        args = android.action_args("start-screen-record", time_limit=30)
        android.prepare_action(args)
        self.assertEqual(android.device_action(args, FakeAdb())["time_limit"], 30)
        self.assertIn("nohup screenrecord --size 720x1280 --time-limit 30 --bit-rate 4000000", scripts[0])
        with self.assertRaisesRegex(android.ControlError, "already running"):
            android.device_action(args, FakeAdb())
        with self.assertRaisesRegex(android.ControlError, "failed to start: Error: encoder rejected size"):
            android.device_action(args, FakeAdb())
        display = DISPLAY.replace(b"1080 x 1920", b"1920 x 1080")
        android.device_action(args, FakeAdb())
        self.assertIn("nohup screenrecord --size 1280x720", scripts[-1])

    def test_stop_screen_record_saves_mp4_with_its_length(self):
        video = self.fake_mp4()
        root = Path(self.temporary_directory.name)
        remote = root / "device-record"
        remote_video = remote.with_suffix(".mp4")
        remote_pid = remote.with_suffix(".pid")
        remote_video.write_bytes(video)
        remote_pid.write_text("999999999")

        class FakeAdb:
            timeout = 30.0

            def run(self, args, **kwargs):
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                self_test.assertEqual(args[0], "exec-out")
                self_test.assertGreaterEqual(kwargs["timeout"], 300)
                return subprocess.run(["/bin/sh", "-c", args[1]], check=True,
                                      capture_output=True, timeout=5).stdout

        self_test = self
        output = root / "screen.mp4"
        args = android.action_args("stop-screen-record", output=str(output))
        with patch("scripts.android_core.RECORD_PATH", str(remote)):
            args.output = str(root)
            with self.assertRaisesRegex(android.ControlError, "could not save recording"):
                android.device_action(args, FakeAdb())
            self.assertEqual(remote_video.read_bytes(), video)
            self.assertTrue(remote_pid.exists())
            args.output = str(output)
            result = android.device_action(args, FakeAdb())
            self.assertEqual(result["seconds"], 4.58)
            self.assertEqual(output.read_bytes(), video)
            self.assertEqual(remote_video.read_bytes(), video)
            output.write_bytes(b"previous recording")
            with patch("scripts.android_core.os.replace", side_effect=OSError("disk full")), \
                 self.assertRaisesRegex(android.ControlError, "device copy retained"):
                android.device_action(args, FakeAdb())
            self.assertEqual(output.read_bytes(), b"previous recording")
            self.assertEqual(list(root.glob(".screen.mp4.*.tmp")), [])
            for broken in (b"", video[:12], video[:-5], video + b"trailing", self.fake_mp4(timescale=0)):
                with self.subTest(broken_length=len(broken)):
                    remote_video.write_bytes(broken)
                    with self.assertRaisesRegex(android.ControlError, "no valid MP4"):
                        android.device_action(args, FakeAdb())
                    self.assertEqual(output.read_bytes(), b"previous recording")
                    self.assertTrue(remote_video.exists())
            remote_video.write_bytes(video)
            android.device_action(args, FakeAdb())
            self.assertEqual(output.read_bytes(), video)
            # A retained PID may have been reused by another screenrecord.
            unrelated = subprocess.Popen(["/bin/bash", "-c", "exec -a screenrecord sleep 30"])
            try:
                deadline = time.monotonic() + 2
                while not Path(f"/proc/{unrelated.pid}/cmdline").read_bytes().startswith(b"screenrecord\0"):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                remote_pid.write_text(str(unrelated.pid))
                android.device_action(args, FakeAdb())
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)
            remote_pid.unlink()
            remote_video.unlink()
            with self.assertRaisesRegex(android.ControlError, "no screen recording is running"):
                android.device_action(args, FakeAdb())

    def test_recording_arguments(self):
        with self.assertRaisesRegex(android.ControlError, "--time-limit is required"):
            android.prepare_action(android.action_args("start-screen-record"))
        stop = android.action_args("stop-screen-record")
        android.prepare_action(stop)
        self.assertRegex(stop.output, r"^android-screen-\d{8}-\d{6}-\d{6}\.mp4$")
        selected = stop.output
        android.prepare_action(stop)
        self.assertEqual(stop.output, selected)
        for action in ("screenshot", "stop-screen-record"):
            explicit = android.action_args(action, output="exact-name.file")
            android.prepare_action(explicit)
            self.assertEqual(explicit.output, "exact-name.file")
        with self.assertRaisesRegex(android.ControlError, "between 1 and 180"):
            android.prepare_action(android.action_args("start-screen-record", time_limit=181))
        with self.assertRaisesRegex(android.ControlError, "not valid for status"):
            android.prepare_action(android.action_args("status", time_limit=5))

    def test_direct_api_defaults_and_validation_before_adb(self):
        calls = []
        png = android.encode_png(1, 1, [bytes([10, 20, 30, 255])])
        video = self.fake_mp4()
        class FakeAdb:
            timeout = 30
            def run(self, args, **kwargs):
                calls.append(args)
                if args == ["exec-out", "screencap", "-p"]:
                    return png
                if args[0] == "exec-out" and "kill -INT" in args[1]:
                    return b"stopped\n" + video
                raise AssertionError(args)
        with chdir(self.temporary_directory.name):
            for action, extension, data in (
                ("screenshot", "png", png),
                ("stop-screen-record", "mp4", video),
            ):
                with self.subTest(default_output=action):
                    result = android.device_action(android.action_args(action, target="phone"), FakeAdb())
                    filename = Path(result["path"]).name
                    self.assertRegex(filename, rf"^android-screen-\d{{8}}-\d{{6}}-\d{{6}}\.{extension}$")
                    self.assertEqual(Path(filename).read_bytes(), data)
                    self.assertEqual(result["path"], str(Path(filename).resolve()))
        calls.clear()
        for action, values in (
            ("launch-app", {}), ("force-stop-app", {}), ("start-screen-record", {}),
            ("start-screen-record", {"time_limit": 181}),
            ("wait-for-ui", {"text": "Ready", "state": "awake"}),
            ("tap", {"x": 1, "y": 2, "duration": 10}),
            ("tap", {"x": -1, "y": 2}),
            ("long-press", {"x": 1, "y": 2, "duration": 0}),
        ):
            with self.subTest(invalid_action=action, values=values), self.assertRaises(android.ControlError):
                android.device_action(android.action_args(action, **values), FakeAdb())
        for action in ("list-targets", "connect", "disconnect", "pair"):
            values = {} if action == "list-targets" else {"address": "phone.local:5555"}
            if action == "pair":
                values["pair_code"] = "654321"
            with self.subTest(host_action=action), self.assertRaisesRegex(android.ControlError, "host action; use execute"):
                android.device_action(android.action_args(action, **values), FakeAdb())
        args = android.action_args("status")
        args.action = "unsupported"
        with self.assertRaisesRegex(android.ControlError, "unsupported action"):
            android.device_action(args, FakeAdb())
        self.assertEqual(calls, [])

    def test_input_bounds_and_literal_text(self):
        root = Path(self.temporary_directory.name)
        typed = root / "typed.txt"
        injector = root / "input"
        injector.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "with open(os.environ['TEST_TYPED_OUTPUT'], 'a') as output:\n"
            "    output.write(sys.argv[2].replace('%s', ' '))\n"
        )
        injector.chmod(0o755)
        commands = []
        class FakeAdb:
            def run(self, args, **kwargs):
                commands.append(args)
                if args == ["shell", "dumpsys", "display"]:
                    return DISPLAY
                if args[:2] == ["shell", "input"]:
                    return b""
                if args[0] == "shell" and args[1].startswith("input text "):
                    result = subprocess.run(["/bin/sh", "-c", args[1]], check=True,
                                            env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                                                     TEST_TYPED_OUTPUT=str(typed)),
                                            capture_output=True, timeout=5)
                    return result.stdout
                raise AssertionError(args)
        for action, values in (
            ("tap", {"x": 1080, "y": 1}),
            ("long-press", {"x": 1, "y": 1920}),
            ("swipe", {"x1": 1, "y1": 1, "x2": 5000, "y2": 1}),
        ):
            with self.subTest(outside=action), self.assertRaisesRegex(android.ControlError, "exceed.*display"):
                android.device_action(android.action_args(action, target="phone", **values), FakeAdb())
        self.assertTrue(all(command == ["shell", "dumpsys", "display"] for command in commands))
        for action, values, duration in (
            ("long-press", {"x": 1, "y": 2}, 1000),
            ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}, 300),
        ):
            result = android.device_action(android.action_args(action, target="phone", **values), FakeAdb())
            self.assertEqual(result["duration_ms"], duration)
        for text in ("100%sure", "%s%s", "100% safe", "a %s b", "it's $HOME; & | <> `uname`", ""):
            with self.subTest(text=text):
                typed.write_text("")
                result = android.device_action(android.action_args("type-text", text=text, target="phone"), FakeAdb())
                self.assertEqual(typed.read_text(), text)
                self.assertEqual(result["characters"], len(text))
        commands.clear()
        for text in ("中文", "hello\nworld", "\t"):
            with self.subTest(unsupported=text), self.assertRaisesRegex(android.ControlError, "printable ASCII"):
                android.device_action(android.action_args("type-text", text=text, target="phone"), FakeAdb())
        self.assertEqual(commands, [])

    def test_execute_via_server_downloads_recording(self):
        server = "http://127.0.0.1:8000"
        relative = "/api/viewer/recording?target=device-1&revision=1"
        video = self.fake_mp4()
        output = Path(self.temporary_directory.name) / "screen.mp4"
        args = android.build_parser().parse_args([
            "--action", "stop-screen-record", "--output", str(output),
        ])
        for url in (relative, server + relative, "https://public.example:40000/phone" + relative):
            with self.subTest(public_url=url):
                call = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": {
                    "target": "device-1", "revision": 1, "seconds": 4.58, "bytes": len(video), "url": url,
                }}}).encode())
                with patch.object(android.urlrequest, "urlopen", side_effect=[call, io.BytesIO(video)]) as urlopen:
                    result = android.execute_via_server(args, server)
                self.assertEqual(result, {"target": "device-1", "path": str(output), "bytes": len(video), "seconds": 4.58})
                self.assertEqual(output.read_bytes(), video)
                self.assertEqual(urlopen.call_args_list[1].args[0], server + relative)
                self.assertGreaterEqual(urlopen.call_args_list[0].kwargs["timeout"], 300 + args.timeout)
                self.assertGreaterEqual(urlopen.call_args_list[1].kwargs["timeout"], 300)
                self.assertEqual(json.loads(urlopen.call_args_list[0].args[0].data), {
                    "name": "stop_screen_record", "arguments": {},
                })
        for broken, expected_bytes, message in (
            (video[:-1], len(video), "download was incomplete"),
            (video[:12], 12, "no valid MP4"),
        ):
            with self.subTest(download_length=len(broken)):
                call = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": {
                    "target": "device-1", "revision": 1, "bytes": expected_bytes, "url": relative,
                }}}).encode())
                with patch.object(android.urlrequest, "urlopen", side_effect=[call, io.BytesIO(broken)]), \
                     self.assertRaisesRegex(android.ControlError, message):
                    android.execute_via_server(args, server)
                self.assertEqual(output.read_bytes(), video)

    def test_set_default_target_requires_explicit_ready_target(self):
        args = android.build_parser().parse_args(["--action", "set-default-target"])
        with self.assertRaisesRegex(android.ControlError, "--target is required"):
            android.validate_action_args(args)

        adb = self.fake_adb(("device-1", "device"), ("device-2", "offline"))
        result = android.set_default_target(adb, "device-1")
        self.assertEqual(result["target"], "device-1")
        self.assertEqual(android.read_default_target(), "device-1")
        with self.assertRaisesRegex(android.ControlError, "not connected and ready"):
            android.set_default_target(adb, "device-2")

    def test_one_ready_target_is_automatic_without_default(self):
        adb = self.fake_adb(("device-1", "device"), ("device-2", "offline"))
        self.assertEqual(android.resolve_serial(adb, None), "device-1")
        self.assertFalse(self.state_path.exists())

    def test_fresh_ready_default_resolves_multiple_targets(self):
        adb = self.fake_adb(("device-1", "device"), ("device-2", "device"))
        android.write_default_target("device-2", now=time.time() - 3599)
        self.assertEqual(android.resolve_serial(adb, None), "device-2")

    def test_stale_or_disconnected_default_does_not_resolve_ambiguity(self):
        adb = self.fake_adb(("device-1", "device"), ("device-2", "device"))
        self.state_path.write_text(json.dumps({
            "target": "device-2", "selected_at": time.time() - 3600,
        }))
        with self.assertRaisesRegex(android.AmbiguousTargetError, "multiple ready targets"):
            android.resolve_serial(adb, None)

        android.write_default_target("device-3")
        with self.assertRaisesRegex(android.ControlError, "specify --target"):
            android.resolve_serial(adb, None)

    def test_explicit_target_is_call_scoped_and_does_not_persist(self):
        adb = self.fake_adb(("device-1", "device"), ("device-2", "device"))
        self.assertEqual(android.resolve_serial(adb, "device-2"), "device-2")
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
