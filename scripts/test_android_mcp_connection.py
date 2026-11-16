"""Optional live-server tests using a simulated ADB executable, never a phone."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from scripts.android_core import KEY_MAP


@unittest.skipUnless(importlib.util.find_spec("mcp"), "requires optional MCP SDK")
class MCPConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        adb = root / "adb"
        adb.write_text(f"#!{sys.executable}\n" + """
import sys
from pathlib import Path
args = sys.argv[1:]
state = Path(__file__).with_suffix('.state')
if args == ['devices', '-l']:
    print('List of devices attached')
    if state.exists(): print(state.read_text() + ' device')
elif args[0] == 'connect':
    state.write_text(args[1])
    print('connected to ' + args[1])
elif args[0] == 'disconnect':
    state.unlink(missing_ok=True)
    print('disconnected ' + args[1])
elif args[0] == 'pair':
    code = sys.stdin.readline().strip()
    if code == '654321': print('Successfully paired to ' + args[1])
    else:
        print('invalid pairing code ' + code, file=sys.stderr)
        sys.exit(1)
elif args[0] == '-s' and args[2] == 'logcat':
    print('10-15 17:53:34.130   123   123 E AndroidRuntime: FATAL EXCEPTION: main')
    print('10-15 17:53:34.131   123   123 E AndroidRuntime: Process: com.example.app, PID: 123')
    print('10-15 17:53:34.132   123   123 E AndroidRuntime: java.lang.IllegalStateException: broken')
    print('10-15 17:53:34.133   123   123 E AndroidRuntime:     at com.example.Code.run(Code.java:20)')
else:
    sys.exit(1)
""")
        adb.chmod(0o755)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        cls.opener = build_opener(ProxyHandler({}))
        cls.log = (root / "server.log").open("w")
        cls.addClassCleanup(cls.log.close)
        environment = dict(os.environ, ANDROID_CONTROL_SERVER_FILE=str(root / "discovery.json"),
                           ANDROID_CONTROL_STATE_FILE=str(root / "state.json"))
        cls.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("android_control.py")),
             "--mcp", "--port", str(port), "--adb", str(adb)],
            stdout=cls.log, stderr=cls.log, env=environment,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                cls.get("/api/info")
                break
            except (OSError, URLError):
                if cls.process.poll() is not None:
                    raise RuntimeError((root / "server.log").read_text())
                time.sleep(0.05)
        else:
            raise RuntimeError("MCP test server did not start")

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)

    @classmethod
    def get(cls, path):
        with cls.opener.open(cls.url + path, timeout=3) as response:
            return json.load(response)

    def call(self, name, arguments):
        request = Request(self.url + "/api/tools/call",
                          data=json.dumps({"name": name, "arguments": arguments}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return json.load(response)

    def test_host_actions_work_without_connected_devices(self):
        tools = self.get("/api/tools")["tools"]
        self.assertTrue({"connect", "disconnect", "pair"} <= {tool["name"] for tool in tools})
        for name, args in (
            ("connect", {"address": "phone.local:5555"}),
            ("pair", {"address": "phone.local:5556", "pair_code": "654321"}),
            ("disconnect", {"address": "phone.local:5555"}),
        ):
            result = self.call(name, args)
            self.assertTrue(result["ok"], result)
            self.assertNotIn("654321", json.dumps(result))
        serialized = json.dumps(self.get("/api/history"))
        self.assertNotIn("pair_code", serialized)
        self.assertNotIn("654321", serialized)

    def test_zoom_schema_has_one_tool_with_explicit_direction(self):
        tools = {tool["name"]: tool for tool in self.get("/api/tools")["tools"]}
        self.assertNotIn("zoom_in", tools)
        self.assertNotIn("zoom_out", tools)
        schema = tools["zoom"]["inputSchema"]
        self.assertTrue({"x", "y", "direction"} <= set(schema["required"]))
        self.assertEqual(schema["properties"]["direction"]["enum"], ["in", "out"])
        for direction in (None, "raise", "lower"):
            args = {"x": 500, "y": 900}
            if direction is not None:
                args["direction"] = direction
            result = self.call("zoom", args)
            self.assertFalse(result.get("ok") and not result.get("result", {}).get("isError"), result)

    def test_set_screen_schema_has_one_tool_with_explicit_state(self):
        tools = {tool["name"]: tool for tool in self.get("/api/tools")["tools"]}
        self.assertIn("screen_status", tools)
        self.assertNotIn("wake_screen", tools)
        self.assertNotIn("sleep_screen", tools)
        schema = tools["set_screen"]["inputSchema"]
        self.assertIn("state", schema["required"])
        self.assertEqual(schema["properties"]["state"]["enum"], ["awake", "asleep"])
        for state in (None, "present", "gone", "enabled"):
            args = {} if state is None else {"state": state}
            result = self.call("set_screen", args)
            self.assertFalse(result.get("ok") and not result.get("result", {}).get("isError"), result)

    def test_navigation_has_one_press_key_tool_with_complete_key_enum(self):
        tools = {tool["name"]: tool for tool in self.get("/api/tools")["tools"]}
        self.assertNotIn("press_home", tools)
        self.assertNotIn("press_back", tools)
        schema = tools["press_key"]["inputSchema"]
        self.assertIn("key", schema["required"])
        self.assertEqual(set(schema["properties"]["key"]["enum"]), set(KEY_MAP))

    def test_logs_schema_and_both_kinds_use_one_tool(self):
        tools = {tool["name"]: tool for tool in self.get("/api/tools")["tools"]}
        self.assertNotIn("crash_logs", tools)
        self.assertIn("clear_logs", tools)
        schema = tools["logs"]["inputSchema"]
        self.assertEqual(schema["properties"]["kind"]["enum"], ["standard", "crash"])
        self.assertEqual(schema["properties"]["kind"]["default"], "standard")
        result = self.call("logs", {"kind": "unknown"})
        self.assertFalse(result.get("ok") and not result.get("result", {}).get("isError"), result)
        address = "phone.local:5555"
        self.assertTrue(self.call("connect", {"address": address})["ok"])
        try:
            for kind, filters in (("standard", {}), ("crash", {"name": "com.example.app", "match": "broken"})):
                arguments = {"target": address, **filters}
                if kind == "crash":
                    arguments["kind"] = kind
                response = self.call("logs", arguments)
                self.assertTrue(response["ok"], response)
                result = response["result"]["structuredContent"]
                self.assertEqual((result["action"], result["kind"]), ("logs", kind))
                self.assertEqual(result["buffers"], ["crash"] if kind == "crash" else ["main", "system", "crash"])
                self.assertIn("Code.java:20", result["text"])
                self.assertEqual(result["line_count"], 4)
        finally:
            self.call("disconnect", {"address": address})

    def test_pair_validation_and_adb_errors_never_echo_input_codes(self):
        for args, code in (
            ({"pair_code": "654321"}, "654321"),
            ({"address": "phone.local:5556", "pair_code": 654321}, "654321"),
            ({"address": {}, "pair_code": "654321"}, "654321"),
            ({"address": "--bad-option", "pair_code": "654321"}, "654321"),
            ({"address": "phone.local:5556", "pair_code": "12345"}, "12345"),
            ({"address": "phone.local:5556", "pair_code": "111111"}, "111111"),
        ):
            with self.subTest(args=args):
                result = self.call("pair", args)
                self.assertFalse(result.get("ok") and not result.get("result", {}).get("isError"), result)
                self.assertNotIn(code, json.dumps(result))
                self.assertNotIn(code, json.dumps(self.get("/api/history")))


if __name__ == "__main__":
    unittest.main()
