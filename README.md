# Android Device Control

A Codex skill and Python CLI for inspecting and controlling an authorized Android phone or emulator over ADB. Core actions have no third-party Python dependencies, use named arguments, and return JSON:

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [action arguments]
```

See [`SKILL.md`](SKILL.md) for the argument schema and operational details.

## Actions

| Action | Summary |
|---|---|
| `list-targets` | List connected ADB devices and their states. |
| `connect` | Connect to `--address HOST:PORT` and verify device readiness. |
| `disconnect` | Disconnect only the explicit `--address HOST:PORT`. |
| `pair` | Pair with a wireless-debugging endpoint and six-digit code. |
| `set-default-target` | Remember one ready target for one hour. |
| `status` | Report device state, model, Android version, and battery information. |
| `screen-status` | Read default-display power, interactive state, and logical size. |
| `set-screen` | Set `--state awake\|asleep` with explicit wake/sleep keys and verify interactivity. |
| `rotate-screen` | Lock rotation to `0`, `90`, `180`, `270`, or enable `auto`. |
| `get-dnd` | Read Do Not Disturb, raw Zen mode, ringer modes, and command support. |
| `set-dnd` | Set `--dnd-mode off\|priority\|alarms\|none` and verify the observed mode. |
| `get-volume` | Read full audio status; optionally filter streams with `--stream`. |
| `set-volume` | Set volume, mute, direction, or ringer mode; stream defaults to media. |
| `list-packages` | List installed application packages. |
| `current-focus` | Report the activity currently holding window focus. |
| `screenshot` | Capture the display, or one `x1,y1,x2,y2` region, to a validated PNG file. |
| `describe-screen` | Return parsed accessibility elements (including resource IDs) or the original UI XML. |
| `wait-for-ui` | Wait for exact accessibility selectors to become present, gone, or enabled. |
| `tap` | Tap one screen coordinate. |
| `swipe` | Swipe between two coordinates. |
| `multi-touch` | Move 2-10 fingers together for pinch/zoom or multi-finger swipes. |
| `zoom` | Zoom `--direction in\|out` around `--x --y`; `--distance` is each finger's travel in pixels (default 150). |
| `long-press` | Hold one screen coordinate. |
| `type-text` | Enter printable ASCII text, preserving literal `%s`; use `press-key` for Enter/Tab. |
| `press-key` | Send navigation (including `--key home` and `--key back`), editing, volume, or media playback keys. |
| `launch-app` | Resolve and start the launcher activity, wait for startup, and return observed focus; optionally force a restart. |
| `force-stop-app` | Force-stop an installed package. |
| `open-url` | Open an HTTP or HTTPS URL on the device. |
| `start-screen-record` | Start a background screen recording with a required time limit (1-180 seconds). |
| `stop-screen-record` | Stop the recording and save the MP4. |
| `logs` | Capture recent logs (`--kind standard`, default) or retained crash reports (`--kind crash`), with priority/text filters. |
| `clear-logs` | Explicitly clear main, system, and crash log buffers. |

Log capture returns bounded text and metadata; it never clears buffers. For example:

```bash
python3 scripts/android_control.py --action logs --name com.example.app --priority W --lines 200
python3 scripts/android_control.py --action logs --kind crash --name com.example.app --output crash.log
```

`--kind standard` (default) reads main, system, and crash buffers; `--name` selects currently running package processes, including service children. `--kind crash` reads the crash buffer and matches retained reports even after an app exits. `--match` is a literal text filter; crash filters select reports with their stack context. Line/byte limits may cut a report and set `truncated`. Priority is a minimum (default V). Output defaults to 200 physical lines, at most 5000 lines/256 KiB; capture retains the latest 4 MiB before filtering and reports `scan_truncated` if older bytes were discarded. The CLI saves `--output` on the client machine even when forwarded through the server. MCP exposes `logs` with `kind="standard"` or `kind="crash"`, and the separate `clear_logs` tool.

Connection actions work before a device is connected and run on the same host as the MCP server's ADB. Thus `localhost` is the server's localhost. Use an explicit `--address HOST:PORT` (or `[IPv6]:PORT`) rather than `--target`. Pairing uses the phone's pairing port, which can differ from its connection port:

```bash
python3 scripts/android_control.py --action connect --address phone.local:5555
python3 scripts/android_control.py --action disconnect --address phone.local:5555
python3 scripts/android_control.py --action pair --address phone.local:37000
```

Interactive CLI pairing prompts for the code without echo; automation and MCP supply `pair_code` (CLI `--pair-code`). The code is sent to ADB via stdin, omitted from tool history/results, and masked in dashboard forms. Pairing doesn't imply a ready connection; call `connect` afterward. These actions preserve the remembered default target.

Screen state and power controls are available as `screen_status` and `set_screen` with `state="awake"` or `state="asleep"`.

App launches use `am start -W`, preserving the user's rotation setting. The result
includes the launcher `component` and observed `focus` (or null if unavailable).
Focus includes its source: `mCurrentFocus` is the current window, while
`mFocusedApp` can be stale. Package matching prefers a full package name, then an
exact final segment, then a substring. When several strongest matches remain,
`launch-app` prefers a unique launchable package; several launchable matches
remain ambiguous. Localized app labels are not searched.

Gestures validate coordinates against the default display's logical dimensions,
including rotation and overrides. Zoom reads dimensions without taking a
screenshot. Long presses require a positive duration. Multi-touch caches a
versioned helper in `/data/local/tmp`; a changed helper gets a new cache filename.
Once that helper is available, multi-touch removes the old unversioned
`android-control-multitouch.dex` left by earlier versions.

Ringer mode is global. `--stream` selects only volume/mute operations, including
when they are combined with a ringer change. `get-volume`'s `supported_commands`
contains native Android subcommand names: audio uses names such as `set-volume`,
and notification uses `set_dnd`. Battery `status` and `health` retain Android's
raw codes, with readable `status_label` and `health_label` alongside them.

Python callers can use `execute(action_args(...))` for every action. Callers with
an existing `Adb` instance can use `device_action(args, adb)` for device actions;
it applies the same defaults and validation. Host actions (`list-targets`,
`connect`, `disconnect`, `pair`) require `execute()`.

## Streamable HTTP MCP

Install the official Python MCP SDK and start the same program in MCP mode:

```bash
python3 -m pip install -r requirements-mcp.txt
python3 scripts/android_control.py --mcp
```

Default endpoints:

- MCP: `http://127.0.0.1:8000/mcp` (Streamable HTTP only)
- Dashboard: `http://127.0.0.1:8000/`
- Override the listener with `--host` and `--port`.
- Recording URLs are relative by default. Remote MCP clients can set
  `--public-base-url https://example.com/phone` to receive absolute download URLs
  with a proxy prefix. Local CLI downloads always use the verified daemon endpoint.

