"""Offline log capture tests: limits, process selection, crash context, and transport."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts import android_core as core
from scripts import android_client as client


def entry(pid, message, priority="E", tag="AndroidRuntime", tid=None):
    return f"10-15 17:53:34.130 {pid:5} {tid or pid:5} {priority} {tag}: {message}\n"


class FakeAdb:
    def __init__(self, text="", processes="123 com.example.app\n124 com.example.app:worker\n"):
        self.text = text
        self.processes = processes
        self.commands = []

    def run(self, args, **kwargs):
        self.commands.append(args)
        if args == ["devices", "-l"]:
            return b"List of devices attached\nphone device\n"
        if args == ["shell", "ps", "-A", "-o", "PID,NAME"]:
            return self.processes.encode()
        if args == ["shell", "logcat", "-b", "main,system,crash", "-c"]:
            return b""
        raise AssertionError(args)

    def run_tail(self, args, **kwargs):
        self.commands.append(args)
        return self.text.encode(), False


class AndroidLogTests(unittest.TestCase):
    def filtered(self, text, **kwargs):
        values = dict(crash=False, package=None, pids=[], priority="V", match=None, lines=200)
        return core.filter_log_text(text, **(values | kwargs))

    def test_defaults_and_argument_validation(self):
        for kind in (None, "standard", "crash"):
            args = core.action_args("logs", kind=kind)
            core.prepare_action(args)
            self.assertEqual((args.kind, args.lines, args.priority), (kind or "standard", 200, "V"))
            for lines in (0, 5001, -1):
                with self.assertRaises(core.ControlError):
                    core.prepare_action(core.action_args("logs", kind=kind, lines=lines))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            core.build_parser().parse_args(["--action", "logs", "--kind", "unknown"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            core.build_parser().parse_args(["--action", "crash-logs"])
        for action in ("status", "clear-logs"):
            with self.assertRaises(core.ControlError):
                core.prepare_action(core.action_args(action, kind="crash"))
        with self.assertRaises(core.ControlError):
            core.prepare_action(core.action_args("clear-logs", name="com.example.app"))
        with self.assertRaises(core.ControlError):
            core.prepare_action(core.action_args("tap", x=1, y=2, lines=20))

    def test_filters_apply_before_physical_line_limit(self):
        text = (entry(123, "audio started", "I", "Audio")
                + entry(123, "audio warning\ncontinued\nmore detail", "W", "Audio")
                + entry(124, "other app", "E"))
        result = self.filtered(text, package="com.example.app", pids=[123],
                               priority="W", match="audio", lines=2)
        self.assertEqual(result["text"], "continued\nmore detail\n")
        self.assertEqual(result["matched_lines"], 3)
        self.assertEqual(result["line_count"], 2)
        self.assertTrue(result["truncated"])

    def test_crash_package_filter_keeps_stack_and_excludes_other_reports(self):
        text = (entry(10, "FATAL EXCEPTION: main")
                + entry(10, "Process: com.example.app:worker, PID: 10")
                + entry(11, "FATAL EXCEPTION: main")
                + entry(11, "Process: com.other.app, PID: 11")
                + entry(10, "java.lang.IllegalStateException: broken")
                + entry(10, "    at com.example.Code.run(Code.java:20)")
                + entry(11, "mentions com.example.app but isn't its crash")
                + entry(10, "FATAL EXCEPTION: main")
                + entry(10, "Process: com.other.app, PID: 10"))
        result = self.filtered(text, crash=True, package="com.example.app", match="broken")
        self.assertEqual(result["line_count"], 4)
        self.assertIn("FATAL EXCEPTION", result["text"])
        self.assertIn("Code.java:20", result["text"])
        self.assertNotIn("com.other.app", result["text"])
        self.assertFalse(result["truncated"])
        self.assertEqual(self.filtered(text, crash=True, package="com.example.ap")["text"], "")

    def test_native_crash_context_and_limit_disclosure(self):
        text = (entry(99, "*** *** ***", "F", "DEBUG")
                + entry(99, "Cmdline: com.example.app", "F", "DEBUG")
                + entry(99, "signal 11 (SIGSEGV)", "F", "DEBUG")
                + entry(99, "backtrace:\n  #00 crash\n  #01 caller", "F", "DEBUG"))
        result = self.filtered(text, crash=True, package="com.example.app", lines=3)
        self.assertEqual(result["line_count"], 3)
        self.assertTrue(result["truncated"])
        self.assertIn("#01 caller", result["text"])

    def test_output_byte_cap_and_empty_output(self):
        text = entry(123, "big\n" + "x" * 500 + "\nlast")
        result = self.filtered(text, byte_limit=100)
        self.assertLessEqual(len(result["text"].encode()), 100)
        self.assertEqual(result["text"], "last\n")
        self.assertTrue(result["truncated"])
        empty = self.filtered("--------- beginning of crash\n")
        self.assertEqual((empty["line_count"], empty["text"], empty["truncated"]), (0, "", False))

    def test_capture_running_processes_literal_match_and_output(self):
        adb = FakeAdb(entry(123, "$(literal)") + entry(124, "child") + entry(125, "unrelated"))
        with tempfile.TemporaryDirectory() as directory:
            args = core.action_args("logs", name="com.example.app", match="$(literal)",
                                    output=str(Path(directory) / "log.txt"), target="phone")
            core.prepare_action(args)
            result = core.device_action(args, adb)
            self.assertEqual(result["pids"], [123, 124])
            self.assertEqual((result["action"], result["kind"]), ("logs", "standard"))
            self.assertEqual(result["buffers"], ["main", "system", "crash"])
            self.assertEqual(result["line_count"], 1)
            self.assertEqual(Path(result["path"]).read_text(), result["text"])
            command = adb.commands[-1]
            self.assertIn("-d", command)
            self.assertNotIn("$(literal)", command)
            self.assertNotIn("-c", command)

    def test_exited_app_logs_error_but_crash_capture_works(self):
        args = core.action_args("logs", name="com.example.app", target="phone")
        core.prepare_action(args)
        with self.assertRaisesRegex(core.ControlError, "use logs --kind crash"):
            core.device_action(args, FakeAdb(processes=""))
        args.kind = "crash"
        adb = FakeAdb(entry(1, "FATAL EXCEPTION: main") +
                      entry(1, "Process: com.example.app, PID: 1"), processes="")
        result = core.device_action(args, adb)
        self.assertEqual(result["line_count"], 2)
        self.assertEqual((result["action"], result["kind"]), ("logs", "crash"))
        self.assertEqual(result["buffers"], ["crash"])
        self.assertNotIn("--pid=1", adb.commands[-1])
        self.assertNotIn(["shell", "ps", "-A", "-o", "PID,NAME"], adb.commands)

    def test_single_pid_filter_and_scan_truncation_metadata(self):
        for kind in ("standard", "crash"):
            adb = FakeAdb(entry(123, "FATAL EXCEPTION: main") +
                          entry(123, "Process: com.example.app, PID: 123"),
                          processes="123 com.example.app\n")
            original = adb.run_tail

            def truncated_tail(*args, **kwargs):
                text, _ = original(*args, **kwargs)
                return text, True
            adb.run_tail = truncated_tail
            args = core.action_args("logs", kind=kind, name="com.example.app", target="phone")
            core.prepare_action(args)
            result = core.device_action(args, adb)
            if kind == "standard":
                self.assertIn("--pid=123", adb.commands[-1])
                self.assertEqual(result["pids"], [123])
            else:
                self.assertNotIn("--pid=123", adb.commands[-1])
                self.assertEqual(result["pids"], [])
            self.assertEqual(result["line_count"], 2)
            self.assertTrue(result["scan_truncated"])
            self.assertTrue(result["truncated"])

    def test_clear_is_separate_and_buffers_are_explicit(self):
        adb = FakeAdb()
        args = core.action_args("clear-logs", target="phone")
        core.prepare_action(args)
        result = core.device_action(args, adb)
        self.assertEqual(result["buffers"], ["main", "system", "crash"])
        self.assertEqual(adb.commands[-1], ["shell", "logcat", "-b", "main,system,crash", "-c"])

    def test_forwarded_cli_saves_log_file_locally(self):
        for kind in (None, "crash"):
            with tempfile.TemporaryDirectory() as directory:
                args = core.action_args("logs", kind=kind, output=str(Path(directory) / "logs.txt"))
                payload = {"text": "sample\n", "line_count": 1, "kind": kind or "standard"}
                response = io.BytesIO(json.dumps({"ok": True, "result": {"structuredContent": payload}}).encode())
                with patch("scripts.android_client.urlrequest.urlopen", return_value=response) as request:
                    result = client.execute_via_server(args, "http://127.0.0.1:8000")
                self.assertEqual(Path(result["path"]).read_text(), "sample\n")
                sent = json.loads(request.call_args.args[0].data)
                self.assertEqual(sent, {"name": "logs", "arguments": {
                    "kind": kind or "standard", "priority": "V", "lines": 200,
                }})

    def test_bounded_reader_drains_both_streams_and_discloses_truncation(self):
        script = "import sys; sys.stderr.write('e'*100000); sys.stdout.write('old\\n'+'x'*10000+'\\nlatest\\n')"
        data, truncated = core.run_process_tail([sys.executable, "-c", script], 5, 100)
        self.assertEqual(data, b"latest\n")
        self.assertTrue(truncated)
        with self.assertRaisesRegex(core.ControlError, "timed out"):
            core.run_process_tail([sys.executable, "-c", "import time; time.sleep(10)"], 0.1, 100)
        with self.assertRaisesRegex(core.ControlError, "failed"):
            core.run_process_tail([sys.executable, "-c", "import sys; sys.stderr.write('failed'); sys.exit(1)"], 5, 100)


if __name__ == "__main__":
    unittest.main()
