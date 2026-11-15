---
name: android-device-control
description: Inspect and control authorized Android devices over ADB via CLI or MCP for screens, UI waits, gestures, apps, recordings, logs, and connections.
---

# Android Device Control

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [arguments]
```

Results are JSON with device identifiers in `target`; errors go to stderr. MCP tool/argument names use underscores; `home/back` become `press_home/press_back`.

Run `list-targets` first. With several devices, specify `--target`; `set-default-target` remembers one for an hour. Other actions preserve that default. Preserve app sessions; force-restart only for a requested clean start or a stuck app.

Global: `--timeout SECONDS` (default 30), `--adb PATH`, `--direct`. `--direct` or `--adb` bypasses server discovery; otherwise CLI forwards to a verified local server. Direct ADB uses the CLI timeout; forwarded ADB uses the server's configured timeout. Never retry a failed forwarded mutation directly.

## Arguments

Coordinates are device pixels; gesture durations are milliseconds. Pass only fields for the selected action.

| Action | Required arguments | Optional arguments |
|---|---|---|
| `list-targets`, `status`, `list-packages`, `current-focus`, `home`, `back` | none | none |
| `set-default-target` | `--target TARGET` | none |
| `connect`, `disconnect` | `--address HOST:PORT` | none |
| `pair` | `--address HOST:PAIRING_PORT` | `--pair-code CODE` (six digits; interactive CLI prompts if omitted) |
| `screen-status`, `wake-screen`, `sleep-screen` | none | none |
| `rotate-screen` | `--rotation 0\|90\|180\|270\|auto` | none |
| `screenshot` | none | `--output PATH` (default `android-screen.png`); `--x1 INT --y1 INT --x2 INT --y2 INT` region |
| `describe-screen` | none | `--format json\|original` (default `json`) |
| `wait-for-ui` | At least one of `--text STRING --description STRING --resource-id STRING` | `--state present\|gone\|enabled` (default present); `--wait-timeout SECONDS` (default 30, >0 to 300) |
| `tap` | `--x INT --y INT` | none |
| `swipe` | `--x1 INT --y1 INT --x2 INT --y2 INT` | `--duration INT` (default 300) |
| `multi-touch` | `--fingers JSON` | `--duration INT` (default 300, range 1-3000) |
| `zoom-in`, `zoom-out` | `--x INT --y INT` center | `--distance INT` (default 150); `--duration INT` (default 500, range 1-3000) |
| `long-press` | `--x INT --y INT` | `--duration INT` (default 1000) |
| `type-text` | `--text STRING` | none |
| `press-key` | `--key home\|back\|up\|down\|left\|right\|tab\|enter\|delete\|recents` | none |
| `launch-app` | `--name PACKAGE_OR_FRAGMENT` | `--force-restart` |
| `force-stop-app` | `--name PACKAGE_OR_FRAGMENT` | none |
| `open-url` | `--url HTTP_OR_HTTPS_URL` | none |
| `start-screen-record` | `--time-limit SECONDS` (1-180) | none |
| `stop-screen-record` | none | `--output PATH` (default `android-screen.mp4`) |
| `logs`, `crash-logs` | none | `--name PACKAGE_OR_FRAGMENT --priority V\|D\|I\|W\|E\|F --match TEXT --lines INT --output PATH` |
| `clear-logs` | none | none |

## Connections

Use explicit `--address HOST:PORT` or `[IPv6]:PORT`, without `--target`. No connected device is required. Addresses, including `localhost`, are reached from the MCP server's ADB host (the local host for direct CLI). Pairing and connection ports can differ. MCP requires `pair_code`; CLI can prompt without echo. Codes are omitted from tool history/results.

## Screens and waits

`describe-screen` returns text, descriptions, `resource_id`, states, bounds, and centers. `--format original` returns XML. Accessibility can omit WebView/canvas/video content; use screenshots when needed.

Wait selectors are exact and combined with AND. Zero-area/explicitly hidden nodes are ignored. Failed captures cannot satisfy `gone`. `--wait-timeout` bounds the wait; success returns matching elements and coordinates without tapping.

Screenshot regions require all four coordinates; `x2,y2` are exclusive, matching element bounds. Prefer regions for small areas. They require Android toybox's `od`, `tail`, `head`, and `gzip`.

Screen status uses logical dimensions; unknown fields are null/`unknown`, with failures in `errors`. Wake/sleep verify interactivity for up to five seconds; waking may leave the lock screen. Rotation is relative to natural orientation; `auto` releases the lock. Check `rotation_matches` separately from `setting_applied`: apps may constrain orientation.

## Gestures

`--fingers` is a JSON array of 2-10 paths, each exactly `{"x1":INT,"y1":INT,"x2":INT,"y2":INT}`. Fingers move simultaneously along straight lines; coordinates must be 0-65535. MCP takes the array directly. The bundled helper needs Android 6+ and shell input injection, without APK installation or root.

Zoom offsets are 100 → 100+distance pixels per finger from the center, reversed for zoom-out. Out-of-screen paths are rejected. Verify screenshots; apps may ignore slow pinches.

## Recording and logs

Recordings stop at the required time limit despite disconnection; collect with `stop-screen-record`. A running recording blocks another start. Starting anew discards finished, uncollected recordings.

`logs --name` selects current package processes, including `:service` children, and fails if none run. `crash-logs` matches retained reports, including exited/uninstalled apps by exact package. Match is literal; crash filters retain report context. Minimum priority defaults to V; lines default to 200 (maximum 5000/256 KiB). `truncated` flags output limits; `scan_truncated` flags discarded bytes beyond the latest 4 MiB. Limits can cut stack traces. CLI `--output` saves the returned text locally. Only `clear-logs` clears main/system/crash buffers.

## Other behavior

- `type-text` converts literal `%s` into a space.
- App fragments match package names, not localized labels.
- For unsupported operations, use ADB on the same host with explicit `-s TARGET`.

## MCP

Install `requirements-mcp.txt`, then run:

```bash
python3 scripts/android_control.py --mcp [--host HOST] [--port PORT]
```

Defaults: `127.0.0.1:8000`; Streamable HTTP `/mcp`, dashboard `/`. Screenshots return images; stopped recordings return video URLs.
