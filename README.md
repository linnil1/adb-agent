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
| `screen-status` | Read default-display power, interactive state, logical size, and rotation. |
| `wake-screen` / `sleep-screen` | Explicitly wake/sleep and verify interactivity. |
| `rotate-screen` | Set `--rotation 0\|90\|180\|270\|auto`, relative to natural orientation. |
| `list-packages` | List installed application packages. |
| `current-focus` | Report the activity currently holding window focus. |
| `screenshot` | Capture the display, or one `x1,y1,x2,y2` region, to a validated PNG file. |
| `describe-screen` | Return parsed accessibility elements (including resource IDs) or the original UI XML. |
| `wait-for-ui` | Wait for exact text, description, or resource-ID selectors to be present, gone, or enabled. |
| `tap` | Tap one screen coordinate. |
| `swipe` | Swipe between two coordinates. |
| `multi-touch` | Move 2-10 fingers together for pinch/zoom or multi-finger swipes. |
| `zoom-in` / `zoom-out` | Pinch around `--x --y`; `--distance` is each finger's travel in pixels (default 150). |
| `long-press` | Hold one screen coordinate. |
| `type-text` | Enter text through Android input. |
| `press-key` | Send navigation, editing, Enter, Home, Back, or Recents keys. |
| `home` | Navigate to the Android home screen. |
| `back` | Navigate back. |
| `launch-app` | Launch an installed package, optionally forcing a restart. |
| `force-stop-app` | Force-stop an installed package. |
| `open-url` | Open an HTTP or HTTPS URL on the device. |
| `start-screen-record` | Start a background screen recording with a required time limit (1-180 seconds). |
| `stop-screen-record` | Stop the recording and save the MP4. |
| `logs` | Capture recent device or running-app logs, with priority/text filters. |
| `crash-logs` | Capture retained crash reports, including exited apps. |
| `clear-logs` | Explicitly clear main, system, and crash log buffers. |

Log capture returns bounded text and metadata; it never clears buffers. For example:

```bash
python3 scripts/android_control.py --action logs --name com.example.app --priority W --lines 200
python3 scripts/android_control.py --action crash-logs --name com.example.app --output crash.log
```

`--match` is a literal text filter. Crash filters select reports with their stack context; line/byte limits may cut a report and set `truncated`. App logs resolve currently running package processes; crash matching also works after the app exits. Output defaults to 200 physical lines, at most 5000 lines/256 KiB; capture retains the latest 4 MiB before filtering and reports `scan_truncated` if older bytes were discarded. The CLI saves `--output` on the client machine even when forwarded through the server. MCP exposes `logs`, `crash_logs`, and `clear_logs`.

Connection actions work before a device is connected and run on the same host as the MCP server's ADB. Thus `localhost` is the server's localhost. Use an explicit `--address HOST:PORT` (or `[IPv6]:PORT`) rather than `--target`. Pairing uses the phone's pairing port, which can differ from its connection port:

```bash
python3 scripts/android_control.py --action connect --address phone.local:5555
python3 scripts/android_control.py --action disconnect --address phone.local:5555
python3 scripts/android_control.py --action pair --address phone.local:37000
```

Interactive CLI pairing prompts for the code without echo; automation and MCP supply `pair_code` (CLI `--pair-code`). The code is sent to ADB via stdin, omitted from tool history/results, and masked in dashboard forms. Pairing doesn't imply a ready connection; call `connect` afterward. These actions preserve the remembered default target.

Selector waits search the current accessibility tree without navigating or tapping. Combine `--text`, `--description`, and `--resource-id` with AND; use `--state present|gone|enabled` and `--wait-timeout` (default 30 seconds, maximum 300). Failed or malformed captures never count as disappearance. MCP exposes `wait_for_ui`; successful results include matching elements, coordinates, attempt count, and elapsed time.

```bash
python3 scripts/android_control.py --action wait-for-ui --text "Ride history" --wait-timeout 20
python3 scripts/android_control.py --action wait-for-ui --resource-id com.example:id/loading --state gone
```

Screen controls use standard ADB commands and are also available as `screen_status`, `wake_screen`, `sleep_screen`, and `rotate_screen`. Unknown status fields are null/`unknown`. Wake and sleep verify the result for up to five seconds; waking can leave the phone locked. Rotation reports both `setting_applied` and `rotation_matches`, because an app can constrain orientation. For example:

```bash
python3 scripts/android_control.py --action screen-status
python3 scripts/android_control.py --action rotate-screen --rotation 90
python3 scripts/android_control.py --action rotate-screen --rotation auto
```

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
python3 scripts/android_control.py --action list-targets
```
