---
name: android-device-control
description: Inspect and control an authorized Android phone or emulator over ADB through the local CLI or MCP, including screenshots, screen power/rotation, UI elements, input, multi-touch, apps, recording, and logs.
---

# Android Device Control

Use the local tool:

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [arguments]
```

Results are JSON; device identifiers use `target`. Errors go to stderr. MCP tool names replace CLI hyphens with underscores; `home/back` become `press_home/press_back`.

Run `list-targets` first. Omit `--target` with one ready device; specify it with several. `set-default-target` remembers a target for one hour; other calls never change it. Preserve the current app session; use `--force-restart` only for a requested clean start or a stuck app.

## Arguments

Global options: `--target TARGET`, `--timeout SECONDS` (default 30), `--adb PATH`, `--direct`. The last two bypass local server discovery. CLI calls otherwise forward to a verified local server when available. If a forwarded action fails, do not retry a mutating action directly.

Pass only arguments for the selected action. Coordinates and durations are non-negative integers in device pixels and milliseconds.

| Action | Required arguments | Optional arguments |
|---|---|---|
| `list-targets`, `status`, `list-packages`, `current-focus`, `home`, `back` | none | none |
| `set-default-target` | `--target TARGET` | none |
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

## Screen inspection

`describe-screen` returns elements with `label`, `text`, `description`, `resource_id`, `bounds`, `center`, `class`, `package`, and `states`. Use `center` for taps and `bounds` for screenshot regions. `--format original` returns raw XML in `xml`.

Wait selectors are exact and combined with AND. Zero-area/explicitly hidden nodes are ignored. Failed captures cannot satisfy `gone`. `--wait-timeout` bounds the wait; success returns matching elements and coordinates without tapping.

Screenshot regions require all four coordinates, with exclusive `x2,y2`, matching element bounds. Prefer regions for small areas: only their screen rows are transferred, gzip-compressed. Regions require Android toybox's `od`, `tail`, `head`, and `gzip`.

`uiautomator` can omit WebView, canvas, video, and accessibility-hidden content; use screenshots when the hierarchy is incomplete.

`screen-status` reads the default display's power, interactivity, logical size, and rotation; unknown fields are null/`unknown`, with read failures in `errors`. Wake/sleep use dedicated keys and verify interactivity within five seconds; waking may leave the lock screen. Rotation angles are relative to natural orientation; `auto` releases the user rotation lock. `setting_applied` confirms the setting, while `rotation_matches` reports whether the display followed it (apps may constrain orientation).

## Multi-touch and pinch-to-zoom

`--fingers`: JSON array of 2-10 paths, each exactly `{"x1":INT,"y1":INT,"x2":INT,"y2":INT}`, moving simultaneously from start to end along straight lines. Coordinates: 0-65535, within the display. MCP `fingers` takes the array directly. Requires shell input injection; the bundled ADB helper needs no APK or root.

Zoom `distance` is each finger's travel in pixels: offsets from the center are 100 → 100+distance for zoom-in, reversed for zoom-out. Out-of-screen endpoints are rejected. Verify screenshots; slow pinches may be ignored by the app.

## Recording

Start with a required time limit, then call `stop-screen-record` to collect the MP4. Recording survives a disconnected client and stops at its limit. One recording runs per device; starting another discards the previous recording, even if it has finished. Results include `seconds`.

## Other tool behavior

- `type-text` converts literal `%s` into a space.
- App fragments match package names, not localized launcher labels.
- ADB discovery checks `ADB_PATH`, `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, and common SDK locations including `~/android-sdk/platform-tools/adb`.
- For unsupported operations, use ADB with an explicit `-s TARGET`.

## Logs

`logs` reads main/system/crash buffers; `--name` selects current app processes (including `:service` children), failing if none run. `crash-logs` reads retained crashes and can match an exited app by its exact package. Priority is a minimum (default V); match is literal. Crash filters preserve matching report context.

Results include `text`, `line_count`, `matched_lines`, `pids`, and truncation flags. Defaults: 200 lines; maximum 5000 lines/256 KiB, scanning the latest 4 MiB. Limits can cut stack traces; `scan_truncated` means older captured bytes were discarded. An empty capture doesn't prove no crash occurred. CLI `--output` saves the same bounded text locally. Only `clear-logs` clears main/system/crash buffers.

## MCP

Install `requirements-mcp.txt`, then run:

```bash
python3 scripts/android_control.py --mcp [--host HOST] [--port PORT]
```

Defaults: `127.0.0.1:8000`; Streamable HTTP at `/mcp`, dashboard at `/`. MCP arguments omit CLI `--` prefixes; screenshot returns an image and stopped recording returns a video URL.
