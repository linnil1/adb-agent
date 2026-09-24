# Android Device Control

A Codex skill and Python CLI for inspecting and controlling an authorized Android phone or emulator over ADB. Core actions have no third-party Python dependencies, use named arguments, and return JSON:

```bash
python3 scripts/android_control.py --action ACTION [--target TARGET] [action arguments]
```

See [`SKILL.md`](SKILL.md) for the complete argument schema, workflow, and safety boundaries.

## Actions

| Action | Summary |
|---|---|
| `list-targets` | List connected ADB devices and their states. |
| `set-default-target` | Remember one ready target for one hour. |
| `status` | Report device state, model, Android version, and battery information. |
| `list-packages` | List installed application packages. |
| `current-focus` | Report the activity currently holding window focus. |
| `screenshot` | Capture the display to a validated PNG file. |
| `describe-screen` | Return parsed accessibility elements or the original UI XML. |
| `tap` | Tap one screen coordinate. |
| `swipe` | Swipe between two coordinates. |
| `long-press` | Hold one screen coordinate. |
| `type-text` | Enter text through Android input. |
| `press-key` | Send navigation, editing, Enter, Home, Back, or Recents keys. |
| `home` | Navigate to the Android home screen. |
| `back` | Navigate back. |
| `launch-app` | Launch an installed package, optionally forcing a restart. |
| `force-stop-app` | Force-stop an installed package. |
| `open-url` | Open an HTTP or HTTPS URL on the device. |

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

tap / swipe / long-press ─→ viewer event ─→ animation over cached screenshot
describe_screen ──────────→ viewer event ─→ accessibility-bound animation
interactive mode ─────────→ 1-second per-target captures + mouse/keyboard input
```

Opening the dashboard loads Vue from a CDN, requires no Node.js build, discovers registered tool schemas and connected targets, and displays per-device command history. Interactive mode captures its target once per second without adding those background captures to command history; clicks, drags, holds, and keyboard input are sent through the normal device tools. Its browser event stream is only for viewer updates; MCP remains Streamable HTTP.

## Attribution

This project is inspired by [benasbarciauskas/androir-mcp](https://github.com/benasbarciauskas/androir-mcp), an Apache-2.0-licensed TypeScript MCP server. This implementation provides a local Python CLI, Streamable HTTP MCP server, and build-free dashboard.

## Tests

```bash
python3 -m unittest scripts/test_android_control.py
python3 scripts/android_control.py --action list-targets
```
