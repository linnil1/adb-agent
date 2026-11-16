---
name: android-device-control
description: Inspect and control authorized Android devices over ADB via CLI or MCP for screens, sound, gestures, apps, recordings, logs, and connections.
---

# Android Device Control

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [arguments]
```

Results are JSON with device identifiers in `target`; errors go to stderr. MCP names use underscores; for example, `press-key` becomes `press_key`.

Run `list-targets` first. With several devices, specify `--target`; `set-default-target` remembers one for an hour. Other actions preserve that default. Preserve app sessions; force-restart only for a requested clean start or a stuck app.

Global: `--timeout SECONDS` (default 30), `--adb PATH`, `--direct`. CLI forwards to a verified local server when available; `--direct` or `--adb` bypasses discovery. Forwarded ADB uses the server's timeout. Never retry a failed forwarded mutation directly.

## Arguments

Coordinates are device pixels; gesture durations are milliseconds. Pass only fields for the selected action.

| Action | Required arguments | Optional arguments |
|---|---|---|
| `list-targets`, `status`, `list-packages`, `current-focus` | none | none |
| `set-default-target` | `--target TARGET` | none |
| `connect`, `disconnect` | `--address HOST:PORT` | none |
| `pair` | `--address HOST:PAIRING_PORT` | `--pair-code CODE` (six digits; interactive CLI prompts if omitted) |
| `screen-status` | none | none |
| `set-screen` | `--state awake\|asleep` | none |
| `rotate-screen` | `--rotation 0\|90\|180\|270\|auto` | none |
| `get-dnd` | none | none |
| `set-dnd` | `--dnd-mode off\|priority\|alarms\|none` | none |
| `get-volume` | none | `--stream media\|ring\|notification\|alarm\|voice-call` |
| `set-volume` | At least one of `--level INT`, `--muted true\|false`, `--direction raise\|lower`, `--ringer-mode normal\|vibrate\|silent` | `--stream STREAM` (default media); level and direction are mutually exclusive |
| `screenshot` | none | `--output PATH` (default `android-screen.png`); `--x1 INT --y1 INT --x2 INT --y2 INT` region |
| `describe-screen` | none | `--format json\|original` (default `json`) |
| `wait-for-ui` | At least one exact, nonempty `--text TEXT`, `--description TEXT`, `--resource-id ID` | `--state present\|gone\|enabled` (default present); `--wait-timeout SECONDS` (default 30, greater than 0 and at most 300) |
| `tap` | `--x INT --y INT` | none |
| `swipe` | `--x1 INT --y1 INT --x2 INT --y2 INT` | `--duration INT` (default 300) |
| `multi-touch` | `--fingers JSON` | `--duration INT` (default 300, range 1-3000) |
| `zoom` | `--x INT --y INT` center; `--direction in\|out` | `--distance INT` (default 150); `--duration INT` (default 500, range 1-3000) |
| `long-press` | `--x INT --y INT` | `--duration INT` (default 1000, positive) |
| `type-text` | `--text STRING` (printable ASCII) | none |
| `press-key` | `--key KEY` (choices below) | none |
| `launch-app` | `--name PACKAGE_OR_FRAGMENT` | `--force-restart` |
| `force-stop-app` | `--name PACKAGE_OR_FRAGMENT` | none |
| `open-url` | `--url HTTP_OR_HTTPS_URL` | none |
| `start-screen-record` | `--time-limit SECONDS` (1-180) | none |
| `stop-screen-record` | none | `--output PATH` (default `android-screen.mp4`) |
| `logs` | none | `--kind standard\|crash` (default standard); `--name PACKAGE_OR_FRAGMENT --priority V\|D\|I\|W\|E\|F --match TEXT --lines INT --output PATH` |
| `clear-logs` | none | none |

`--fingers` is a JSON array of 2-10 paths, each exactly `{"x1":INT,"y1":INT,"x2":INT,"y2":INT}`; MCP takes the array directly. Coordinates must be 0-65535 and inside the current logical display. All gestures check display bounds; zoom reads dimensions without a screenshot. Multi-touch reuses a helper cached on-device by content version and removes the legacy unversioned helper once its replacement is available. Verify pinch results with a screenshot.

`press-key --key` accepts these values (MCP: `press_key(key=...)`):

- Navigation/editing: `home`, `back`, `up`, `down`, `left`, `right`, `tab`, `enter`, `delete`, `recents`.
- Volume: `volume-up`, `volume-down`, `volume-mute`.
- Media: `media-play`, `media-pause`, `media-play-pause`, `media-next`, `media-previous`.

Use `type-text --text` for ordinary text; send Enter, Tab, and arrow keys with `press-key`. `delete` means Backspace. Home and Back use `press-key --key home` and `press-key --key back`; there are no standalone Home/Back actions. To type and then submit:

```bash
python3 scripts/android_control.py --action type-text --target TARGET --text "search terms"
python3 scripts/android_control.py --action press-key --target TARGET --key enter
```

## Usage notes

- Connections use `--address HOST:PORT` or `[IPv6]:PORT`, without `--target` or an existing device connection. Addresses, including `localhost`, resolve on the ADB server host. Pairing and connection ports can differ; MCP requires `pair_code`, while interactive CLI can prompt without echo.
- Use accessibility element centers for taps and bounds for screenshot regions; `x2,y2` are exclusive. Regions require Android toybox's `od`, `tail`, `head`, and `gzip`. Accessibility can omit WebView/canvas/video content; use screenshots when needed. Waking may leave the phone locked.
- `wait-for-ui` combines selectors with AND and returns matching accessibility elements without tapping. Rotation angles are relative to the device's natural orientation; apps may constrain the observed orientation.
- `get-volume` retains global audio status when filtering streams. Read native index ranges before setting a level. Muted streams can report effective zero below the settable minimum; unmuting restores the remembered level. Ringer mode is global; `--stream` applies only to volume/mute operations. Ringer-only changes leave media untouched. Stream changes return that stream; ringer changes return all five. `supported_commands` lists native Android audio/notification subcommands, including `set_dnd`.
- DND modes: `off` disables DND, `priority` follows the existing policy, `alarms` allows alarms only, and `none` blocks interruptions. MCP's setter argument is `mode`, matching CLI `--dnd-mode`. Android may link DND, ringer, and stream mute states.
- Sound setters return observed status with `requested` and `verified`. Combined volume requests apply ringer, level/direction, then mute, without rollback. On verification failure, inspect state before retrying; earlier steps may have applied. Volume/media keys confirm dispatch only, so prefer volume tools for verified changes.
- Collect recordings with `stop-screen-record`. Starting a new recording discards a finished, uncollected recording; a running recording blocks another start.
- `logs --kind standard` (default) reads main/system/crash buffers; `--name` selects running package processes, including `:service` children. `logs --kind crash` reads the crash buffer and can match retained reports after exit by exact package; package/text filters retain report and stack context. `--match` is literal and priority is a minimum (default V). Output defaults to 200 lines (`--lines` 1-5000), capped at 256 KiB; `truncated` marks output limits (which may cut a report) and `scan_truncated` marks discarded data beyond the latest 4 MiB. CLI `--output` saves on the client, including forwarded calls. MCP uses `logs(kind="standard", ...)` or `logs(kind="crash", ...)` with the same filters, defaults, and optional target, without an output path. Only the separate `clear-logs` / MCP `clear_logs` clears buffers.
- App matching prefers the full package name, then an exact final segment, then a substring. For several strongest matches, `launch-app` prefers a unique launchable package; several launchable candidates remain ambiguous. Localized labels are not searched. Launching resolves the launcher activity and uses `am start -W`, preserving rotation settings. Results include `component` and observed `focus` (nullable); check `focus.source` because `mFocusedApp` can be stale.
- `type-text` preserves literal `%s` and accepts printable ASCII only. Use `press-key` for Enter/Tab and an appropriate IME for non-ASCII text. For unsupported operations, use ADB on the same host with explicit `-s TARGET`.
- Battery `status` and `health` retain Android codes; `status_label` and `health_label` provide readable values.

```bash
python3 scripts/android_control.py --action logs --target TARGET --name com.example.app --priority W --lines 200
python3 scripts/android_control.py --action logs --target TARGET --kind crash --name com.example.app --output crash.log
```

## MCP

Install `requirements-mcp.txt`, then run:

```bash
python3 scripts/android_control.py --mcp [--host HOST] [--port PORT]
```

Defaults: `127.0.0.1:8000`; Streamable HTTP `/mcp`, dashboard `/`. Screenshots return images; stopped recordings return video URLs.