Execution pipeline:

```text
MCP client ───── Streamable HTTP /mcp ─┐
Local CLI ────── verified server call ──┼─→ registered tool ─→ ADB
Vue dashboard ─ schema-driven call ─────┘          │
                                                   └─→ history + viewer events
```

Local CLI discovery:

```text
private discovery file ─→ verify PID + /api/info
                          ├─ valid ─────→ use MCP tool handler
                          └─ unavailable→ use ADB directly
```

Use `--direct` or an explicit `--adb` path to bypass discovery.

Screenshot and visualization pipeline:

```text
explicit screenshot action ─→ ADB capture ─→ per-target cache ─→ viewer event
                                                              └─→ browser loads PNG

region screenshot ─→ raw screencap on device ─→ rows y1..y2, gzip ─→ crop x1..x2 ─→ PNG
                                                 (one adb call)       (host, stdlib)
                  └─→ cached as a crop over the last full screenshot

start_screen_record ─→ screenrecord detached on device ─→ viewer REC badge
stop_screen_record ──→ SIGINT, pull MP4 ─→ per-target cache ─→ viewer video + download URL

tap / swipe / long-press ─→ viewer event ─→ animation over cached screenshot
describe_screen ──────────→ viewer event ─→ accessibility bounds + labels
                                          (also works without a screenshot)
interactive mode ─────────→ 1-second per-target captures + mouse/keyboard input
```

