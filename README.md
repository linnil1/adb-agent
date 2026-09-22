# Android Device Control Skill

A Codex skill and dependency-free Python CLI for inspecting and controlling an authorized Android phone or emulator over ADB. It replaces an MCP server with an ordinary named-argument command:

```bash
python3 android-device-control/scripts/android_control.py --action status
python3 android-device-control/scripts/android_control.py --action tap --x 540 --y 260
```

See [`android-device-control/SKILL.md`](android-device-control/SKILL.md) for the complete argument schema, workflow, and safety boundaries.

## Attribution

This project is inspired by [benasbarciauskas/androir-mcp](https://github.com/benasbarciauskas/androir-mcp), an Apache-2.0-licensed TypeScript MCP server. This implementation replaces MCP with a local Python CLI and adds named-argument device pairing and connection actions.

## Test

```bash
python3 -m unittest android-device-control/scripts/test_android_control.py
python3 android-device-control/scripts/android_control.py --action list-targets
```
