---
name: android-device-control
description: Inspect and operate an authorized Android phone or emulator through ADB, directly or through Streamable HTTP MCP. Use for checking status, packages, focused activities, screenshots, UI hierarchy, input, navigation, app launch or force-stop, and web URLs. Do not use for bypassing device security, anti-detection, account farming, or devices the user does not own or administer.
---

# Android Device Control

Use `scripts/android_control.py` as the automation tool. It is a dependency-free Python replacement for androir-mcp and calls `adb` directly without `shell=True`.

## Workflow

1. Run `python3 scripts/android_control.py --action list-targets`.
2. If one ready device exists, omit `--target`. If several exist, pass `--target TARGET` for one call, or explicitly remember one for an hour with `--action set-default-target --target TARGET`.
3. Read state with `status`, `current-focus`, `describe-screen`, or `screenshot` before actions.
4. Use the `center` coordinates returned by `describe-screen` directly with `tap`, `swipe`, or `long-press`.
5. Inspect the screen again after navigation or another state-changing action.

Device mutations affect the user's phone. Stay within the requested task. Confirm before consequential actions such as purchases, sending messages, deleting data, changing security settings, or publishing content. Stop when the displayed target is ambiguous.

## Argument schema

Every call has this shape:

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [action arguments]
```

Common arguments:

| Argument | Type | Required | Meaning |
|---|---|---:|---|
| `--action` | enum | yes | One of the actions below |
| `--target` | string | no | ADB target for this call; required by `set-default-target` |
| `--adb` | path | no | Explicit adb executable |
| `--timeout` | positive number | no | Timeout in seconds; default `30` |

To serve the same actions as MCP tools over Streamable HTTP, use `--mcp` instead of `--action`:

```bash
python3 scripts/android_control.py --mcp [--host HOST] [--port PORT]
```

MCP mode defaults to `127.0.0.1:8000` with endpoint `/mcp` and requires the packages in `requirements-mcp.txt`. It intentionally does not support the superseded SSE transport.

The same listener serves a lightweight Vue viewer at `/`. It requires no Node.js build step, obtains the registered tools and their JSON input schemas from the MCP server, and invokes the same tool handlers as MCP clients. It provides in-memory history, crossfades new screenshots, highlights new success or failure entries, and animates target-matching tap, swipe, and long-press calls from any MCP client over the captured screenshot.

Loading the viewer never runs an ADB action. Only an explicit `screenshot` tool call from MCP or the dashboard captures a new PNG. The server caches the latest image in memory and pushes a notification to open viewers; their browser event stream is only a dashboard update channel and does not add an MCP SSE transport.

Target selection is shared by CLI, MCP, and dashboard calls. One ready target is selected silently. With multiple ready targets, a remembered target set less than one hour ago is used if it is still ready; otherwise the operation warns and refuses to guess. Passing `--target` to an ordinary action affects only that call and does not update the remembered target. `set-default-target` validates that its target is currently ready, then replaces the prior default. State is kept in the user's local state directory; `ANDROID_CONTROL_STATE_FILE` may override the state-file path for isolated environments.

Public JSON results consistently identify the selected device with `target`; `list-targets` entries also use `target`. `serial` is only an internal ADB implementation term.

Action-specific schema:

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

Coordinates and durations must be non-negative integers. Unknown or irrelevant action arguments are rejected.

## Examples

```bash
python3 scripts/android_control.py --action status
python3 scripts/android_control.py --action list-packages
python3 scripts/android_control.py --action current-focus
python3 scripts/android_control.py --action set-default-target --target 127.0.0.1:5555
python3 scripts/android_control.py --action screenshot --output /tmp/android-screen.png
python3 scripts/android_control.py --action describe-screen
python3 scripts/android_control.py --action describe-screen --format original
python3 scripts/android_control.py --action tap --x 540 --y 260
python3 scripts/android_control.py --action swipe --x1 540 --y1 1800 --x2 540 --y2 500 --duration 300
python3 scripts/android_control.py --action type-text --text "hello world"
python3 scripts/android_control.py --action launch-app --name com.android.settings
python3 scripts/android_control.py --action launch-app --name com.example.transit --force-restart
python3 scripts/android_control.py --action force-stop-app --name com.example.transit
python3 scripts/android_control.py --action status --target 127.0.0.1:5555
python3 scripts/android_control.py --mcp
```

The script emits JSON on success and concise errors on stderr. `list-packages` refreshes and returns installed package names. `current-focus` reports the focused component, package, and fully qualified activity from `dumpsys window`. `force-stop-app` resolves exact names or unambiguous fragments before calling `am force-stop`. `launch-app --force-restart` performs that force-stop before launching the resolved package; MCP exposes the same behavior as `force_restart: true`. `screenshot` saves a signature-validated PNG and reports its absolute path. `describe-screen` defaults to parsed `json`, returning `label`, `text`, decoded `description`, bounds, centers, classes, packages, and a `states` list containing each active value from `clickable`, `selected`, `scrollable`, `enabled`, `checked`, and `focusable`. Scrollable or selected containers are retained even when they have no label. `--format original` instead preserves the raw `uiautomator` XML in the `xml` field. The MCP `describe_screen` tool exposes the same `format` enum. Prefer visible text or descriptions over unlabeled clickable nodes.

## ADB discovery

ADB discovery checks `ADB_PATH`, `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, common SDK locations, and `~/android-sdk/platform-tools/adb`. Pair or connect wireless devices with `adb` before invoking this skill.

If the requested Android operation is not covered by an action in `scripts/android_control.py`, invoke `adb` directly. Resolve the intended target first, pass it with `adb -s TARGET`, use argument arrays rather than interpolated shell commands, and preserve the same authorization and confirmation boundaries described above.

## Limitations

- Android `input text` converts a literal `%s` sequence into a space.
- `uiautomator` can omit WebView, canvas, video, and accessibility-hidden content; inspect a screenshot when the hierarchy is incomplete.
- Friendly app names match package names, not localized launcher labels. Pass the exact package if matching is ambiguous.

The implementation is inspired by the Apache-2.0-licensed `benasbarciauskas/androir-mcp` project and reimplements its core workflow as a local Python CLI with optional Streamable HTTP MCP.
