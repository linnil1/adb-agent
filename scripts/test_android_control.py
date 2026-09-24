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
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


from scripts import android_control as android


MODULE_PATH = Path(__file__).with_name("android_control.py")


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

    def test_direct_cli_entrypoint_loads_split_modules(self):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--help"],
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
            "http://127.0.0.1:8000/api/viewer/screenshot?revision=1",
        )

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
