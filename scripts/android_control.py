#!/usr/bin/env python3
"""Dependency-free ADB automation CLI inspired by androir-mcp."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


ACTIONS = (
    "list-targets", "status", "screenshot", "describe-screen", "tap", "swipe",
    "long-press", "type-text", "press-key", "home", "back", "launch-app",
    "open-url",
)
ACTION_FIELDS = {
    "list-targets": set(), "status": set(), "screenshot": {"output"},
    "describe-screen": set(), "tap": {"x", "y"},
    "swipe": {"x1", "y1", "x2", "y2", "duration"},
    "long-press": {"x", "y", "duration"}, "type-text": {"text"},
    "press-key": {"key"}, "home": set(), "back": set(),
    "launch-app": {"name"}, "open-url": {"url"},
}
SERIAL_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,128}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
KEY_MAP = {"home": 3, "back": 4, "enter": 66, "recents": 187}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_TIMEOUT = 30.0
_package_cache: dict[str, tuple[float, list[str]]] = {}


class ControlError(RuntimeError):
    pass


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def resolve_adb(explicit: str | None) -> str:
    candidates: list[Path] = []
    for value in (explicit, os.environ.get("ADB_PATH"), shutil.which("adb")):
        if value:
            candidates.append(Path(value).expanduser())
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]).expanduser() / "platform-tools" / "adb")
    candidates += [
        Path.home() / "Android/Sdk/platform-tools/adb",
        Path.home() / "android-sdk/platform-tools/adb",
        Path("/opt/homebrew/bin/adb"), Path("/usr/local/bin/adb"), Path("/usr/bin/adb"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise ControlError("adb not found; install Android platform-tools or set ADB_PATH")


def scrub_error(stderr: bytes, code: int | None) -> str:
    text = stderr.decode("utf-8", "replace").strip()
    lower = text.lower()
    if "device offline" in lower:
        return "device offline"
    if "unauthorized" in lower:
        return "device unauthorized (accept the debugging prompt)"
    if "no devices" in lower or "device not found" in lower:
        return "device not found"
    if "more than one device" in lower:
        return "multiple devices connected; specify --target"
    if "permission denied" in lower:
        return "adb command failed: permission denied"
    first = text.splitlines()[0][:120] if text else f"exit {code}"
    return f"adb command failed: {first}"


def run_process(argv: list[str], timeout: float) -> bytes:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise ControlError(f"adb timed out after {timeout:g}s") from None
    if process.returncode:
        raise ControlError(scrub_error(stderr, process.returncode))
    return stdout


class Adb:
    def __init__(self, executable: str, timeout: float):
        self.executable = executable
        self.timeout = timeout

    def run(
        self, args: list[str], *, serial: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        argv = [self.executable]
        if serial:
            validate_serial(serial)
            argv += ["-s", serial]
        return run_process(argv + args, timeout or self.timeout)


def validate_serial(serial: str) -> None:
    if not SERIAL_RE.fullmatch(serial):
        raise ControlError("invalid device serial")


def parse_devices(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for line in output.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) < 2 or parts[1] == "no" or not SERIAL_RE.fullmatch(parts[0]):
            continue
        item = {"serial": parts[0], "state": parts[1]}
        for field in parts[2:]:
            for key in ("model", "product", "device"):
                if field.startswith(key + ":"):
                    item[key] = field.split(":", 1)[1]
        devices.append(item)
    return devices


def list_targets(adb: Adb) -> list[dict[str, str]]:
    return parse_devices(adb.run(["devices", "-l"]).decode("utf-8", "replace"))


def resolve_serial(adb: Adb, supplied: str | None) -> str:
    serial = supplied or os.environ.get("ANDROID_SERIAL")
    if serial:
        validate_serial(serial)
        return serial
    ready = [item for item in list_targets(adb) if item["state"] == "device"]
    if not ready:
        raise ControlError("no ready Android device connected")
    if len(ready) > 1:
        names = ", ".join(item["serial"] for item in ready)
        raise ControlError(f"multiple devices connected ({names}); specify --target")
    return ready[0]["serial"]


def shell_quote(value: str) -> str:
    """Quote one token for Android's remote /system/bin/sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def shell(adb: Adb, serial: str, args: list[str], timeout: float | None = None) -> bytes:
    return adb.run(["shell", *args], serial=serial, timeout=timeout)


