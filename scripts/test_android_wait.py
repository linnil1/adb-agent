"""Selector waits: strict snapshots, deadlines, and CLI forwarding."""
import html
import io
import json
import unittest
from unittest.mock import patch

from scripts import android_core as core
from scripts import android_client as client
from scripts import android_wait as wait


def node(**attributes):
    values = {"text": "", "content-desc": "", "resource-id": "", "bounds": "[0,0][100,40]",
              "enabled": "true", **attributes}
    return "<node " + " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in values.items()) + "/>"


def hierarchy(*nodes):
    return "<hierarchy>" + "".join(nodes) + "</hierarchy>"


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class AndroidWaitTests(unittest.TestCase):
    def run_wait(self, snapshots, **values):
        clock = Clock()
        with patch("scripts.android_wait.time", clock), \
             patch("scripts.android_wait.dump_ui", side_effect=snapshots):
            return wait.wait_for_ui(object(), "phone", wait_timeout=3, **values)

    def test_delayed_appearance_exact_combined_selectors_and_all_matches(self):
        wanted = node(text="Ride history", **{"content-desc": "Trips", "resource-id": "com.example:id/history"})
        almost = node(text="Ride history", **{"resource-id": "com.example:id/other"})
        result = self.run_wait([hierarchy(almost), hierarchy(wanted, wanted)],
                               text="Ride history", description="Trips", resource_id="com.example:id/history")
        self.assertEqual((result["attempts"], result["elapsed_seconds"]), (2, 1.0))
        self.assertEqual(len(result["elements"]), 2)
        self.assertEqual(result["elements"][0]["center"], [50, 20])
        with self.assertRaisesRegex(core.ControlError, "timed out"):
            self.run_wait([hierarchy(wanted)] * 3, text="Ride")

    def test_enabled_waits_for_matching_element_to_become_enabled(self):
        disabled = hierarchy(node(text="Continue", enabled="false"))
        enabled = hierarchy(node(text="Continue"))
        result = self.run_wait([disabled, enabled], text="Continue", state="enabled")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("enabled", result["elements"][0]["states"])

    def test_gone_requires_a_successful_valid_snapshot(self):
        present = hierarchy(node(text="Loading"))
        result = self.run_wait([present, core.ControlError("offline"), hierarchy()],
                               text="Loading", state="gone")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["elements"], [])
        self.assertTrue(result["matched"])
        for bad in ("not XML", "<hierarchy><node", "<error/>"):
            with self.subTest(bad=bad), self.assertRaisesRegex(core.ControlError, "could not inspect"):
                self.run_wait([bad] * 3, text="Loading", state="gone")
        with self.assertRaisesRegex(core.ControlError, "last capture failed"):
            self.run_wait([present, core.ControlError("offline"), core.ControlError("offline")],
                          text="Loading", state="gone")

    def test_failed_capture_can_recover_without_counting_as_absence(self):
        result = self.run_wait([core.ControlError("busy"), hierarchy(node(text="Ready"))], text="Ready")
        self.assertEqual(result["attempts"], 2)
        with self.assertRaisesRegex(core.ControlError, "could not inspect"):
            self.run_wait([core.ControlError("offline")] * 3, text="anything")

    def test_resource_id_only_nodes_and_nonvisible_nodes(self):
        hidden = node(**{"resource-id": "button", "visible-to-user": "false"})
        clipped = node(**{"resource-id": "button", "bounds": "[0,0][0,0]"})
        visible = node(**{"resource-id": "button"})
        self.assertEqual(len(core.parse_ui_xml(hierarchy(hidden, clipped, visible))), 3)
        result = self.run_wait([hierarchy(hidden, clipped, visible)], resource_id="button")
        self.assertEqual(len(result["elements"]), 1)
        self.assertEqual(result["elements"][0]["resource_id"], "button")
        self.assertTrue(self.run_wait([hierarchy(hidden, clipped)], resource_id="button", state="gone")["matched"])

    def test_xml_entities_are_matched_as_decoded_text(self):
        result = self.run_wait([hierarchy(node(text="A & B", **{"content-desc": "History > 0"}))],
                               text="A & B", description="History > 0")
        self.assertEqual(result["elements"][0]["text"], "A & B")
        xml = '''<hierarchy><node text='say "hi" >x' bounds='[0,0][100,40]' enabled='true'/></hierarchy>'''
        result = self.run_wait([xml], text='say "hi" >x')
        self.assertEqual(result["elements"][0]["text"], 'say "hi" >x')

    def test_late_capture_cannot_succeed_after_deadline(self):
        clock = Clock()
        def late_dump(*args, **kwargs):
            clock.now = 10
            return hierarchy(node(text="Ready"))
        with patch("scripts.android_wait.time", clock), \
             patch("scripts.android_wait.dump_ui", side_effect=late_dump):
            with self.assertRaisesRegex(core.ControlError, "timed out after 3s"):
                wait.wait_for_ui(object(), "phone", text="Ready", wait_timeout=3)
        for initial in ([], [hierarchy(node(text="Other"))]):
            clock = Clock()
            captures = iter(initial)
            def bounded_dump(*args, **kwargs):
                snapshot = next(captures, None)
                if snapshot is not None:
                    return snapshot
                clock.now = 3
                raise core.ControlError("UI capture deadline reached")
            with self.subTest(successful_captures=len(initial)), \
                 patch("scripts.android_wait.time", clock), \
                 patch("scripts.android_wait.dump_ui", side_effect=bounded_dump):
                with self.assertRaisesRegex(core.ControlError, "timed out after 3s") as raised:
                    wait.wait_for_ui(object(), "phone", text="Ready", wait_timeout=3)
                self.assertNotIn("last capture failed", str(raised.exception))

    def test_wait_arguments_and_deadline_validation(self):
        args = core.action_args("wait-for-ui", text="Ready", target="phone")
        with patch("scripts.android_wait.dump_ui", return_value=hierarchy(node(text="Ready"))):
            result = core.device_action(args, object())
        self.assertEqual(result["state"], "present")
        self.assertEqual((args.state, args.wait_timeout), ("present", 30))
        for values in ({}, {"text": ""}, {"text": "Ready", "description": ""},
                       {"text": "Ready", "state": "awake"}, {"text": "Ready", "state": "asleep"},
                       *({"text": "Ready", "wait_timeout": timeout} for timeout in (0, -1, 301, float("inf"), float("nan")))):
            with self.subTest(values=values), self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args("wait-for-ui", **values))
        with self.assertRaises(core.ControlError):
            core.prepare_action(core.action_args("status", resource_id="button"))

    def test_forwarded_wait_preserves_selectors_and_allows_full_wait_budget(self):
        args = core.action_args("wait-for-ui", resource_id="button", state="enabled", wait_timeout=60)
        response = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": {"matched": True}}}).encode())
        with patch("scripts.android_client.urlrequest.urlopen", return_value=response) as request:
            self.assertTrue(client.execute_via_server(args, "http://127.0.0.1:8000")["matched"])
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent["name"], "wait_for_ui")
        self.assertEqual(sent["arguments"]["wait_timeout"], 60)
        self.assertEqual(sent["arguments"]["resource_id"], "button")
        self.assertGreater(request.call_args.kwargs["timeout"], 60)

    def test_dump_ui_fallback_uses_fresh_path_and_shared_deadline(self):
        commands = []
        xml = hierarchy(node(text="Ready"))
        class Adb:
            timeout = 30
            def run(self, args, **kwargs):
                commands.append((args, kwargs))
                if args[0] == "exec-out":
                    raise core.ControlError("unsupported")
                return ("UI hierarchy dumped\n" + xml + "\nfinished").encode()
        with patch("scripts.android_core.time.monotonic", return_value=10):
            self.assertEqual(core.dump_ui(Adb(), "phone", deadline=12), xml)
        self.assertTrue(all(0 < values["timeout"] <= 2 for _, values in commands))
        script = commands[-1][0][1]
        self.assertIn("android-control-ui-", script)
        self.assertIn("&& cat ", script)
        self.assertIn("rm -f ", script)
        with patch("scripts.android_core.time.monotonic", return_value=12), \
             self.assertRaisesRegex(core.ControlError, "deadline"):
            core.dump_ui(Adb(), "phone", deadline=12)


if __name__ == "__main__":
    unittest.main()
