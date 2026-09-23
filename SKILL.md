---
name: android-device-control
description: Inspect and operate an authorized Android phone or emulator through ADB, directly or through Streamable HTTP MCP. Use for device status, packages, focused activities, screenshots, UI hierarchy, input, navigation, app launch or force-stop, and web URLs. Do not use for bypassing device security, anti-detection, account farming, or devices the user does not own or administer.
---

# Android Device Control

Use `scripts/android_control.py` for supported operations. It accepts named arguments and emits JSON on success.

## Safety

Operate only devices the user owns or administers. Stay within the requested task and confirm before consequential actions such as purchases, messages, data deletion, security changes, or publishing content.

Preserve the current app session during ordinary automation. Do not relaunch or force-restart an app when navigation can continue from its current state. Use `launch-app --force-restart` only when the user requests a clean start or the app is stuck.

## Workflow

1. Run `python3 scripts/android_control.py --action list-targets`.
2. If one ready device exists, omit `--target`. If several exist, use `--target TARGET` for one call or explicitly remember one for an hour with `set-default-target`.
3. Inspect the current state with `status`, `current-focus`, `screenshot`, or `describe-screen` as appropriate.
4. Use the `center` coordinates from `describe-screen` for `tap`, `swipe`, or `long-press`.
5. Inspect the result after navigation or another state-changing action.

Stop rather than guess when the intended target or consequential UI control is ambiguous.

## CLI schema

Every action uses:

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [action arguments]
```

Common arguments:

| Argument | Type | Required | Meaning |
|---|---|---:|---|
| `--action` | enum | yes | One action from the table below |
| `--target` | string | no | Target for this call; required by `set-default-target` |
| `--adb` | path | no | Explicit ADB executable; also bypasses MCP discovery |
| `--timeout` | positive number | no | Timeout in seconds; default `30` |
| `--direct` | flag | no | Bypass a discovered local MCP server |

Action arguments:

| Action | Required arguments | Optional arguments |
|---|---|---|
| `list-targets` | none | none |
| `set-default-target` | `--target TARGET` | none |
| `status` | none | none |
| `list-packages` | none | none |
| `current-focus` | none | none |
| `screenshot` | none | `--output PATH` (default `android-screen.png`) |
| `describe-screen` | none | `--format json\|original` (default `json`) |
| `tap` | `--x INT --y INT` | none |
| `swipe` | `--x1 INT --y1 INT --x2 INT --y2 INT` | `--duration INT` ms (default `300`) |
| `long-press` | `--x INT --y INT` | `--duration INT` ms (default `1000`) |
| `type-text` | `--text STRING` | none |
| `press-key` | `--key home\|back\|enter\|recents` | none |
| `home` | none | none |
| `back` | none | none |
| `launch-app` | `--name PACKAGE_OR_FRAGMENT` | `--force-restart` |
| `force-stop-app` | `--name PACKAGE_OR_FRAGMENT` | none |
| `open-url` | `--url HTTP_OR_HTTPS_URL` | none |

Coordinates and durations must be non-negative integers. Do not pass arguments unrelated to the selected action.

Representative calls:

```bash
python3 scripts/android_control.py --action status
python3 scripts/android_control.py --action screenshot --output /tmp/android-screen.png
python3 scripts/android_control.py --action describe-screen
python3 scripts/android_control.py --action tap --x 540 --y 260
python3 scripts/android_control.py --action launch-app --name com.android.settings
python3 scripts/android_control.py --action set-default-target --target TARGET
```

## Target selection

One ready target is selected automatically. With multiple ready targets, a remembered target set less than one hour ago is used only if it remains ready; otherwise specify `--target` or run `set-default-target`. Passing `--target` to another action does not change the remembered default.

Results identify devices with `target`. Do not expose the internal ADB term `serial` as a public result field.

## Screen descriptions

Use the default JSON format for automation. Each element includes `label`, `text`, decoded `description`, `bounds`, `center`, `class`, `package`, and a `states` list. States can include `clickable`, `selected`, `scrollable`, `enabled`, `checked`, and `focusable`. Prefer visible text or descriptions over unlabeled clickable nodes.

Use `--format original` only when parsed JSON omits information needed for the task; it returns the raw `uiautomator` XML in `xml`.

## MCP mode

Start the same actions as MCP tools over Streamable HTTP with:

```bash
python3 scripts/android_control.py --mcp [--host HOST] [--port PORT]
```

MCP mode requires `requirements-mcp.txt` and defaults to `127.0.0.1:8000/mcp`. Ordinary CLI calls automatically use a verified local server when one is discoverable, keeping its viewer synchronized. Use `--direct` when direct ADB execution is specifically required. If a verified server accepts a request and then fails, report the failure; do not retry a mutating action directly.

## Direct ADB fallback

ADB discovery checks `ADB_PATH`, `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, common SDK locations, and `~/android-sdk/platform-tools/adb`. Pair or connect wireless devices with ADB before using this skill.

If an operation has no matching action, use ADB directly. Resolve the intended target first, pass it with `adb -s TARGET`, avoid interpolated shell commands, and retain the same authorization and confirmation boundaries.

## Limitations

- Android `input text` converts a literal `%s` sequence into a space.
- `uiautomator` can omit WebView, canvas, video, and accessibility-hidden content; compare with a screenshot when the hierarchy is incomplete.
- App-name fragments match package names, not localized launcher labels. Use the exact package when matching is ambiguous.