def extract_node_tags(xml: str) -> list[str]:
    tags: list[str] = []
    for match in re.finditer(r"<node(?=[\s/>])", xml, re.IGNORECASE):
        in_quote = False
        for index in range(match.end(), len(xml)):
            if xml[index] == '"':
                in_quote = not in_quote
            elif xml[index] == ">" and not in_quote:
                tags.append(xml[match.start():index + 1])
                break
        else:
            break
    return tags


def attribute(tag: str, name: str) -> str:
    match = re.search(rf'(?:^|\s){re.escape(name)}="([^"]*)"', tag, re.IGNORECASE)
    return html.unescape(match.group(1)) if match else ""


def parse_ui_xml(xml: str) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = []
    for tag in extract_node_tags(xml):
        text = attribute(tag, "text")
        description = attribute(tag, "content-desc")
        class_name = attribute(tag, "class")
        clickable = attribute(tag, "clickable") == "true"
        if not text and not description and not clickable:
            continue
        bounds = BOUNDS_RE.fullmatch(attribute(tag, "bounds"))
        if not bounds:
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        elements.append({
            "label": text or description or class_name.rsplit(".", 1)[-1] or "element",
            "text": text,
            "content_description": description,
            "class": class_name,
            "package": attribute(tag, "package"),
            "clickable": clickable,
            "bounds": [x1, y1, x2, y2],
            "center": [round((x1 + x2) / 2), round((y1 + y2) / 2)],
        })
    return elements


def dump_ui(adb: Adb, serial: str) -> str:
    try:
        text = adb.run(
            ["exec-out", "uiautomator", "dump", "/dev/tty"], serial=serial
        ).decode("utf-8", "replace")
        start = text.find("<?xml")
        if start >= 0:
            return text[start:]
        if "<hierarchy" in text:
            return text
    except ControlError:
        pass
    remote = "/sdcard/window_dump.xml"
    shell(adb, serial, ["uiautomator", "dump", remote])
    return adb.run(["exec-out", "cat", remote], serial=serial).decode("utf-8", "replace")


def packages(adb: Adb, serial: str) -> list[str]:
    cached = _package_cache.get(serial)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    output = shell(adb, serial, ["pm", "list", "packages"]).decode("utf-8", "replace")
    result = [line[8:].strip() for line in output.splitlines() if line.startswith("package:")]
    _package_cache[serial] = (time.monotonic(), result)
    return result