Opening the dashboard loads Vue from a CDN, requires no Node.js build, discovers registered tool schemas and connected targets, and displays per-device command history. Interactive mode captures its target once per second without adding those background captures to command history; clicks, drags, holds, and keyboard input are sent through the normal device tools. Its browser event stream is only for viewer updates; MCP remains Streamable HTTP. On desktop, each device workspace has a maximum height of 900 px, with command history scrolling inside it.

Multi-touch uses a bundled Android helper run over ADB; no APK installation or root is needed. See [the skill's gesture schema](SKILL.md#gestures) for CLI and MCP usage. In dashboard interactive mode, use two fingers or hold **Alt** while dragging with a mouse: the second finger mirrors the first around the screen center. Gestures replay along straight paths when all fingers lift, with a maximum duration of three seconds. Restart an existing MCP server after upgrading to register new tools.

The multi-touch helper requires Android 6.0/API 23+. To rebuild `scripts/multitouch/multitouch.dex`, run `python3 scripts/multitouch/build.py` with a JDK and Android SDK platform/build-tools installed; use `JAVA_HOME` and `ANDROID_SDK_ROOT` (or `ANDROID_HOME`) to select them.

A region screenshot is drawn at its position over the dimmed last full screenshot, outlined, until the next full screenshot replaces it; without a full screenshot it is placed on a blank screen of the reported size. Description overlays, gesture animations, and interactive input keep using full-screen device coordinates.

While a recording runs, the device heading shows a **REC** badge. A stopped recording appears in a **Screen recording** panel with playback, **Download**, and **Clear recording**.

Recording size follows the current logical display orientation, scaled to at most
1280 pixels on the longer side. Stopping retains the completed device recording;
repeat `stop-screen-record` to retry a failed transfer or save. Starting the next
recording replaces that retained copy. CLI saves validate the MP4 and atomically
replace the requested output only after writing it successfully. Forwarded stop
and download requests allow at least the daemon's 300-second capture budget.
Clearing the dashboard recording removes its viewer cache; it does not delete
the retained device copy.

Without `--output`, screenshots and collected recordings save in the current
directory as `android-screen-YYYYMMDD-HHMMSS-ffffff.png` or `.mp4`, using local
time with microseconds. An explicit `--output PATH` uses that exact path.

Without a cached screenshot, `describe_screen` displays a standalone view of accessibility bounds and labels, with the canvas extent estimated from the elements. With a screenshot, the same elements overlay the image. Taking a new screenshot preserves the latest description until it is replaced by `describe_screen` or removed with **Clear description**.

Each device shows **Clear screenshot** and **Clear description** buttons only when the corresponding content exists. They remove only that device's selected viewer cache and synchronize open dashboards. Clearing the screenshot stops interactive capture and preserves any description; clearing the description preserves the screenshot. These controls do not modify the phone or command history.

## Dashboard preview

![Android Device Viewer dashboard with phone preview, tools, and command history](docs/android-dashboard-chatgpt.png)

## Attribution

This project is inspired by [benasbarciauskas/androir-mcp](https://github.com/benasbarciauskas/androir-mcp), an Apache-2.0-licensed TypeScript MCP server. This implementation provides a local Python CLI, Streamable HTTP MCP server, and build-free dashboard.

## License

MIT. See [LICENSE](LICENSE).

## Tests

```bash
python3 -m unittest discover -s scripts -p 'test_android_*.py'
node scripts/test_viewer.js
```

Tests use fake ADB and do not need or control a phone. The daemon tests require
`requirements-mcp.txt` in the Python environment running the tests; they otherwise
skip. They start one local daemon, exercise `/mcp` and the dashboard API, and stop
it afterward. CLI tests cover routing, startup, pairing prompts, and exit codes.

To measure Python line and branch coverage, install `coverage` in that same
environment and run:

```bash
python3 -m coverage erase
COVERAGE_PROCESS_START="$PWD/.coveragerc" python3 -m coverage run -m unittest discover -s scripts -p 'test_android_*.py'
python3 -m coverage combine
python3 -m coverage report
```

The test subprocess helper uses `COVERAGE_PROCESS_START` to include CLI and daemon
processes. The report excludes tests and the helper build script; it does not
measure dashboard JavaScript or the Android Java helper. For an optional manual
device check, run `python3 scripts/android_control.py --action list-targets`.
