#!/usr/bin/env python3
"""Dependency-free ADB automation CLI inspired by androir-mcp."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit


ACTIONS = (
    "list-targets", "set-default-target", "status", "list-packages", "current-focus",
    "screenshot", "describe-screen", "tap", "swipe", "long-press", "type-text",
    "press-key", "home", "back", "launch-app", "force-stop-app", "open-url",
)
ACTION_FIELDS = {
    "list-targets": set(), "set-default-target": set(), "status": set(),
    "list-packages": set(), "current-focus": set(), "screenshot": {"output"},
    "describe-screen": {"format"}, "tap": {"x", "y"},
    "swipe": {"x1", "y1", "x2", "y2", "duration"},
    "long-press": {"x", "y", "duration"}, "type-text": {"text"},
    "press-key": {"key"}, "home": set(), "back": set(),
    "launch-app": {"name", "force_restart"}, "force-stop-app": {"name"},
    "open-url": {"url"},
}
SERIAL_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,128}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
COMPONENT_RE = re.compile(
    r"(?P<component>(?P<package>[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/"
    r"(?P<activity>\.?[A-Za-z0-9_.$]+))"
)
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
KEY_MAP = {"home": 3, "back": 4, "enter": 66, "recents": 187}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_TIMEOUT = 30.0
DEFAULT_TARGET_TTL = 60 * 60
STATE_FILE_ENV = "ANDROID_CONTROL_STATE_FILE"
SERVER_FILE_ENV = "ANDROID_CONTROL_SERVER_FILE"
SERVER_NAME = "android-device-control"
SERVER_API_VERSION = 1
_package_cache: dict[str, tuple[float, list[str]]] = {}


class ControlError(RuntimeError):
    pass


class AmbiguousTargetError(ControlError):
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
        item = {"target": parts[0], "state": parts[1]}
        for field in parts[2:]:
            for key in ("model", "product", "device"):
                if field.startswith(key + ":"):
                    item[key] = field.split(":", 1)[1]
        devices.append(item)
    return devices


def list_targets(adb: Adb) -> list[dict[str, str]]:
    return parse_devices(adb.run(["devices", "-l"]).decode("utf-8", "replace"))


def state_file() -> Path:
    override = os.environ.get(STATE_FILE_ENV)
    if override:
        return Path(override).expanduser()
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_root / "android-device-control" / "default-target.json"


def server_file() -> Path:
    override = os.environ.get(SERVER_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return state_file().with_name("mcp-server.json")


def advertised_server_url(host: str, port: int) -> str:
    advertised = "127.0.0.1" if host in {"0.0.0.0", "::", "localhost"} else host
    if ":" in advertised and not advertised.startswith("["):
        advertised = f"[{advertised}]"
    return f"http://{advertised}:{port}"


def write_server_file(url: str, pid: int | None = None) -> None:
    path = server_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    process_id = os.getpid() if pid is None else pid
    temporary = path.with_name(path.name + f".{process_id}.tmp")
    temporary.write_text(json.dumps({
        "name": SERVER_NAME,
        "api_version": SERVER_API_VERSION,
        "pid": process_id,
        "url": url,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def clear_server_file(pid: int | None = None) -> None:
    expected = os.getpid() if pid is None else pid
    path = server_file()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("pid") == expected:
            path.unlink()
    except (OSError, AttributeError, json.JSONDecodeError):
        pass


def read_default_target(now: float | None = None) -> str | None:
    try:
        value = json.loads(state_file().read_text(encoding="utf-8"))
        target = value["target"]
        selected_at = float(value["selected_at"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    current = time.time() if now is None else now
    if not isinstance(target, str) or not SERIAL_RE.fullmatch(target):
        return None
    if selected_at > current or current - selected_at >= DEFAULT_TARGET_TTL:
        return None
    return target


def write_default_target(target: str, now: float | None = None) -> None:
    validate_serial(target)
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"target": target, "selected_at": time.time() if now is None else now}) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def resolve_serial(adb: Adb, supplied: str | None) -> str:
    serial = supplied or os.environ.get("ANDROID_SERIAL")
    if serial:
        validate_serial(serial)
        return serial
    ready = [item for item in list_targets(adb) if item["state"] == "device"]
    if not ready:
        raise ControlError("no ready Android device connected")
    if len(ready) > 1:
        remembered = read_default_target()
        if remembered and any(item["target"] == remembered for item in ready):
            return remembered
        names = ", ".join(item["target"] for item in ready)
        raise AmbiguousTargetError(
            f"multiple ready targets ({names}); specify --target or use set-default-target"
        )
    return ready[0]["target"]


def set_default_target(adb: Adb, target: str) -> dict[str, object]:
    validate_serial(target)
    ready = {item["target"] for item in list_targets(adb) if item["state"] == "device"}
    if target not in ready:
        raise ControlError(f"target is not connected and ready: {target}")
    write_default_target(target)
    return {
        "target": target,
        "default_until": datetime.fromtimestamp(
            time.time() + DEFAULT_TARGET_TTL, timezone.utc
        ).isoformat(),
    }


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
        selected = attribute(tag, "selected") == "true"
        scrollable = attribute(tag, "scrollable") == "true"
        enabled = attribute(tag, "enabled") == "true"
        checked = attribute(tag, "checked") == "true"
        focusable = attribute(tag, "focusable") == "true"
        states = [
            name for name, active in (
                ("clickable", clickable),
                ("selected", selected),
                ("scrollable", scrollable),
                ("enabled", enabled),
                ("checked", checked),
                ("focusable", focusable),
            ) if active
        ]
        if not text and not description and not clickable and not selected and not scrollable:
            continue
        bounds = BOUNDS_RE.fullmatch(attribute(tag, "bounds"))
        if not bounds:
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        elements.append({
            "label": text or description or class_name.rsplit(".", 1)[-1] or "element",
            "text": text,
            "description": description,
            "class": class_name,
            "package": attribute(tag, "package"),
            "states": states,
            "bounds": [x1, y1, x2, y2],
            "center": [round((x1 + x2) / 2), round((y1 + y2) / 2)],
        })
    return elements


def describe_ui(target: str, xml: str, output_format: str) -> dict[str, object]:
    if output_format == "original":
        return {"target": target, "format": "original", "xml": xml}
    elements = parse_ui_xml(xml)
    if not elements:
        raise ControlError("no UI elements found")
    return {"target": target, "format": "json", "elements": elements}


def description_elements(result: dict[str, object]) -> list[dict[str, object]]:
    elements = result.get("elements")
    if isinstance(elements, list):
        return elements
    xml = result.get("xml")
    return parse_ui_xml(xml) if isinstance(xml, str) else []


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


def parse_packages(output: str) -> list[str]:
    return [line[8:].strip() for line in output.splitlines() if line.startswith("package:")]


def packages(adb: Adb, serial: str, *, refresh: bool = False) -> list[str]:
    cached = _package_cache.get(serial)
    if not refresh and cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    output = shell(adb, serial, ["pm", "list", "packages"]).decode("utf-8", "replace")
    result = parse_packages(output)
    _package_cache[serial] = (time.monotonic(), result)
    return result


def parse_current_focus(output: str) -> dict[str, str] | None:
    for source in ("mCurrentFocus", "mFocusedApp"):
        for line in output.splitlines():
            if source not in line:
                continue
            match = COMPONENT_RE.search(line)
            if not match:
                continue
            package = match.group("package")
            activity = match.group("activity")
            return {
                "component": match.group("component"),
                "package": package,
                "activity": package + activity if activity.startswith(".") else activity,
                "source": source,
            }
    return None


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
        "force-stop-app": ("name",),
        "open-url": ("url",),
    }.get(args.action, ())
    if args.action == "set-default-target":
        require(args.target, "--target")
    for field in required:
        require(getattr(args, field), "--" + field.replace("_", "-"))
    optional_fields = {"output", "format", "force_restart", "x", "y", "x1", "y1", "x2", "y2", "duration", "text", "key", "name", "url"}
    extras = [field for field in optional_fields - ACTION_FIELDS[args.action] if getattr(args, field) is not None]
    if extras:
        flags = ", ".join("--" + field.replace("_", "-") for field in sorted(extras))
        raise ControlError(f"argument(s) not valid for {args.action}: {flags}")


def device_action(args: argparse.Namespace, adb: Adb) -> object:
    if args.action == "set-default-target":
        return set_default_target(adb, args.target)
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
        return {"target": serial, "state": adb.run(["get-state"], serial=serial).decode().strip(), "properties": props, "battery": battery}
    elif action == "list-packages":
        installed = packages(adb, serial, refresh=True)
        return {"target": serial, "packages": installed, "count": len(installed)}
    elif action == "current-focus":
        output = shell(adb, serial, ["dumpsys", "window"]).decode("utf-8", "replace")
        focused = parse_current_focus(output)
        if not focused:
            raise ControlError("focused activity not found in dumpsys window")
        return {"target": serial, **focused}
    elif action == "screenshot":
        png = adb.run(["exec-out", "screencap", "-p"], serial=serial, timeout=60)
        if not png.startswith(PNG_MAGIC):
            raise ControlError("screenshot response is not a valid PNG")
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return {"target": serial, "path": str(output), "bytes": len(png)}
    elif action == "describe-screen":
        return describe_ui(serial, dump_ui(adb, serial), args.format or "json")
    elif action == "tap":
        shell(adb, serial, ["input", "tap", str(args.x), str(args.y)])
        return {"target": serial, "action": action, "point": [args.x, args.y]}
    elif action == "swipe":
        shell(adb, serial, ["input", "swipe", str(args.x1), str(args.y1), str(args.x2), str(args.y2), str(args.duration)])
        return {"target": serial, "action": action, "from": [args.x1, args.y1], "to": [args.x2, args.y2], "duration_ms": args.duration}
    elif action == "long-press":
        shell(adb, serial, ["input", "swipe", str(args.x), str(args.y), str(args.x), str(args.y), str(args.duration)])
        return {"target": serial, "action": action, "point": [args.x, args.y], "duration_ms": args.duration}
    elif action == "type-text":
        shell(adb, serial, ["input", "text", shell_quote(args.text.replace(" ", "%s"))])
        return {"target": serial, "action": action, "characters": len(args.text)}
    elif action in {"press-key", "home", "back"}:
        key = args.key if action == "press-key" else action
        shell(adb, serial, ["input", "keyevent", str(KEY_MAP[key])])
        return {"target": serial, "action": action, "key": key}
    elif action == "launch-app":
        package = resolve_package(adb, serial, args.name)
        if not PACKAGE_RE.fullmatch(package):
            raise ControlError("resolved package name is invalid")
        if args.force_restart:
            shell(adb, serial, ["am", "force-stop", package])
        shell(adb, serial, ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        return {
            "target": serial, "action": action, "package": package,
            "force_restarted": bool(args.force_restart),
        }
    elif action == "force-stop-app":
        package = resolve_package(adb, serial, args.name)
        if not PACKAGE_RE.fullmatch(package):
            raise ControlError("resolved package name is invalid")
        shell(adb, serial, ["am", "force-stop", package])
        return {"target": serial, "action": action, "package": package}
    elif action == "open-url":
        parsed = urlsplit(args.url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ControlError("URL must be an absolute http:// or https:// URL")
        shell(adb, serial, ["am", "start", "-a", "android.intent.action.VIEW", "-d", shell_quote(args.url)])
        return {"target": serial, "action": action, "url": args.url}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control an authorized Android device over adb")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--action", choices=ACTIONS)
    mode.add_argument("--mcp", action="store_true", help="serve the tools over Streamable HTTP")
    parser.add_argument("--target", help="ADB target for this call; required by set-default-target")
    parser.add_argument("--adb")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--host", help="MCP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="MCP bind port (default: 8000)")
    parser.add_argument("--direct", action="store_true", help="bypass a discovered local MCP server")
    parser.add_argument("--output")
    parser.add_argument("--format", choices=("original", "json"))
    parser.add_argument("--force-restart", action="store_true", default=None)
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


def prepare_action(args: argparse.Namespace) -> None:
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


def execute(args: argparse.Namespace) -> object:
    prepare_action(args)
    adb = Adb(resolve_adb(args.adb), args.timeout)
    if args.action == "list-targets":
        return list_targets(adb)
    return device_action(args, adb)


def read_json_response(response) -> dict[str, object]:
    try:
        value = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("local MCP server returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ControlError("local MCP server returned an invalid response")
    return value


def verify_server(url: str, pid: int, timeout: float = 0.5) -> bool:
    try:
        with urlrequest.urlopen(url + "/api/info", timeout=timeout) as response:
            value = read_json_response(response)
    except (OSError, urlerror.URLError, ControlError):
        return False
    return (
        value.get("name") == SERVER_NAME
        and value.get("api_version") == SERVER_API_VERSION
        and value.get("pid") == pid
    )


def discover_server() -> str | None:
    path = server_file()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = value["pid"]
        url = value["url"]
        if (
            value.get("name") != SERVER_NAME
            or value.get("api_version") != SERVER_API_VERSION
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(url, str)
            or urlsplit(url).scheme != "http"
        ):
            raise ValueError
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    if verify_server(url, pid):
        return url.rstrip("/")
    clear_server_file(pid)
    return None


def tool_name_for_action(action: str) -> str:
    return {"home": "press_home", "back": "press_back"}.get(
        action, action.replace("-", "_")
    )


def execute_via_server(args: argparse.Namespace, server_url: str) -> object:
    prepare_action(args)
    arguments: dict[str, object] = {}
    for field in ACTION_FIELDS[args.action]:
        if field == "output":
            continue
        value = getattr(args, field)
        if value is not None:
            arguments[field] = value
    if args.target:
        arguments["target"] = args.target
    payload = json.dumps({
        "name": tool_name_for_action(args.action),
        "arguments": arguments,
    }).encode("utf-8")
    request = urlrequest.Request(
        server_url + "/api/tools/call",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=max(args.timeout, 1)) as response:
            body = read_json_response(response)
    except urlerror.HTTPError as error:
        try:
            detail = read_json_response(error).get("error")
        except ControlError:
            detail = None
        raise ControlError(str(detail or f"local MCP server returned HTTP {error.code}")) from None
    except (OSError, urlerror.URLError) as error:
        raise ControlError(
            "verified local MCP server became unavailable; action was not retried"
        ) from error
    if body.get("ok") is not True:
        raise ControlError(str(body.get("error") or "local MCP tool call failed"))
    if args.action == "screenshot":
        viewer = body.get("viewer")
        target = viewer.get("target") if isinstance(viewer, dict) else None
        revision = viewer.get("revision") if isinstance(viewer, dict) else None
        if not isinstance(revision, int):
            raise ControlError("local MCP server omitted the screenshot revision")
        screenshot_url = server_url + f"/api/viewer/screenshot?revision={revision}"
        try:
            with urlrequest.urlopen(screenshot_url, timeout=max(args.timeout, 1)) as response:
                png = response.read()
        except (OSError, urlerror.URLError) as error:
            raise ControlError("screenshot completed but cached PNG could not be downloaded") from error
        if not png.startswith(PNG_MAGIC):
            raise ControlError("local MCP server returned an invalid screenshot")
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return {"target": target, "path": str(output), "bytes": len(png)}
    result = body.get("result")
    if not isinstance(result, dict):
        raise ControlError("local MCP server omitted the tool result")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if structured is not None:
        return structured
    raise ControlError("local MCP server omitted structured tool output")


def action_args(action: str, *, adb_path: str | None = None, timeout: float = DEFAULT_TIMEOUT, **values: object) -> argparse.Namespace:
    argv = ["--action", action, "--timeout", str(timeout)]
    if adb_path:
        argv += ["--adb", adb_path]
    for name, value in values.items():
        if isinstance(value, bool):
            if value:
                argv.append("--" + name.replace("_", "-"))
        elif value is not None:
            argv += ["--" + name.replace("_", "-"), str(value)]
    return build_parser().parse_args(argv)


def run_mcp(host: str, port: int, adb_path: str | None, timeout: float) -> None:
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver import Image
        from mcp.server.mcpserver.exceptions import ToolError
        from starlette.requests import Request
        from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
    except ImportError:
        raise ControlError(
            "MCP support requires the 'mcp' package; install requirements-mcp.txt"
        ) from None

    resolved_adb = resolve_adb(adb_path)
    web_root = Path(__file__).resolve().parent.parent / "web"
    history: deque[dict[str, object]] = deque(maxlen=100)
    viewer_lock = threading.Lock()
    viewer_events: deque[dict[str, object]] = deque(maxlen=200)
    viewer_sequence = 0
    latest_screenshot: dict[str, object] = {}
    latest_description: dict[str, object] = {}
    server = MCPServer(
        SERVER_NAME,
        instructions="Inspect and control an authorized Android device over ADB.",
    )

    def publish(event_type: str, **values: object) -> None:
        nonlocal viewer_sequence
        with viewer_lock:
            viewer_sequence += 1
            viewer_events.append({"id": viewer_sequence, "type": event_type, **values})

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
        with viewer_lock:
            history.appendleft(entry)
        publish("history", entry=entry)

    def call(action: str, **values: object) -> object:
        try:
            result = execute(action_args(action, adb_path=resolved_adb, timeout=timeout, **values))
            record(action, values, True)
            return result
        except ControlError as error:
            record(action, values, False, str(error))
            raise ToolError(str(error)) from None

    def capture_png(target: str | None = None) -> tuple[str, bytes]:
        adb = Adb(resolved_adb, timeout)
        selected = resolve_serial(adb, target)
        png = adb.run(["exec-out", "screencap", "-p"], serial=selected, timeout=60)
        if not png.startswith(PNG_MAGIC):
            raise ControlError("screenshot response is not a valid PNG")
        return selected, png

    def cache_screenshot(target: str, png: bytes) -> int:
        with viewer_lock:
            revision = int(latest_screenshot.get("revision", 0)) + 1
            latest_description.clear()
            latest_screenshot.update({
                "revision": revision,
                "target": target,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "png": png,
            })
        publish("screenshot", revision=revision, target=target)
        return revision

    def cache_description(target: str, elements: list[dict[str, object]]) -> None:
        description = {
            "target": target,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "elements": elements,
        }
        with viewer_lock:
            latest_description.clear()
            latest_description.update(description)
        publish("description", **description)

    @server.tool(name="list_targets")
    def list_targets_tool() -> list[dict[str, str]]:
        """List Android devices visible to ADB."""
        return call("list-targets")  # type: ignore[return-value]

    @server.tool(name="set_default_target")
    def set_default_target_tool(target: str) -> dict[str, object]:
        """Remember a connected, ready target as the automatic choice for one hour."""
        return call("set-default-target", target=target)  # type: ignore[return-value]

    @server.tool(name="status")
    def status_tool(target: str | None = None) -> dict[str, object]:
        """Return device state, identity, Android version, and battery details."""
        return call("status", target=target)  # type: ignore[return-value]

    @server.tool(name="list_packages")
    def list_packages_tool(target: str | None = None) -> dict[str, object]:
        """List package names currently installed on the device."""
        return call("list-packages", target=target)  # type: ignore[return-value]

    @server.tool(name="current_focus")
    def current_focus_tool(target: str | None = None) -> dict[str, object]:
        """Return the package and activity currently focused by Android."""
        return call("current-focus", target=target)  # type: ignore[return-value]

    @server.tool(name="screenshot")
    def screenshot_tool(target: str | None = None):
        """Capture the current device screen as a PNG image."""
        try:
            selected, png = capture_png(target)
            cache_screenshot(selected, png)
            record("screenshot", {"target": target}, True)
            return Image(data=png, format="png")
        except ControlError as error:
            record("screenshot", {"target": target}, False, str(error))
            raise ToolError(str(error)) from None

    @server.tool(name="describe_screen")
    def describe_screen_tool(
        format: Literal["original", "json"] = "json",
        target: str | None = None,
    ) -> dict[str, object]:
        """Return raw uiautomator XML or parsed JSON elements (default)."""
        result = call("describe-screen", format=format, target=target)
        elements = description_elements(result)  # type: ignore[arg-type]
        cache_description(result["target"], elements)  # type: ignore[index]
        return result  # type: ignore[return-value]

    @server.tool(name="tap")
    def tap_tool(x: int, y: int, target: str | None = None) -> dict[str, object]:
        """Tap non-negative device-pixel coordinates."""
        result = call("tap", x=x, y=y, target=target)
        publish("gesture", gesture="tap", target=result["target"], x=x, y=y)  # type: ignore[index]
        return result  # type: ignore[return-value]

    @server.tool(name="swipe")
    def swipe_tool(
        x1: int, y1: int, x2: int, y2: int,
        duration: int = 300, target: str | None = None,
    ) -> dict[str, object]:
        """Swipe between device-pixel coordinates."""
        result = call("swipe", x1=x1, y1=y1, x2=x2, y2=y2, duration=duration, target=target)
        publish(
            "gesture", gesture="swipe", target=result["target"],  # type: ignore[index]
            x1=x1, y1=y1, x2=x2, y2=y2, duration=duration,
        )
        return result  # type: ignore[return-value]

    @server.tool(name="long_press")
    def long_press_tool(
        x: int, y: int, duration: int = 1000, target: str | None = None,
    ) -> dict[str, object]:
        """Long-press device-pixel coordinates."""
        result = call("long-press", x=x, y=y, duration=duration, target=target)
        publish(
            "gesture", gesture="long_press", target=result["target"],  # type: ignore[index]
            x=x, y=y, duration=duration,
        )
        return result  # type: ignore[return-value]

    @server.tool(name="type_text")
    def type_text_tool(text: str, target: str | None = None) -> dict[str, object]:
        """Type text into the focused Android input."""
        return call("type-text", text=text, target=target)  # type: ignore[return-value]

    @server.tool(name="press_key")
    def press_key_tool(
        key: Literal["home", "back", "enter", "recents"],
        target: str | None = None,
    ) -> dict[str, object]:
        """Press home, back, enter, or recents."""
        return call("press-key", key=key, target=target)  # type: ignore[return-value]

    @server.tool(name="press_home")
    def press_home_tool(target: str | None = None) -> dict[str, object]:
        """Press the Android home button."""
        return call("home", target=target)  # type: ignore[return-value]

    @server.tool(name="press_back")
    def press_back_tool(target: str | None = None) -> dict[str, object]:
        """Press the Android back button."""
        return call("back", target=target)  # type: ignore[return-value]

    @server.tool(name="launch_app")
    def launch_app_tool(
        name: str,
        force_restart: bool = False,
        target: str | None = None,
    ) -> dict[str, object]:
        """Launch an installed app, optionally force-stopping it first."""
        return call(
            "launch-app", name=name, force_restart=force_restart, target=target,
        )  # type: ignore[return-value]

    @server.tool(name="force_stop_app")
    def force_stop_app_tool(name: str, target: str | None = None) -> dict[str, object]:
        """Force-stop an installed app by exact package or package-name fragment."""
        return call("force-stop-app", name=name, target=target)  # type: ignore[return-value]

    @server.tool(name="open_url")
    def open_url_tool(url: str, target: str | None = None) -> dict[str, object]:
        """Open an absolute HTTP or HTTPS URL on the device."""
        return call("open-url", url=url, target=target)  # type: ignore[return-value]

    @server.custom_route("/", methods=["GET"])
    async def dashboard(_request: Request) -> Response:
        return FileResponse(web_root / "index.html", media_type="text/html")

    @server.custom_route("/api/info", methods=["GET"])
    async def server_info(_request: Request) -> Response:
        return JSONResponse({
            "name": SERVER_NAME,
            "api_version": SERVER_API_VERSION,
            "pid": os.getpid(),
        })

    @server.custom_route("/api/viewer/screenshot", methods=["GET"])
    async def dashboard_screenshot(request: Request) -> Response:
        with viewer_lock:
            png = latest_screenshot.get("png")
            revision = latest_screenshot.get("revision")
        if not isinstance(png, bytes):
            return JSONResponse(
                {"ok": False, "error": "no screenshot has been captured"},
                status_code=404,
            )
        requested_revision = request.query_params.get("revision")
        if requested_revision is not None and requested_revision != str(revision):
            return JSONResponse(
                {"ok": False, "error": "screenshot revision is no longer available"},
                status_code=409,
            )
        return Response(
            png,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Screenshot-Revision": str(revision),
            },
        )

    @server.custom_route("/api/viewer/events", methods=["GET"])
    async def dashboard_events(request: Request) -> Response:
        requested_after = request.query_params.get("after")
        if requested_after is None:
            requested_after = request.headers.get("last-event-id")
        with viewer_lock:
            current_sequence = viewer_sequence
        try:
            after = int(requested_after) if requested_after is not None else current_sequence
        except (TypeError, ValueError):
            after = current_sequence

        async def stream():
            nonlocal after
            with viewer_lock:
                initial = {
                    "type": "state",
                    "screenshot": {
                        key: value for key, value in latest_screenshot.items() if key != "png"
                    },
                    "description": dict(latest_description),
                }
            yield "event: viewer\ndata: " + json.dumps(initial, ensure_ascii=False) + "\n\n"
            idle = 0
            while not await request.is_disconnected():
                with viewer_lock:
                    pending = [event.copy() for event in viewer_events if int(event["id"]) > after]
                if pending:
                    idle = 0
                    for event in pending:
                        after = int(event["id"])
                        yield (
                            f"id: {after}\nevent: viewer\ndata: "
                            + json.dumps(event, ensure_ascii=False)
                            + "\n\n"
                        )
                else:
                    idle += 1
                    if idle >= 60:
                        idle = 0
                        yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @server.custom_route("/api/history", methods=["GET"])
    async def dashboard_history(_request: Request) -> Response:
        with viewer_lock:
            entries = list(history)
        return JSONResponse({"history": entries})

    @server.custom_route("/api/tools", methods=["GET"])
    async def dashboard_tools(_request: Request) -> Response:
        tools = await server.list_tools()
        return JSONResponse({
            "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
        })

    @server.custom_route("/api/tools/call", methods=["POST"])
    async def dashboard_tool_call(request: Request) -> Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            return JSONResponse({"ok": False, "error": "name must be a tool name"}, status_code=400)
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            return JSONResponse({"ok": False, "error": "arguments must be an object"}, status_code=400)
        try:
            result = await server.call_tool(payload["name"], arguments)
            body = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            has_image = False
            for content in body.get("content", []):
                if content.get("type") == "image":
                    has_image = True
                    content.pop("data", None)
                    content["cachedForViewer"] = True
            response: dict[str, object] = {"ok": True, "result": body}
            if has_image:
                with viewer_lock:
                    response["viewer"] = {
                        key: value for key, value in latest_screenshot.items() if key != "png"
                    }
            return JSONResponse(response)
        except ToolError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    write_server_file(advertised_server_url(host, port))
    try:
        server.run(transport="streamable-http", host=host, port=port)
    finally:
        clear_server_file()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mcp:
        action_fields = set().union(*ACTION_FIELDS.values())
        if args.direct or args.target or any(getattr(args, field) is not None for field in action_fields):
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
        discovered = None if args.direct or args.adb else discover_server()
        emit(execute_via_server(args, discovered) if discovered else execute(args))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AmbiguousTargetError as error:
        print(f"warning: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except ControlError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
