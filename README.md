# Android Device Control Skill

A Codex skill and dependency-free Python CLI for inspecting and controlling an authorized Android phone or emulator over ADB. It replaces an MCP server with an ordinary named-argument command:

```bash
python3 scripts/android_control.py --action status
python3 scripts/android_control.py --action tap --x 540 --y 260
python3 scripts/android_control.py --action set-default-target --target TARGET
```

See [`SKILL.md`](SKILL.md) for the complete argument schema, workflow, and safety boundaries.

## Streamable HTTP MCP

Install the official Python MCP SDK and start the same program in MCP mode:

```bash
python3 -m pip install -r requirements-mcp.txt
python3 scripts/android_control.py --mcp
```

The endpoint is `http://127.0.0.1:8000/mcp` by default. Override the listener with `--host` and `--port`. This project supports Streamable HTTP only, not the superseded SSE transport.

The same server provides a build-free Vue viewer at `http://127.0.0.1:8000/`. It discovers every registered MCP tool and its input schema, can invoke those same registered handlers, shows in-memory command history, and animates tap/swipe calls made by any MCP client. Vue loads from a CDN; Node.js is not required.

Opening the viewer does not run an ADB action. A screenshot is captured only when an MCP client or a user in the dashboard invokes the `screenshot` tool. The server caches that image in memory and notifies open viewers, which then load the cached PNG. The small browser event stream used for these notifications is not an alternate MCP transport; MCP remains Streamable HTTP only.

When exactly one target is ready, it is selected automatically. With multiple ready targets, either pass `--target` for that call or use `set_default_target` to remember a validated target for one hour. All MCP tools, including `list_targets` and `set_default_target`, are available through the dashboard's schema-driven tool form. Ordinary `--target` use never changes the remembered default.

## Attribution

This project is inspired by [benasbarciauskas/androir-mcp](https://github.com/benasbarciauskas/androir-mcp), an Apache-2.0-licensed TypeScript MCP server. This implementation provides a local Python CLI, Streamable HTTP MCP server, and build-free dashboard.

## Test

```bash
python3 -m unittest scripts/test_android_control.py
python3 scripts/android_control.py --action list-targets
```
