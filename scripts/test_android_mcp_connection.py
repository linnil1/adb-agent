"""Four daemon scenarios using real HTTP/MCP and a strict fake ADB executable."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

from scripts.android_core import ACTIONS, KEY_MAP, action_args, encode_png
from scripts import android_client as client
from scripts.test_support import python_command


FAKE_ADB = r'''
import json
import sys
from pathlib import Path
root = Path(__file__).parent
args = sys.argv[1:]
with (root / 'commands.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
devices = root / 'devices'
if args == ['devices', '-l']:
    print('List of devices attached')
    if devices.exists(): print(devices.read_text() + ' device')
elif args[0] == 'connect':
    devices.write_text(args[1])
    print('connected to ' + args[1])
elif args[0] == 'disconnect':
    devices.unlink(missing_ok=True)
    print('disconnected ' + args[1])
elif args[0] == 'pair':
    code = sys.stdin.readline().strip()
    if code == '654321': print('Successfully paired to ' + args[1])
    else:
        print('invalid pairing code ' + code, file=sys.stderr)
        sys.exit(1)
elif args[:2] == ['-s', 'phone.local:5555']:
    args = args[2:]
    fault = (root / 'fault').read_text() if (root / 'fault').exists() else ''
    if fault == 'input' and args[:2] == ['shell', 'input']:
        sys.exit('injected input failure')
    if args == ['get-state']: print('device')
    elif args[:2] == ['shell', 'getprop']: print('fixture')
    elif args == ['shell', 'dumpsys', 'battery']: print('level: 80\nstatus: 5\nhealth: 2')
    elif args == ['shell', 'dumpsys', 'display']:
        print('mDisplayId=0\nmOverrideDisplayInfo=DisplayInfo{real 1080 x 1920, rotation 0, state ON}')
    elif args == ['shell', 'pm', 'list', 'packages']: print('package:com.example.app')
    elif args == ['shell', 'ps', '-A', '-o', 'PID,NAME']: print('123 com.example.app')
    elif args[:4] == ['shell', 'cmd', 'package', 'resolve-activity']:
        print('com.example.app/.MainActivity')
    elif args == ['shell', 'dumpsys', 'window']:
        print('mCurrentFocus=Window{123 u0 com.example.app/.MainActivity}')
    elif args[0] == 'logcat': sys.stdout.write((root / 'logcat.txt').read_text())
    elif args == ['shell', 'logcat', '-b', 'main,system,crash', '-c']: pass
    elif args == ['exec-out', 'screencap', '-p']:
        sys.stdout.buffer.write(b'broken' if fault == 'screenshot' else (root / 'screen.png').read_bytes())
    elif args[0] == 'exec-out' and 'android-control-screen-' in args[1]:
        sys.stdout.buffer.write(b'1080 1920 1 16\n' + (root / 'rows.gz').read_bytes())
    elif args == ['exec-out', 'uiautomator', 'dump', '/dev/tty']:
        print('<hierarchy><node text="Ready" resource-id="app:id/ready" bounds="[0,0][100,40]" enabled="true"/></hierarchy>')
    elif args[0] == 'shell' and 'nohup screenrecord' in args[1]:
        (root / 'recording').touch()
        print('started')
    elif args[0] == 'exec-out' and 'kill -INT' in args[1]:
        if (root / 'recording').exists():
            if fault == 'slow-stop':
                import time
                time.sleep(1.2)
            sys.stdout.buffer.write(b'stopped\n' + (root / 'recording.mp4').read_bytes())
        else: print('none')
    elif args[:2] == ['shell', 'input'] or args[0] == 'push': pass
    elif args[0] == 'shell' and args[1].startswith('[ -s '): pass
    elif args == ['shell', 'rm', '-f', '/data/local/tmp/android-control-multitouch.dex']: pass
    elif args[0] == 'shell' and args[1].startswith('input text '): pass
    elif args[0] == 'shell' and args[1].startswith('CLASSPATH='): pass
    elif args[:3] in (['shell', 'am', 'force-stop'], ['shell', 'am', 'start']): pass
    else: sys.exit('unhandled fixture command: ' + repr(args))
else: sys.exit('unhandled fixture command: ' + repr(args))
'''


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'requires optional MCP SDK')
class MCPConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.adb = cls.root / 'adb'
        # Use the test interpreter, not whichever Python happens to be on PATH.
        import sys
        cls.adb.write_text(f'#!{sys.executable}\n' + FAKE_ADB)
        cls.adb.chmod(0o755)
        cls.png = encode_png(1080, 1920, [bytes([10, 20, 30, 255]) * 1080] * 1920)
        (cls.root / 'screen.png').write_bytes(cls.png)
        (cls.root / 'rows.gz').write_bytes(zlib.compress(bytes([10, 20, 30, 255]) * 1080 * 2, wbits=31))
        def box(kind, body):
            return (len(body) + 8).to_bytes(4, 'big') + kind + body
        cls.mp4 = box(b'ftyp', b'isom' + bytes(4)) + box(b'mdat', b'fixture-frame') + box(
            b'moov', box(b'mvhd', bytes(12) + (1000).to_bytes(4, 'big') + (4580).to_bytes(4, 'big') + bytes(80)),
        )
        (cls.root / 'recording.mp4').write_bytes(cls.mp4)
        (cls.root / 'logcat.txt').write_text(''.join(
            f'10-15 17:53:34.130   123   123 E AndroidRuntime: {message}\n'
            for message in ('FATAL EXCEPTION: main', 'Process: com.example.app, PID: 123',
                            'java.lang.IllegalStateException: broken', '    at com.example.Code.run(Code.java:20)')
        ))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{port}'
        cls.opener = build_opener(ProxyHandler({}))
        cls.log = (cls.root / 'server.log').open('w')
        cls.addClassCleanup(cls.log.close)
        cls.environment = dict(os.environ, ANDROID_CONTROL_SERVER_FILE=str(cls.root / 'discovery.json'),
                               ANDROID_CONTROL_STATE_FILE=str(cls.root / 'state.json'))
        cls.environment.pop('ANDROID_SERIAL', None)
        cls.process = subprocess.Popen(
            python_command(Path(__file__).with_name('android_control.py'),
                           '--mcp', '--port', port, '--adb', cls.adb),
            stdout=cls.log, stderr=cls.log, env=cls.environment,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                cls.get('/api/info')
                break
            except (OSError, URLError):
                if cls.process.poll() is not None:
                    raise RuntimeError((cls.root / 'server.log').read_text())
                time.sleep(0.05)
        else:
            raise RuntimeError('MCP test server did not start')

    @classmethod
    def stop_server(cls):
        cls.process.send_signal(signal.SIGINT)
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)
            raise
        if (cls.root / 'discovery.json').exists():
            raise AssertionError('daemon did not remove discovery file during shutdown')

    def setUp(self):
        for name in ('devices', 'fault', 'recording'):
            (self.root / name).unlink(missing_ok=True)

    @classmethod
    def request(cls, path, *, method='GET', body=None):
        request = Request(cls.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'}, method=method)
        try:
            return cls.opener.open(request, timeout=5)
        except HTTPError as error:
            return error

    @classmethod
    def get(cls, path):
        with cls.request(path) as response:
            return json.load(response)

    def call(self, name, arguments=None):
        with self.request('/api/tools/call', method='POST', body={
            'name': name, 'arguments': arguments or {},
        }) as response:
            return json.load(response)

    def result(self, name, arguments=None):
        response = self.call(name, arguments)
        self.assertTrue(response['ok'], response)
        self.assertFalse(response['result'].get('isError'), response)
        return response['result'].get('structuredContent')

    def viewer_events(self, count=1, after=None):
        path = '/api/viewer/events' + (f'?after={after}' if after is not None else '')
        events = []
        with self.request(path) as response:
            while len(events) < count:
                line = response.readline().decode()
                if not line:
                    self.fail('viewer stream ended before expected events')
                if line.startswith('data: '):
                    events.append(json.loads(line[6:]))
        return events

    def test_tool_contract_matches_skill(self):
        tools = {tool['name']: tool for tool in self.get('/api/tools')['tools']}
        skill = Path(__file__).resolve().parents[1].joinpath('SKILL.md').read_text()
        # Compare the documented action column, including grouped actions.
        documented = set()
        for line in skill.splitlines():
            if line.startswith('| `'):
                documented.update(re.findall(r'`([a-z][a-z-]*)`', line.split('|')[1]))
        self.assertEqual(set(tools), {action.replace('-', '_') for action in documented})
        self.assertEqual(documented, set(ACTIONS))
        for name, field, values, required in (
            ('zoom', 'direction', ['in', 'out'], True),
            ('set_screen', 'state', ['awake', 'asleep'], True),
            ('press_key', 'key', list(KEY_MAP), True),
            ('logs', 'kind', ['standard', 'crash'], False),
        ):
            with self.subTest(tool=name):
                schema = tools[name]['inputSchema']
                self.assertEqual(set(schema['properties'][field]['enum']), set(values))
                self.assertEqual(field in schema.get('required', []), required)
                if name == 'logs':
                    self.assertEqual(schema['properties'][field]['default'], 'standard')
                invalid = self.call(name, {field: 'invalid'})
                self.assertFalse(invalid.get('ok') and not invalid.get('result', {}).get('isError'), invalid)

    def test_protocol_workflow_and_cli_forwarding(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        import httpx2

        async def workflow():
            async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
                async with streamable_http_client(self.url + '/mcp', http_client=http) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=5) as session:
                        initialized = await session.initialize()
                        self.assertTrue(initialized.server_info.name)
                        listed = await session.list_tools()
                        self.assertEqual({tool.name for tool in listed.tools}, {
                            tool['name'] for tool in self.get('/api/tools')['tools']
                        })
                        connected = await session.call_tool('connect', {'address': 'phone.local:5555'})
                        self.assertFalse(connected.is_error)
                        self.assertTrue(connected.structured_content['ready'])
                        targets = await session.call_tool('list_targets')
                        self.assertFalse(targets.is_error)
                        self.assertIn('phone.local:5555', json.dumps(targets.model_dump()))
                        for kind in ('standard', 'crash'):
                            arguments = {'name': 'com.example.app', 'match': 'broken'}
                            if kind == 'crash':
                                arguments['kind'] = kind
                            result = await session.call_tool('logs', arguments)
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content['kind'], kind)
                            self.assertIn('broken', result.structured_content['text'])
                            self.assertEqual(result.structured_content['buffers'],
                                             ['crash'] if kind == 'crash' else ['main', 'system', 'crash'])
                            if kind == 'crash':
                                self.assertEqual(result.structured_content['line_count'], 4)
                                self.assertIn('Code.java:20', result.structured_content['text'])
                        tapped = await session.call_tool('tap', {'x': 25, 'y': 40})
                        self.assertFalse(tapped.is_error)
                        self.assertEqual(tapped.structured_content['point'], [25, 40])
                        error = await session.call_tool('tap', {'x': -1, 'y': 40})
                        self.assertTrue(error.is_error)
        asyncio.run(workflow())

        # Exercise other dispatch shapes through the same registered handlers.
        for name, arguments, field, expected in (
            ('set_default_target', {'target': 'phone.local:5555'}, 'target', 'phone.local:5555'),
            ('status', {}, 'state', 'device'),
            ('list_packages', {}, 'packages', ['com.example.app']),
            ('current_focus', {}, 'package', 'com.example.app'),
            ('swipe', {'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}, 'to', [3, 4]),
            ('long_press', {'x': 1, 'y': 2}, 'point', [1, 2]),
            ('multi_touch', {'fingers': [{'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}] * 2}, 'duration_ms', 300),
            ('zoom', {'x': 500, 'y': 900, 'direction': 'in'}, 'direction', 'in'),
            ('type_text', {'text': 'hello world'}, 'characters', 11),
            ('press_key', {'key': 'home'}, 'key', 'home'),
            ('launch_app', {'name': 'example', 'force_restart': True}, 'force_restarted', True),
            ('force_stop_app', {'name': 'example'}, 'package', 'com.example.app'),
            ('open_url', {'url': 'https://example.com'}, 'action', 'open-url'),
            ('clear_logs', {}, 'buffers', ['main', 'system', 'crash']),
        ):
            with self.subTest(tool=name):
                self.assertEqual(self.result(name, arguments)[field], expected)
        commands = [json.loads(line) for line in (self.root / 'commands.jsonl').read_text().splitlines()]
        self.assertIn(['-s', 'phone.local:5555', 'shell', 'input', 'tap', '25', '40'], commands)
        self.assertFalse(any('monkey' in command for command in commands))
        self.assertEqual(self.result('status')['battery']['status_label'], 'full')
        self.assertEqual(self.result('status')['battery']['health_label'], 'good')

        output = self.root / 'client.log'
        forwarded = subprocess.run(
            python_command(Path(__file__).with_name('android_control.py'), '--action', 'logs',
                           '--kind', 'crash', '--output', output),
            env=self.environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(forwarded.returncode, 0, forwarded.stderr)
        self.assertIn('Code.java:20', output.read_text())
        self.assertEqual(json.loads(forwarded.stdout)['kind'], 'crash')
        history = self.get('/api/history')['history']
        self.assertEqual(history[0]['action'], 'logs')
        self.assertTrue(history[0]['ok'])
        self.assertTrue(self.result('disconnect', {'address': 'phone.local:5555'})['disconnected'])

    def test_failures_and_pairing_secrets(self):
        self.assertTrue(self.result('pair', {'address': 'phone.local:5556', 'pair_code': '654321'})['paired'])
        for args, code in (
            ({'pair_code': '654321'}, '654321'),
            ({'address': 'phone.local:5556', 'pair_code': 654321}, '654321'),
            ({'address': {}, 'pair_code': '654321'}, '654321'),
            ({'address': '--bad-option', 'pair_code': '654321'}, '654321'),
            ({'address': 'phone.local:5556', 'pair_code': '12345'}, '12345'),
            ({'address': 'phone.local:5556', 'pair_code': '111111'}, '111111'),
        ):
            with self.subTest(arguments=args):
                result = self.call('pair', args)
                self.assertFalse(result.get('ok') and not result.get('result', {}).get('isError'), result)
                self.assertNotIn(code, json.dumps(result))
                self.assertNotIn(code, json.dumps(self.get('/api/history')))
        self.result('connect', {'address': 'phone.local:5555'})
        (self.root / 'fault').write_text('input')
        result = self.call('tap', {'x': 1, 'y': 2})
        self.assertFalse(result['ok'], result)
        self.assertIn('injected input failure', result['error'])
        history = self.get('/api/history')['history']
        self.assertEqual(history[0]['action'], 'tap')
        self.assertFalse(history[0]['ok'])
        serialized = json.dumps(history)
        self.assertNotIn('pair_code', serialized)
        self.assertNotIn('654321', serialized)
        self.assertNotIn('111111', (self.root / 'server.log').read_text())
        for body in ([], {}, {'name': 'logs', 'arguments': []}, {'name': 'no_such_tool'}):
            with self.subTest(body=body), self.request('/api/tools/call', method='POST', body=body) as response:
                self.assertEqual(response.status, 400)
                self.assertFalse(json.load(response)['ok'])

    def test_media_lifecycle_and_viewer_cache(self):
        self.result('connect', {'address': 'phone.local:5555'})
        with self.request('/') as dashboard:
            self.assertEqual(dashboard.status, 200)
            self.assertIn(b'Android', dashboard.read())
        image = self.call('screenshot')
        self.assertTrue(image['ok'], image)
        content = image['result']['content'][0]
        self.assertEqual(content['type'], 'image')
        self.assertNotIn('data', content)
        self.assertTrue(content['cachedForViewer'])
        metadata = image['viewer']
        query = urlencode({'target': metadata['target'], 'revision': metadata['revision']})
        with self.request('/api/viewer/screenshot?' + query) as response:
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertEqual(response.read(), self.png)
        self.result('describe_screen')
        self.result('wait_for_ui', {'text': 'Ready', 'state': 'enabled', 'wait_timeout': 2})
        crop = self.call('screenshot', {'x1': 1, 'y1': 0, 'x2': 3, 'y2': 2})
        self.assertEqual(crop['viewer']['region'], [1, 0, 3, 2])
        state = self.viewer_events()[0]
        self.assertEqual(state['screenshots'][0]['revision'], metadata['revision'])
        self.assertEqual(state['crops'][0]['revision'], crop['viewer']['revision'])
        self.assertEqual(state['descriptions'][0]['elements'][0]['text'], 'Ready')
        self.assertNotIn('png', json.dumps(state))

        self.result('start_screen_record', {'time_limit': 30})
        self.assertEqual(self.viewer_events()[0]['active_recordings'][0]['time_limit'], 30)
        video = self.result('stop_screen_record')
        self.assertEqual(video['seconds'], 4.58)
        self.assertTrue(video['url'].startswith('/api/viewer/recording?'))
        with self.opener.open(self.url + video['url'], timeout=5) as response:
            self.assertEqual(response.headers['Content-Type'], 'video/mp4')
            self.assertEqual(response.read(), self.mp4)
        self.assertTrue((self.root / 'recording').exists())
        # A capture longer than the old 1-second HTTP floor is still collected.
        (self.root / 'fault').write_text('slow-stop')
        saved = self.root / 'retried.mp4'
        result = client.execute_via_server(action_args('stop-screen-record', target='phone.local:5555',
                                                       timeout=0.1, output=str(saved)), self.url)
        self.assertEqual(result['seconds'], 4.58)
        self.assertEqual(saved.read_bytes(), self.mp4)
        (self.root / 'fault').unlink()
        # A separately configured public origin must never redirect local CLI downloads.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            public_port = sock.getsockname()[1]
        public_origin = 'https://review.example:40000/phone'
        local_url = f'http://127.0.0.1:{public_port}'
        environment = dict(self.environment, ANDROID_CONTROL_SERVER_FILE=str(self.root / 'public-discovery.json'))
        process = subprocess.Popen(
            python_command(Path(__file__).with_name('android_control.py'), '--mcp', '--port', public_port,
                           '--adb', self.adb, '--public-base-url', public_origin),
            env=environment, stdout=self.log, stderr=self.log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with self.opener.open(local_url + '/api/info', timeout=1):
                        break
                except (OSError, URLError):
                    if time.monotonic() >= deadline or process.poll() is not None:
                        self.fail('public-base-url daemon did not start')
                    time.sleep(0.05)
            request = Request(local_url + '/api/tools/call', method='POST', headers={
                'Content-Type': 'application/json', 'Host': 'review.example:40000',
            }, data=json.dumps({'name': 'stop_screen_record', 'arguments': {'target': 'phone.local:5555'}}).encode())
            with self.opener.open(request, timeout=5) as response:
                public_result = json.load(response)['result']['structuredContent']
            self.assertTrue(public_result['url'].startswith(public_origin + '/api/viewer/recording?'))
            client.execute_via_server(action_args('stop-screen-record', target='phone.local:5555',
                                                  output=str(saved)), local_url)
            self.assertEqual(saved.read_bytes(), self.mp4)
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
        state = self.viewer_events()[0]
        self.assertEqual(state['active_recordings'], [])
        self.assertEqual(state['recordings'][0]['bytes'], len(self.mp4))
        self.assertNotIn('mp4', json.dumps(state))
        replay = self.viewer_events(count=2, after=0)
        self.assertEqual(replay[0]['type'], 'state')
        self.assertIn(replay[1]['type'], {'history', 'screenshot', 'description', 'gesture', 'recording', 'cleared'})

        # Interactive capture updates the cache without adding command history.
        before = self.get('/api/history')
        with self.request('/api/viewer/screenshot', method='POST', body={'target': 'phone.local:5555'}) as response:
            interactive = json.load(response)
        self.assertTrue(interactive['ok'])
        self.assertEqual(self.get('/api/history'), before)
        with self.request('/api/viewer/screenshot?' + query) as response:
            self.assertEqual(response.status, 409)

        for kind, state_field in (('screenshot', 'screenshots'), ('description', 'descriptions'), ('recording', 'recordings')):
            with self.subTest(clear=kind), self.request('/api/viewer/cache', method='DELETE', body={
                'target': 'phone.local:5555', 'kind': kind,
            }) as response:
                self.assertTrue(json.load(response)['ok'])
            self.assertEqual(self.viewer_events()[0][state_field], [])
        for path, status in (
            ('/api/viewer/screenshot', 400),
            ('/api/viewer/screenshot?' + query, 404),
            ('/api/viewer/recording?target=phone.local:5555&revision=1', 404),
        ):
            with self.subTest(path=path), self.request(path) as response:
                self.assertEqual(response.status, status)
        for path, method, body in (
            ('/api/viewer/cache', 'DELETE', {'kind': 'unknown'}),
            ('/api/viewer/screenshot', 'POST', {}),
        ):
            with self.request(path, method=method, body=body) as response:
                self.assertEqual(response.status, 400)
        (self.root / 'fault').write_text('screenshot')
        self.assertFalse(self.call('screenshot')['ok'])
        (self.root / 'recording').unlink()
        self.assertFalse(self.call('stop_screen_record')['ok'])
        self.assertEqual(self.viewer_events()[0]['active_recordings'], [])


if __name__ == '__main__':
    unittest.main()
