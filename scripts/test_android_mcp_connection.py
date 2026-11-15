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
