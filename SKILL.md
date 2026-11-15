---
name: android-device-control
description: Inspect and control authorized Android devices over ADB via CLI or MCP for screens, sound, gestures, apps, recordings, logs, and connections.
---

# Android Device Control

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [arguments]
```

Results are JSON with device identifiers in `target`; errors go to stderr. MCP names use underscores; `home/back` become `press_home/press_back`.

Run `list-targets` first. With several devices, specify `--target`; `set-default-target` remembers one for an hour. Other actions preserve that default. Preserve app sessions; force-restart only for a requested clean start or a stuck app.

Global: `--timeout SECONDS` (default 30), `--adb PATH`, `--direct`. CLI forwards to a verified local server when available; `--direct` or `--adb` bypasses discovery. Forwarded ADB uses the server's timeout. Never retry a failed forwarded mutation directly.

## Arguments

Coordinates are device pixels; gesture durations are milliseconds. Pass only fields for the selected action.

| Action | Required arguments | Optional arguments |
|---|---|---|
| `list-targets`, `status`, `list-packages`, `current-focus`, `home`, `back` | none | none |
| `set-default-target` | `--target TARGET` | none |
| `connect`, `disconnect` | `--address HOST:PORT` | none |
| `pair` | `--address HOST:PAIRING_PORT` | `--pair-code CODE` (six digits; interactive CLI prompts if omitted) |
| `screen-status`, `wake-screen`, `sleep-screen` | none | none |
| `get-dnd` | none | none |
| `set-dnd` | `--dnd-mode off\|priority\|alarms\|none` | none |
| `get-volume` | none | `--stream media\|ring\|notification\|alarm\|voice-call` |
| `set-volume` | At least one of `--level INT`, `--muted true\|false`, `--direction raise\|lower`, `--ringer-mode normal\|vibrate\|silent` | `--stream STREAM` (default media); level and direction are mutually exclusive |
| `screenshot` | none | `--output PATH` (default `android-screen.png`); `--x1 INT --y1 INT --x2 INT --y2 INT` region |
| `describe-screen` | none | `--format json\|original` (default `json`) |
| `tap` | `--x INT --y INT` | none |
| `swipe` | `--x1 INT --y1 INT --x2 INT --y2 INT` | `--duration INT` (default 300) |
| `multi-touch` | `--fingers JSON` | `--duration INT` (default 300, range 1-3000) |
| `zoom-in`, `zoom-out` | `--x INT --y INT` center | `--distance INT` (default 150); `--duration INT` (default 500, range 1-3000) |
| `long-press` | `--x INT --y INT` | `--duration INT` (default 1000) |
| `type-text` | `--text STRING` | none |
| `press-key` | `--key KEY` | none |
| `launch-app` | `--name PACKAGE_OR_FRAGMENT` | `--force-restart` |
| `force-stop-app` | `--name PACKAGE_OR_FRAGMENT` | none |
| `open-url` | `--url HTTP_OR_HTTPS_URL` | none |
| `start-screen-record` | `--time-limit SECONDS` (1-180) | none |
| `stop-screen-record` | none | `--output PATH` (default `android-screen.mp4`) |
| `logs`, `crash-logs` | none | `--name PACKAGE_OR_FRAGMENT --priority V\|D\|I\|W\|E\|F --match TEXT --lines INT --output PATH` |
| `clear-logs` | none | none |

`--fingers` is a JSON array of 2-10 paths, each exactly `{"x1":INT,"y1":INT,"x2":INT,"y2":INT}`; MCP takes the array directly. Coordinates must be 0-65535. Keep paths within the display and verify pinch results with a screenshot.

## Usage notes

- Connections use `--address HOST:PORT` or `[IPv6]:PORT`, without `--target` or an existing device connection. Addresses, including `localhost`, resolve on the ADB server host. Pairing and connection ports can differ; MCP requires `pair_code`, while interactive CLI can prompt without echo.
- Use accessibility element centers for taps and bounds for screenshot regions; `x2,y2` are exclusive. Regions require Android toybox's `od`, `tail`, `head`, and `gzip`. Accessibility can omit WebView/canvas/video content; use screenshots when needed. Waking may leave the phone locked.
- `get-volume` retains global audio status when filtering streams. Read native index ranges before setting a level. Muted streams can report effective zero below the settable minimum; unmuting restores the remembered level. Ringer-only changes leave media untouched. Stream changes return that stream; ringer changes return all five.
- DND modes: `off` disables DND, `priority` follows the existing policy, `alarms` allows alarms only, and `none` blocks interruptions. MCP's setter argument is `mode`, matching CLI `--dnd-mode`. Android may link DND, ringer, and stream mute states.
- Sound setters return observed status with `requested` and `verified`. Combined volume requests apply ringer, level/direction, then mute, without rollback. On verification failure, inspect state before retrying; earlier steps may have applied. Volume/media keys confirm dispatch only, so prefer volume tools for verified changes.
- Collect recordings with `stop-screen-record`. Starting a new recording discards a finished, uncollected recording; a running recording blocks another start.
- `logs --name` selects running package processes, including `:service` children; `crash-logs` can match retained reports after exit by exact package. `--match` is literal and priority is a minimum (default V). Output defaults to 200 lines, capped at 5000 lines/256 KiB; `truncated` marks output limits and `scan_truncated` marks discarded data beyond the latest 4 MiB. Only `clear-logs` clears buffers.
- App fragments match package names, not localized labels. `type-text` converts literal `%s` into a space. For unsupported operations, use ADB on the same host with explicit `-s TARGET`.

## MCP

Install `requirements-mcp.txt`, then run:

```bash
python3 scripts/android_control.py --mcp [--host HOST] [--port PORT]
```

Defaults: `127.0.0.1:8000`; Streamable HTTP `/mcp`, dashboard `/`. Screenshots return images; stopped recordings return video URLs.