def resolve_package(adb: Adb, serial: str, query: str) -> str:
    installed = packages(adb, serial)
    exact = next((pkg for pkg in installed if pkg.lower() == query.lower()), None)
    if exact:
        return exact
    needle = query.lower()
    matches = [pkg for pkg in installed if needle in pkg.lower() or needle in pkg.rsplit(".", 1)[-1].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ControlError(f"no package found for {query!r}")
    raise ControlError(f"ambiguous app name {query!r}: {', '.join(matches[:5])}")


def require(value: object, flag: str) -> object:
    if value is None:
        raise ControlError(f"{flag} is required for this action")
    return value


def validate_action_args(args: argparse.Namespace) -> None:
    required = {
        "tap": ("x", "y"),
        "swipe": ("x1", "y1", "x2", "y2"),
        "long-press": ("x", "y"),
        "type-text": ("text",),
        "press-key": ("key",),
        "launch-app": ("name",),
        "open-url": ("url",),
    }.get(args.action, ())
    for field in required:
        require(getattr(args, field), "--" + field.replace("_", "-"))
    optional_fields = {"output", "x", "y", "x1", "y1", "x2", "y2", "duration", "text", "key", "name", "url"}
    extras = [field for field in optional_fields - ACTION_FIELDS[args.action] if getattr(args, field) is not None]
    if extras:
        flags = ", ".join("--" + field.replace("_", "-") for field in sorted(extras))
        raise ControlError(f"argument(s) not valid for {args.action}: {flags}")


def device_action(args: argparse.Namespace, adb: Adb) -> object:
    serial = resolve_serial(adb, args.target)
    action = args.action
    if action == "status":
        props = {}
        for key in ("ro.product.model", "ro.product.manufacturer", "ro.build.version.release", "ro.build.version.sdk"):
            try:
                props[key] = shell(adb, serial, ["getprop", key]).decode().strip()
            except ControlError:
                props[key] = ""
        battery: dict[str, str] = {}
        try:
            output = shell(adb, serial, ["dumpsys", "battery"]).decode("utf-8", "replace")
            for line in output.splitlines():
                if ":" in line:
                    key, value = (part.strip() for part in line.split(":", 1))
                    if key.lower() in {"level", "status", "health", "ac powered", "usb powered"}:
                        battery[key.lower()] = value
        except ControlError:
            pass
        return {"serial": serial, "state": adb.run(["get-state"], serial=serial).decode().strip(), "properties": props, "battery": battery}
    elif action == "screenshot":
        png = adb.run(["exec-out", "screencap", "-p"], serial=serial, timeout=60)
        if not png.startswith(PNG_MAGIC):
            raise ControlError("screenshot response is not a valid PNG")
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return {"serial": serial, "path": str(output), "bytes": len(png)}
    elif action == "describe-screen":
        elements = parse_ui_xml(dump_ui(adb, serial))
        if not elements:
            raise ControlError("no UI elements found")
        return {"serial": serial, "elements": elements}
    elif action == "tap":
        shell(adb, serial, ["input", "tap", str(args.x), str(args.y)])
        return {"serial": serial, "action": action, "point": [args.x, args.y]}
    elif action == "swipe":
        shell(adb, serial, ["input", "swipe", str(args.x1), str(args.y1), str(args.x2), str(args.y2), str(args.duration)])
        return {"serial": serial, "action": action, "from": [args.x1, args.y1], "to": [args.x2, args.y2], "duration_ms": args.duration}
    elif action == "long-press":
        shell(adb, serial, ["input", "swipe", str(args.x), str(args.y), str(args.x), str(args.y), str(args.duration)])
        return {"serial": serial, "action": action, "point": [args.x, args.y], "duration_ms": args.duration}
    elif action == "type-text":
        shell(adb, serial, ["input", "text", shell_quote(args.text.replace(" ", "%s"))])
        return {"serial": serial, "action": action, "characters": len(args.text)}
    elif action in {"press-key", "home", "back"}:
        key = args.key if action == "press-key" else action
        shell(adb, serial, ["input", "keyevent", str(KEY_MAP[key])])
        return {"serial": serial, "action": action, "key": key}
    elif action == "launch-app":
        package = resolve_package(adb, serial, args.name)
        if not PACKAGE_RE.fullmatch(package):
            raise ControlError("resolved package name is invalid")
        shell(adb, serial, ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        return {"serial": serial, "action": action, "package": package}
    elif action == "open-url":
        parsed = urlsplit(args.url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ControlError("URL must be an absolute http:// or https:// URL")
        shell(adb, serial, ["am", "start", "-a", "android.intent.action.VIEW", "-d", shell_quote(args.url)])
        return {"serial": serial, "action": action, "url": args.url}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control an authorized Android device over adb")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--action", choices=ACTIONS)
    mode.add_argument("--mcp", action="store_true", help="serve the tools over Streamable HTTP")
    parser.add_argument("--target", help="ADB device serial; defaults to the only ready target")
    parser.add_argument("--adb")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--host", help="MCP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="MCP bind port (default: 8000)")
    parser.add_argument("--output")
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--x1", type=int)
    parser.add_argument("--y1", type=int)
    parser.add_argument("--x2", type=int)
    parser.add_argument("--y2", type=int)
    parser.add_argument("--duration", type=int)
    parser.add_argument("--text")
    parser.add_argument("--key", choices=sorted(KEY_MAP))
    parser.add_argument("--name")
    parser.add_argument("--url")
    return parser


def execute(args: argparse.Namespace) -> object:
    if args.timeout <= 0:
        raise ControlError("--timeout must be positive")
    for field in ("x", "y", "x1", "y1", "x2", "y2"):
        value = getattr(args, field)
        if value is not None and value < 0:
            raise ControlError(f"--{field} must be non-negative")
    if args.duration is not None and args.duration < 0:
        raise ControlError("--duration must be non-negative")
    validate_action_args(args)
    if args.action in {"swipe", "long-press"} and args.duration is None:
        args.duration = 1000 if args.action == "long-press" else 300
    if args.action == "screenshot" and args.output is None:
        args.output = "android-screen.png"
    adb = Adb(resolve_adb(args.adb), args.timeout)
    if args.action == "list-targets":
        return list_targets(adb)
    return device_action(args, adb)


def action_args(action: str, *, adb_path: str | None = None, timeout: float = DEFAULT_TIMEOUT, **values: object) -> argparse.Namespace:
    argv = ["--action", action, "--timeout", str(timeout)]
    if adb_path:
        argv += ["--adb", adb_path]
    for name, value in values.items():
        if value is not None:
            argv += ["--" + name.replace("_", "-"), str(value)]
    return build_parser().parse_args(argv)


def run_mcp(host: str, port: int, adb_path: str | None, timeout: float) -> None:
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver import Image
        from mcp.server.mcpserver.exceptions import ToolError
        from starlette.requests import Request
        from starlette.responses import FileResponse, JSONResponse, Response
    except ImportError:
        raise ControlError(
            "MCP support requires the 'mcp' package; install requirements-mcp.txt"
        ) from None

    resolved_adb = resolve_adb(adb_path)
    web_root = Path(__file__).resolve().parent.parent / "web"
    history: deque[dict[str, object]] = deque(maxlen=100)
    server = MCPServer(
        "android-device-control",
        instructions="Inspect and control an authorized Android device over ADB.",
    )

    def record(action: str, values: dict[str, object], ok: bool, error: str | None = None) -> None:
        safe_values = {key: value for key, value in values.items() if value is not None}
        entry: dict[str, object] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "arguments": safe_values,
            "ok": ok,
        }
        if error:
            entry["error"] = error
        history.appendleft(entry)

    def call(action: str, **values: object) -> object:
        try:
            result = execute(action_args(action, adb_path=resolved_adb, timeout=timeout, **values))
            record(action, values, True)
            return result
        except ControlError as error:
            record(action, values, False, str(error))
            raise ToolError(str(error)) from None

    def capture_png(target: str | None = None) -> bytes:
        adb = Adb(resolved_adb, timeout)
        selected = resolve_serial(adb, target)
        png = adb.run(["exec-out", "screencap", "-p"], serial=selected, timeout=60)
        if not png.startswith(PNG_MAGIC):
            raise ControlError("screenshot response is not a valid PNG")
        return png

    @server.tool(name="list_targets")
    def list_targets_tool() -> list[dict[str, str]]:
        """List Android devices visible to ADB."""
        return call("list-targets")  # type: ignore[return-value]

    @server.tool(name="status")
    def status_tool(target: str | None = None) -> dict:
        """Return device state, identity, Android version, and battery details."""
        return call("status", target=target)  # type: ignore[return-value]

    @server.tool(name="screenshot")
    def screenshot_tool(target: str | None = None):
        """Capture the current device screen as a PNG image."""
        try:
            png = capture_png(target)
            record("screenshot", {"target": target}, True)
            return Image(data=png, format="png")
        except ControlError as error:
            record("screenshot", {"target": target}, False, str(error))
            raise ToolError(str(error)) from None

    @server.tool(name="describe_screen")
    def describe_screen_tool(target: str | None = None) -> dict:
        """Return UI labels, bounds, and center coordinates from uiautomator."""
        return call("describe-screen", target=target)  # type: ignore[return-value]

    @server.tool(name="tap")
    def tap_tool(x: int, y: int, target: str | None = None) -> dict:
        """Tap non-negative device-pixel coordinates."""
        return call("tap", x=x, y=y, target=target)  # type: ignore[return-value]

    @server.tool(name="swipe")
    def swipe_tool(
        x1: int, y1: int, x2: int, y2: int,
        duration: int = 300, target: str | None = None,
    ) -> dict:
        """Swipe between device-pixel coordinates."""
        return call("swipe", x1=x1, y1=y1, x2=x2, y2=y2, duration=duration, target=target)  # type: ignore[return-value]

    @server.tool(name="long_press")
    def long_press_tool(
        x: int, y: int, duration: int = 1000, target: str | None = None,
    ) -> dict:
        """Long-press device-pixel coordinates."""
        return call("long-press", x=x, y=y, duration=duration, target=target)  # type: ignore[return-value]

    @server.tool(name="type_text")
    def type_text_tool(text: str, target: str | None = None) -> dict:
        """Type text into the focused Android input."""
        return call("type-text", text=text, target=target)  # type: ignore[return-value]

    @server.tool(name="press_key")
    def press_key_tool(
        key: Literal["home", "back", "enter", "recents"],
        target: str | None = None,
    ) -> dict:
        """Press home, back, enter, or recents."""
        return call("press-key", key=key, target=target)  # type: ignore[return-value]

    @server.tool(name="press_home")
    def press_home_tool(target: str | None = None) -> dict:
        """Press the Android home button."""
        return call("home", target=target)  # type: ignore[return-value]

    @server.tool(name="press_back")
    def press_back_tool(target: str | None = None) -> dict:
        """Press the Android back button."""
        return call("back", target=target)  # type: ignore[return-value]

    @server.tool(name="launch_app")
    def launch_app_tool(name: str, target: str | None = None) -> dict:
        """Launch an installed app by exact package or package-name fragment."""
        return call("launch-app", name=name, target=target)  # type: ignore[return-value]

    @server.tool(name="open_url")
    def open_url_tool(url: str, target: str | None = None) -> dict:
        """Open an absolute HTTP or HTTPS URL on the device."""
        return call("open-url", url=url, target=target)  # type: ignore[return-value]

    @server.custom_route("/", methods=["GET"])
    async def dashboard(_request: Request) -> Response:
        return FileResponse(web_root / "index.html", media_type="text/html")

    @server.custom_route("/api/screenshot", methods=["GET"])
    async def dashboard_screenshot(request: Request) -> Response:
        target = request.query_params.get("target") or None
        try:
            return Response(
                capture_png(target),
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        except ControlError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    @server.custom_route("/api/history", methods=["GET"])
    async def dashboard_history(_request: Request) -> Response:
        return JSONResponse({"history": list(history)})

    @server.custom_route("/api/command", methods=["POST"])
    async def dashboard_command(request: Request) -> Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
            return JSONResponse({"ok": False, "error": "invalid action"}, status_code=400)
        action = str(payload["action"])
        allowed = ACTION_FIELDS[action] | {"target"}
        values = {key: payload[key] for key in allowed if key in payload}
        try:
            return JSONResponse({"ok": True, "result": call(action, **values)})
        except ToolError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    server.run(transport="streamable-http", host=host, port=port)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mcp:
        action_fields = set().union(*ACTION_FIELDS.values())
        if args.target or any(getattr(args, field) is not None for field in action_fields):
            raise ControlError("action arguments cannot be combined with --mcp")
        host = args.host or "127.0.0.1"
        port = args.port or 8000
        if not 1 <= port <= 65535:
            raise ControlError("--port must be between 1 and 65535")
        try:
            run_mcp(host, port, args.adb, args.timeout)
        except KeyboardInterrupt:
            pass
    else:
        if args.host is not None or args.port is not None:
            raise ControlError("--host and --port require --mcp")
        emit(execute(args))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControlError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
