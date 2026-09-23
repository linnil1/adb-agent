#!/usr/bin/env python3
"""Offline unit tests for android_control.py."""

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("android_control.py")
SPEC = importlib.util.spec_from_file_location("android_control", MODULE_PATH)
assert SPEC and SPEC.loader
android = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(android)


class AndroidControlTests(unittest.TestCase):
    def setUp(self):
        android._package_cache.clear()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "state.json"
        self.state_override = patch.dict(
            os.environ, {android.STATE_FILE_ENV: str(self.state_path)}, clear=False
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

    def test_parse_devices(self):
        output = (
            "List of devices attached\n"
            "127.0.0.1:5555 device product:p model:Pixel_8 device:shiba transport_id:1\n"
        )
        result = android.parse_devices(output)
        self.assertEqual(result[0]["serial"], "127.0.0.1:5555")
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

    def test_parse_ui_xml_preserves_accessibility_state(self):
        xml = (
            '<hierarchy>'
            '<node text="" content-desc="More" class="android.widget.Button" '
            'clickable="true" selected="false" scrollable="false" enabled="true" '
            'checked="false" focusable="true" bounds="[0,0][40,40]"/>'
            '<node text="" content-desc="Transit 2.0&#10;Tab 1 of 2" '
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
            "More", "Transit 2.0\nTab 1 of 2", "RecyclerView",
        ])
        self.assertEqual(elements[1]["content_description"], "Transit 2.0\nTab 1 of 2")
        self.assertTrue(elements[1]["selected"])
        self.assertTrue(elements[1]["enabled"])
        self.assertTrue(elements[1]["checked"])
        self.assertTrue(elements[1]["focusable"])
        self.assertTrue(elements[2]["scrollable"])
        self.assertFalse(elements[2]["clickable"])
        self.assertFalse(elements[2]["checked"])
        self.assertFalse(elements[2]["focusable"])

    def test_describe_ui_supports_json_and_original_xml(self):
        xml = '<hierarchy><node text="OK" clickable="true" bounds="[0,0][10,20]"/></hierarchy>'
        parsed = android.describe_ui("device-1", xml, "json")
        self.assertEqual(parsed["format"], "json")
        self.assertEqual(parsed["elements"][0]["center"], [5, 10])

        original = android.describe_ui("device-1", xml, "original")
        self.assertEqual(original, {
            "serial": "device-1", "format": "original", "xml": xml,
        })

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

    def test_launch_force_restart_stops_then_launches(self):
        calls = []

        class FakeAdb:
            def run(self, args, **kwargs):
                calls.append((args, kwargs))
                if args == ["devices", "-l"]:
                    return b"List of devices attached\ndevice-1 device\n"
                if args == ["shell", "pm", "list", "packages"]:
                    return b"package:com.example.transit\n"
                if args in (
                    ["shell", "am", "force-stop", "com.example.transit"],
                    ["shell", "monkey", "-p", "com.example.transit", "-c", "android.intent.category.LAUNCHER", "1"],
                ):
                    return b""
                raise AssertionError(args)

        args = android.action_args(
            "launch-app", name="transit", force_restart=True,
        )
        result = android.device_action(args, FakeAdb())
        self.assertTrue(result["force_restarted"])
        self.assertEqual([call[0] for call in calls[-2:]], [
            ["shell", "am", "force-stop", "com.example.transit"],
            ["shell", "monkey", "-p", "com.example.transit", "-c", "android.intent.category.LAUNCHER", "1"],
        ])

    def test_mcp_is_an_alternative_mode(self):
        args = android.build_parser().parse_args(["--mcp", "--port", "9000"])
        self.assertTrue(args.mcp)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            android.build_parser().parse_args(["--mcp", "--action", "status"])

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
