"""Dependency-free ADB operations and CLI argument validation."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import selectors
import shutil
import signal
import struct
import subprocess
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ACTIONS = (
    "list-targets", "set-default-target", "status", "list-packages", "current-focus",
    "screenshot", "describe-screen", "tap", "swipe", "long-press", "type-text",
    "press-key", "launch-app", "force-stop-app", "open-url",
    "start-screen-record", "stop-screen-record", "multi-touch", "zoom",
    "logs", "clear-logs",
    "screen-status", "set-screen", "rotate-screen",
    "wait-for-ui", "connect", "disconnect", "pair",
    "get-volume", "set-volume", "get-dnd", "set-dnd",
)
ACTION_FIELDS = {
    "list-targets": set(), "set-default-target": set(), "status": set(),
    "list-packages": set(), "current-focus": set(),
    "screenshot": {"output", "x1", "y1", "x2", "y2"},
    "describe-screen": {"format"}, "tap": {"x", "y"},
    "swipe": {"x1", "y1", "x2", "y2", "duration"},
    "multi-touch": {"fingers", "duration"},
    "zoom": {"x", "y", "direction", "distance", "duration"},
    "long-press": {"x", "y", "duration"}, "type-text": {"text"},
    "press-key": {"key"},
    "launch-app": {"name", "force_restart"}, "force-stop-app": {"name"},
    "open-url": {"url"},
    "start-screen-record": {"time_limit"}, "stop-screen-record": {"output"},
    "logs": {"kind", "name", "priority", "match", "lines", "output"},
    "clear-logs": set(),
    "screen-status": set(), "set-screen": {"state"},
    "rotate-screen": {"rotation"},
    "wait-for-ui": {"text", "description", "resource_id", "state", "wait_timeout"},
    "connect": {"address"}, "disconnect": {"address"}, "pair": {"address", "pair_code"},
    "get-volume": {"stream"},
    "set-volume": {"stream", "level", "direction", "muted", "ringer_mode"},
    "get-dnd": set(), "set-dnd": {"dnd_mode"},
}
AUDIO_STREAMS = {"media": 3, "ring": 2, "notification": 5, "alarm": 4, "voice-call": 0}
SERIAL_RE = re.compile(r"^[A-Za-z0-9.:\[\]_-]{1,259}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
COMPONENT_RE = re.compile(
    r"(?P<component>(?P<package>[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/"
    r"(?P<activity>\.?[A-Za-z0-9_.$]+))"
)
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
KEY_MAP = {
    "home": 3, "back": 4, "up": 19, "down": 20, "left": 21, "right": 22,
    "tab": 61, "enter": 66, "delete": 67, "recents": 187,
    "volume-up": 24, "volume-down": 25, "volume-mute": 164,
    "media-play": 126, "media-pause": 127, "media-play-pause": 85,
    "media-next": 87, "media-previous": 88,
}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
RECORD_PATH = "/data/local/tmp/android-control-record"
RECORD_BIT_RATE = 4_000_000
RECORD_MAX_SIDE = 1280
MAX_RECORD_SECONDS = 180
DEFAULT_TIMEOUT = 30.0
DEFAULT_TARGET_TTL = 60 * 60
STATE_FILE_ENV = "ANDROID_CONTROL_STATE_FILE"
SERVER_FILE_ENV = "ANDROID_CONTROL_SERVER_FILE"
SERVER_NAME = "android-device-control"
SERVER_API_VERSION = 2
_package_cache: dict[str, tuple[float, list[str]]] = {}
LOG_SCAN_BYTES = 4 * 1024 * 1024
LOG_OUTPUT_BYTES = 256 * 1024
LOG_PRIORITIES = "VDIWEF"
LOG_LINE_RE = re.compile(
    r"^\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]+): ?(.*)$"
)


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


def run_process(argv: list[str], timeout: float, *, input_data: bytes | None = None) -> bytes:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_data, timeout=timeout)
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


def run_process_tail(argv: list[str], timeout: float, limit: int) -> tuple[bytes, bool]:
    """Drain both pipes, retaining only the latest limit bytes of stdout."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    output, errors = bytearray(), bytearray()
    truncated = False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data:
                        output.extend(chunk)
                        if len(output) > limit:
                            del output[:len(output) - limit]
                            truncated = True
                    elif len(errors) < 4096:
                        errors.extend(chunk[:4096 - len(errors)])
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if isinstance(error, subprocess.TimeoutExpired):
            raise ControlError(f"adb timed out after {timeout:g}s") from None
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    if process.returncode:
        raise ControlError(scrub_error(bytes(errors), process.returncode))
    # A retained tail may begin midway through a UTF-8 character or log line.
    if truncated:
        _, _, output = output.partition(b"\n")
    return bytes(output), truncated


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

    def run_tail(self, args: list[str], *, serial: str) -> tuple[bytes, bool]:
        validate_serial(serial)
        return run_process_tail([self.executable, "-s", serial, *args], self.timeout, LOG_SCAN_BYTES)

    def run_input(self, args: list[str], data: bytes) -> bytes:
        return run_process([self.executable, *args], self.timeout, input_data=data)


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


def parse_ui_xml(xml: str, *, visible_only: bool = False) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = []
    for tag in extract_node_tags(xml):
        text = attribute(tag, "text")
        description = attribute(tag, "content-desc")
        resource_id = attribute(tag, "resource-id")
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
        if not text and not description and not resource_id and not clickable and not selected and not scrollable:
            continue
        bounds = BOUNDS_RE.fullmatch(attribute(tag, "bounds"))
        if not bounds:
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        if visible_only and (attribute(tag, "visible-to-user") == "false" or x2 <= x1 or y2 <= y1):
            continue
        elements.append({
            "label": text or description or class_name.rsplit(".", 1)[-1] or "element",
            "text": text,
            "description": description,
            "resource_id": resource_id,
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


def dump_ui(adb: Adb, serial: str, *, deadline: float | None = None) -> str:
    def budget() -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError("UI capture deadline reached")
        return min(adb.timeout, remaining)

    def xml_part(text: str) -> str:
        start = text.find("<?xml")
        if start < 0:
            start = text.find("<hierarchy")
        if start < 0:
            raise ControlError("UI capture did not return a hierarchy")
        end = text.find("</hierarchy>", start)
        return text[start:end + len("</hierarchy>")] if end >= 0 else text[start:]

    try:
        text = adb.run(
            ["exec-out", "uiautomator", "dump", "/dev/tty"], serial=serial, timeout=budget()
        ).decode("utf-8", "replace")
        return xml_part(text)
    except ControlError:
        pass
    # A fresh path prevents a failed dump from reusing an earlier screen's XML.
    remote = f"/sdcard/android-control-ui-{os.getpid()}-{time.time_ns()}.xml"
    script = f"uiautomator dump {remote} && cat {remote}; result=$?; rm -f {remote}; exit $result"
    return xml_part(shell(adb, serial, [script], timeout=budget()).decode("utf-8", "replace"))


def screenshot_region(
    x1: int | None, y1: int | None, x2: int | None, y2: int | None,
) -> tuple[int, int, int, int] | None:
    values = (x1, y1, x2, y2)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ControlError("a screenshot region needs all of x1, y1, x2, and y2")
    if min(values) < 0 or x2 <= x1 or y2 <= y1:  # type: ignore[operator]
        raise ControlError("screenshot region must satisfy 0 <= x1 < x2 and 0 <= y1 < y2")
    return x1, y1, x2, y2  # type: ignore[return-value]


def encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"".join(b"\x00" + row for row in rows), 6)
    return PNG_MAGIC + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def capture_screen(
    adb: Adb, serial: str, region: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, list[int]]:
    """Return a PNG and the full screen size; a region transfers only its rows."""
    if region is None:
        png = adb.run(["exec-out", "screencap", "-p"], serial=serial, timeout=60)
        if not png.startswith(PNG_MAGIC):
            raise ControlError("screenshot response is not a valid PNG")
        return png, list(struct.unpack(">II", png[16:24]))
    x1, y1, x2, y2 = region
    # Raw screencap is width, height, format[, colorspace] then RGBA rows.
    script = (
        "f=/data/local/tmp/android-control-screen-$$.raw; screencap \"$f\" || exit 1; "
        "set -- $(od -An -tu4 -N12 \"$f\"); w=$1; h=$2; "
        "s=$(($(wc -c < \"$f\") - w * h * 4)); echo \"$w $h $3 $s\"; "
        f"if [ {y2} -le $h ]; then tail -c +$((s + {y1} * w * 4 + 1)) \"$f\" "
        f"| head -c $(({y2 - y1} * w * 4)) | gzip -1; fi; rm -f \"$f\""
    )
    output = adb.run(["exec-out", script], serial=serial, timeout=60)
    line, _, raw = output.partition(b"\n")
    try:
        width, height, pixel_format, header_size = map(int, line.split())
    except ValueError:
        raise ControlError("raw screenshot header is invalid") from None
    if pixel_format != 1 or header_size not in (12, 16):
        raise ControlError("raw screenshot is not RGBA_8888")
    if x2 > width or y2 > height:
        raise ControlError(f"screenshot region exceeds the {width}x{height} screen")
    try:
        raw = zlib.decompress(raw, 31)
    except zlib.error:
        raise ControlError("raw screenshot could not be decompressed") from None
    stride = width * 4
    if len(raw) != (y2 - y1) * stride:
        raise ControlError("raw screenshot was truncated")
    rows = [raw[row * stride + x1 * 4:row * stride + x2 * 4] for row in range(y2 - y1)]
    return encode_png(x2 - x1, y2 - y1, rows), [width, height]


def start_screen_record(adb: Adb, serial: str, time_limit: int) -> None:
    # Detached on the device, so later adb calls (or CLI runs) can stop it.
    script = (
        f"f={RECORD_PATH}; "
        "if [ -f $f.pid ] && grep -q screenrecord /proc/$(cat $f.pid)/cmdline 2>/dev/null; "
        "then echo running; exit 0; fi; rm -f $f.mp4 $f.log; "
        # Keep the screen's aspect ratio; encoders often reject full resolution and letterbox.
        "s=$(wm size | tail -n 1 | sed 's/.*: //'); w=${s%x*}; h=${s#*x}; size=; "
        "case \"$w$h\" in *[!0-9]*|'') ;; *) l=$((w > h ? w : h)); "
        f"if [ $l -gt {RECORD_MAX_SIDE} ]; then w=$((w * {RECORD_MAX_SIDE} / l / 2 * 2)); "
        f"h=$((h * {RECORD_MAX_SIDE} / l / 2 * 2)); fi; size=\"--size ${{w}}x${{h}}\";; esac; "
        f"nohup screenrecord $size --time-limit {time_limit} --bit-rate {RECORD_BIT_RATE} $f.mp4 "
        "</dev/null >$f.log 2>&1 & echo $! > $f.pid; "
        "sleep 0.5; if [ -d /proc/$! ]; then echo started; else echo failed; cat $f.log; rm -f $f.pid; fi"
    )
    output = shell(adb, serial, [script]).decode("utf-8", "replace").strip()
    if output == "running":
        raise ControlError("a screen recording is already running")
    if output != "started":
        detail = output.removeprefix("failed").strip().splitlines()
        raise ControlError("screenrecord failed to start" + (f": {detail[-1][:160]}" if detail else ""))


def mp4_boxes(data: bytes, start: int, end: int):
    while start + 8 <= end:
        size, kind = struct.unpack(">I4s", data[start:start + 8])
        header = 8
        if size == 1 and start + 16 <= end:
            size, header = struct.unpack(">Q", data[start + 8:start + 16])[0], 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            return
        yield kind, start + header, start + size
        start += size


def mp4_duration(video: bytes) -> float | None:
    """Read the movie length from moov/mvhd."""
    for kind, start, end in mp4_boxes(video, 0, len(video)):
        if kind != b"moov":
            continue
        for child, body, child_end in mp4_boxes(video, start, end):
            if child != b"mvhd" or child_end - body < 32:
                continue
            if video[body] == 1:
                timescale, duration = struct.unpack(">IQ", video[body + 20:body + 32])
            else:
                timescale, duration = struct.unpack(">II", video[body + 12:body + 20])
            return round(duration / timescale, 2) if timescale else None
    return None


def stop_screen_record(adb: Adb, serial: str) -> tuple[bytes, float | None]:
    """Stop the recording (or collect one that hit its time limit); return MP4 and seconds."""
    script = (
        f"f={RECORD_PATH}; [ -f $f.pid ] || {{ echo none; exit 0; }}; p=$(cat $f.pid); "
        # SIGINT lets screenrecord finish the MP4 before exiting.
        "if grep -q screenrecord /proc/$p/cmdline 2>/dev/null; then kill -INT $p; i=0; "
        "while [ -d /proc/$p ] && [ $i -lt 50 ]; do sleep 0.2; i=$((i + 1)); done; fi; "
        "echo stopped; cat $f.mp4 2>/dev/null; rm -f $f.mp4 $f.pid $f.log"
    )
    output = adb.run(["exec-out", script], serial=serial, timeout=max(adb.timeout, 300))
    line, _, video = output.partition(b"\n")
    if line.strip() == b"none":
        raise ControlError("no screen recording is running")
    if line.strip() != b"stopped":
        raise ControlError("screen recording status is invalid")
    if video[4:8] != b"ftyp":
        raise ControlError("screen recording produced no valid MP4")
    return video, mp4_duration(video)


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


def parse_log_entries(text: str) -> list[dict]:
    entries: list[dict] = []
    for line in text.splitlines():
        match = LOG_LINE_RE.match(line)
        if match:
            pid, tid, priority, tag, message = match.groups()
            entries.append({"pid": int(pid), "tid": int(tid), "priority": priority,
                            "tag": tag.strip(), "message": message, "lines": [line]})
        elif entries and not line.startswith("---------"):
            entries[-1]["lines"].append(line)
            entries[-1]["message"] += "\n" + line
    return entries


def crash_reports(entries: list[dict]) -> list[list[dict]]:
    """Keep each emitter's crash together, including interleaved stack traces."""
    reports: list[list[dict]] = []
    active: dict[tuple, list[dict]] = {}
    for entry in entries:
        key = (entry["pid"], entry["tid"], entry["tag"])
        if key not in active or entry["message"].startswith(("FATAL EXCEPTION:", "*** *** ***")):
            active[key] = []
            reports.append(active[key])
        active[key].append(entry)
    return reports


def filter_log_text(text: str, *, crash: bool, package: str | None, pids: list[int],
                    priority: str, match: str | None, lines: int,
                    byte_limit: int = LOG_OUTPUT_BYTES) -> dict[str, object]:
    entries = parse_log_entries(text)
    threshold = LOG_PRIORITIES.index(priority)
    if crash:
        package_pattern = re.compile(
            r"(?:Process: |Cmdline: |>>> )" + re.escape(package) + r"(?::[^\s,<>]+)?(?=[\s,]|$)"
        ) if package else None
        selected = []
        for report in crash_reports(entries):
            messages = "\n".join(entry["message"] for entry in report)
            if package_pattern and not package_pattern.search(messages):
                continue
            if match is not None and match not in messages:
                continue
            if not any(LOG_PRIORITIES.index(entry["priority"]) >= threshold for entry in report):
                continue
            selected.extend(report)
        # Restore buffer order after collecting interleaved reports.
        chosen = {id(entry) for entry in selected}
        selected = [entry for entry in entries if id(entry) in chosen]
    else:
        selected = [entry for entry in entries
                    if (not package or entry["pid"] in pids)
                    and LOG_PRIORITIES.index(entry["priority"]) >= threshold
                    and (match is None or match in entry["message"] or match in entry["tag"])]
    rendered = [line for entry in selected for line in entry["lines"]]
    matched_lines = len(rendered)
    rendered = rendered[-lines:]
    encoded = ("\n".join(rendered) + ("\n" if rendered else "")).encode("utf-8")
    truncated = matched_lines > lines
    if len(encoded) > byte_limit:
        encoded = encoded[-byte_limit:]
        _, _, encoded = encoded.partition(b"\n")
        truncated = True
    return {"text": encoded.decode("utf-8"), "line_count": len(encoded.splitlines()),
            "matched_lines": matched_lines, "truncated": truncated}


def save_log_output(result: dict[str, object], output: str | None) -> dict[str, object]:
    if output is not None:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result["text"], encoding="utf-8")
        result = {**result, "path": str(path)}
    return result


def capture_logs(adb: Adb, serial: str, args: argparse.Namespace) -> dict[str, object]:
    crash = args.kind == "crash"
    package = None
    pids: list[int] = []
    if args.name:
        package = args.name if PACKAGE_RE.fullmatch(args.name) else resolve_package(adb, serial, args.name)
        if not crash:
            processes = shell(adb, serial, ["ps", "-A", "-o", "PID,NAME"]).decode("utf-8", "replace")
            for line in processes.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit() and (
                    parts[1] == package or parts[1].startswith(package + ":")
                ):
                    pids.append(int(parts[0]))
            if not pids:
                raise ControlError(f"app is not running: {package}; use logs --kind crash for earlier crashes")
    buffers = "crash" if crash else "main,system,crash"
    command = ["logcat", "-d", "-b", buffers, "-v", "threadtime"]
    if len(pids) == 1:
        command += [f"--pid={pids[0]}"]
    # Filter in Python so crash matching retains the surrounding report.
    command += ["'*:V'"]
    raw, scan_truncated = adb.run_tail(command, serial=serial)
    result = filter_log_text(raw.decode("utf-8", "replace"), crash=crash, package=package,
                             pids=pids, priority=args.priority, match=args.match, lines=args.lines)
    result["truncated"] = result["truncated"] or scan_truncated
    return save_log_output({
        "target": serial, "action": args.action, "kind": args.kind, "package": package, "pids": pids,
        "buffers": buffers.split(","), "priority": args.priority, "match": args.match,
        "line_limit": args.lines, "scan_truncated": scan_truncated, **result,
    }, args.output)


def require(value: object, flag: str) -> object:
    if value is None:
        raise ControlError(f"{flag} is required for this action")
    return value


def validate_action_args(args: argparse.Namespace) -> None:
    required = {
        "set-screen": ("state",),
        "rotate-screen": ("rotation",),
        "zoom": ("x", "y", "direction"),
        "multi-touch": ("fingers",),
        "tap": ("x", "y"),
        "swipe": ("x1", "y1", "x2", "y2"),
        "long-press": ("x", "y"),
        "type-text": ("text",),
        "press-key": ("key",),
        "set-dnd": ("dnd_mode",),
        "launch-app": ("name",),
        "force-stop-app": ("name",),
        "open-url": ("url",),
        # Required so a recording always ends, even if the connection drops.
        "start-screen-record": ("time_limit",),
    }.get(args.action, ())
    if args.action == "set-default-target":
        require(args.target, "--target")
    for field in required:
        require(getattr(args, field), "--" + field.replace("_", "-"))
    optional_fields = set().union(*ACTION_FIELDS.values())
    extras = [field for field in optional_fields - ACTION_FIELDS[args.action] if getattr(args, field) is not None]
    if extras:
        flags = ", ".join("--" + field.replace("_", "-") for field in sorted(extras))
        raise ControlError(f"argument(s) not valid for {args.action}: {flags}")


def zoom_paths(direction: str, x: int, y: int, distance: int, width: int, height: int) -> list[dict[str, int]]:
    if direction not in {"in", "out"}:
        raise ControlError("zoom direction must be in or out")
    near, far = 100, 100 + distance
    if not (0 <= x - far and x + far < width and 0 <= y < height):
        raise ControlError("zoom gesture exceeds the display; move its center or reduce --distance")
    start, end = (near, far) if direction == "in" else (far, near)
    return [{"x1": x + sign * start, "y1": y, "x2": x + sign * end, "y2": y}
            for sign in (-1, 1)]


def inject_multi_touch(adb: Adb, serial: str, fingers: list[dict[str, int]], duration: int) -> None:
    helper = Path(__file__).with_name("multitouch") / "multitouch.dex"
    if not helper.is_file():
        raise ControlError("multi-touch helper missing; run python3 scripts/multitouch/build.py")
    remote = "/data/local/tmp/android-control-multitouch.dex"
    adb.run(["push", str(helper), remote], serial=serial)
    coordinates = [str(finger[key]) for finger in fingers for key in ("x1", "y1", "x2", "y2")]
    shell(adb, serial, [f"CLASSPATH={remote}", "app_process", "/", "MultiTouch", str(duration), *coordinates])


def device_action(args: argparse.Namespace, adb: Adb) -> object:
    if args.action == "set-default-target":
        return set_default_target(adb, args.target)
    serial = resolve_serial(adb, args.target)
    action = args.action
    if action in {"get-dnd", "set-dnd"}:
        if __package__:
            from .android_dnd import get_dnd, set_dnd
        else:
            from android_dnd import get_dnd, set_dnd
        return get_dnd(adb, serial) if action == "get-dnd" else set_dnd(adb, serial, args.dnd_mode)
    elif action in {"get-volume", "set-volume"}:
        if __package__:
            from .android_audio import get_volume, set_volume
        else:
            from android_audio import get_volume, set_volume
        if action == "get-volume":
            return get_volume(adb, serial, args.stream)
        return set_volume(adb, serial, stream=args.stream, level=args.level,
                          direction=args.direction, muted=args.muted, ringer_mode=args.ringer_mode)
    elif action == "wait-for-ui":
        if __package__:
            from .android_wait import wait_for_ui
        else:
            from android_wait import wait_for_ui
        return wait_for_ui(adb, serial, text=args.text, description=args.description,
                           resource_id=args.resource_id, state=args.state, wait_timeout=args.wait_timeout)
    elif action in {"screen-status", "set-screen", "rotate-screen"}:
        if __package__:
            from .android_screen import control_screen, screen_status
        else:
            from android_screen import control_screen, screen_status
        if action == "screen-status":
            return screen_status(adb, serial)
        return control_screen(adb, serial, action, args.rotation, state=args.state)
    elif action == "logs":
        return capture_logs(adb, serial, args)
    elif action == "clear-logs":
        shell(adb, serial, ["logcat", "-b", "main,system,crash", "-c"])
        return {"target": serial, "action": action, "buffers": ["main", "system", "crash"]}
    elif action == "status":
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
        region = screenshot_region(args.x1, args.y1, args.x2, args.y2)
        png, _ = capture_screen(adb, serial, region)
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        result = {"target": serial, "path": str(output), "bytes": len(png)}
        if region:
            result["region"] = list(region)
        return result
    elif action == "describe-screen":
        return describe_ui(serial, dump_ui(adb, serial), args.format or "json")
    elif action == "tap":
        shell(adb, serial, ["input", "tap", str(args.x), str(args.y)])
        return {"target": serial, "action": action, "point": [args.x, args.y]}
    elif action == "swipe":
        shell(adb, serial, ["input", "swipe", str(args.x1), str(args.y1), str(args.x2), str(args.y2), str(args.duration)])
        return {"target": serial, "action": action, "from": [args.x1, args.y1], "to": [args.x2, args.y2], "duration_ms": args.duration}
    elif action == "multi-touch":
        inject_multi_touch(adb, serial, args.fingers, args.duration)
        return {"target": serial, "action": action, "fingers": args.fingers, "duration_ms": args.duration}
    elif action == "zoom":
        _, screen = capture_screen(adb, serial)
        fingers = zoom_paths(args.direction, args.x, args.y, args.distance, *screen)
        inject_multi_touch(adb, serial, fingers, args.duration)
        return {"target": serial, "action": action, "direction": args.direction, "center": [args.x, args.y],
                "distance": args.distance, "fingers": fingers, "duration_ms": args.duration}
    elif action == "long-press":
        shell(adb, serial, ["input", "swipe", str(args.x), str(args.y), str(args.x), str(args.y), str(args.duration)])
        return {"target": serial, "action": action, "point": [args.x, args.y], "duration_ms": args.duration}
    elif action == "type-text":
        shell(adb, serial, ["input", "text", shell_quote(args.text.replace(" ", "%s"))])
        return {"target": serial, "action": action, "characters": len(args.text)}
    elif action == "press-key":
        key = args.key
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
    elif action == "start-screen-record":
        start_screen_record(adb, serial, args.time_limit)
        return {"target": serial, "action": action, "time_limit": args.time_limit}
    elif action == "stop-screen-record":
        video, seconds = stop_screen_record(adb, serial)
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(video)
        return {"target": serial, "path": str(output), "bytes": len(video), "seconds": seconds}
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
    parser.add_argument("--distance", type=int, help="zoom movement per finger in pixels (default 150)")
    parser.add_argument("--fingers", help='JSON array of 2-10 {"x1":0,"y1":0,"x2":100,"y2":100} finger paths')
    parser.add_argument("--text")
    parser.add_argument("--key", choices=sorted(KEY_MAP))
    parser.add_argument("--name")
    parser.add_argument("--url")
    parser.add_argument("--time-limit", type=int, help=f"recording seconds, 1-{MAX_RECORD_SECONDS}")
    parser.add_argument("--priority", choices=list(LOG_PRIORITIES), help="minimum log priority (default V)")
    parser.add_argument("--kind", choices=("standard", "crash"), help="log kind (default standard)")
    parser.add_argument("--match", help="literal log text filter")
    parser.add_argument("--lines", type=int, help="maximum returned log lines, 1-5000 (default 200)")
    parser.add_argument("--rotation", choices=("0", "90", "180", "270", "auto"),
                        help="rotation relative to natural orientation, or auto")
    parser.add_argument("--description", help="exact accessibility description for wait-for-ui")
    parser.add_argument("--resource-id", help="exact resource ID for wait-for-ui")
    parser.add_argument("--state", choices=("awake", "asleep", "present", "gone", "enabled"),
                        help="set-screen: awake/asleep; wait-for-ui: present/gone/enabled")
    parser.add_argument("--wait-timeout", type=float, help="UI wait budget in seconds, >0 to 300 (default 30)")
    parser.add_argument("--address", help="ADB endpoint HOST:PORT, or [IPv6]:PORT, on the server's network")
    parser.add_argument("--pair-code", help="six-digit pairing code; interactive CLI prompts when omitted")
    parser.add_argument("--stream", choices=tuple(AUDIO_STREAMS), help="get-volume: all streams by default; set-volume: media by default")
    parser.add_argument("--level", type=int, help="volume index within the phone's reported range")
    parser.add_argument("--direction", choices=("in", "out", "raise", "lower"),
                        help="zoom: in/out; set-volume: raise/lower")
    parser.add_argument("--muted", choices=("true", "false"), help="explicit stream mute state")
    parser.add_argument("--ringer-mode", choices=("normal", "vibrate", "silent"))
    parser.add_argument("--dnd-mode", choices=("off", "priority", "alarms", "none"), help="Do Not Disturb interruption filter")
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
    if args.action == "set-screen" and args.state not in {"awake", "asleep"}:
        raise ControlError("set-screen state must be awake or asleep")
    if args.action == "set-volume":
        if isinstance(args.muted, str):
            args.muted = args.muted == "true"
        if args.stream is None:
            args.stream = "media"
        if __package__:
            from .android_audio import validate_volume_request
        else:
            from android_audio import validate_volume_request
        validate_volume_request(args.stream, args.level, args.direction, args.muted, args.ringer_mode)
    if args.action in {"connect", "disconnect", "pair"}:
        if __package__:
            from .android_connection import validate_address
        else:
            from android_connection import validate_address
        require(args.address, "--address")
        args.address = validate_address(args.address)
        if args.target is not None:
            raise ControlError("connection actions use --address, not --target")
        if args.action == "pair":
            require(args.pair_code, "--pair-code")
            if not re.fullmatch(r"[0-9]{6}", args.pair_code):
                raise ControlError("pairing code must contain exactly six digits")
    if args.action == "wait-for-ui":
        selectors = [args.text, args.description, args.resource_id]
        if not any(value is not None for value in selectors):
            raise ControlError("wait-for-ui requires --text, --description, or --resource-id")
        if any(value == "" for value in selectors):
            raise ControlError("UI selectors must not be empty")
        if args.state is None:
            args.state = "present"
        if args.state not in {"present", "gone", "enabled"}:
            raise ControlError("wait-for-ui state must be present, gone, or enabled")
        if args.wait_timeout is None:
            args.wait_timeout = 30.0
        if not math.isfinite(args.wait_timeout) or not 0 < args.wait_timeout <= 300:
            raise ControlError("--wait-timeout must be greater than 0 and at most 300 seconds")
    if args.action == "logs":
        if args.kind is None:
            args.kind = "standard"
        if args.kind not in {"standard", "crash"}:
            raise ControlError("log kind must be standard or crash")
        if args.lines is None:
            args.lines = 200
        if not 1 <= args.lines <= 5000:
            raise ControlError("--lines must be between 1 and 5000")
        if args.priority is None:
            args.priority = "V"
    if args.action == "screenshot":
        screenshot_region(args.x1, args.y1, args.x2, args.y2)
    if args.action in {"swipe", "long-press", "multi-touch"} and args.duration is None:
        args.duration = 1000 if args.action == "long-press" else 300
    if args.action == "zoom":
        if args.direction not in {"in", "out"}:
            raise ControlError("zoom direction must be in or out")
        if args.distance is None:
            args.distance = 150
        if args.duration is None:
            args.duration = 500
        if not 1 <= args.distance <= 65535:
            raise ControlError("--distance must be between 1 and 65535 pixels")
        if not 1 <= args.duration <= 3000:
            raise ControlError("zoom duration must be between 1 and 3000 ms")
        if args.x > 65535 or args.y > 65535:
            raise ControlError("zoom coordinates must be between 0 and 65535")
    if args.action == "multi-touch":
        if isinstance(args.fingers, str):
            try:
                args.fingers = json.loads(args.fingers)
            except json.JSONDecodeError:
                raise ControlError("--fingers must be a JSON array") from None
        if not isinstance(args.fingers, list) or not 2 <= len(args.fingers) <= 10:
            raise ControlError("--fingers must contain 2-10 finger paths")
        for finger in args.fingers:
            if not isinstance(finger, dict) or set(finger) != {"x1", "y1", "x2", "y2"}:
                raise ControlError("each finger needs exactly x1, y1, x2, y2")
            if any(type(value) is not int or not 0 <= value <= 65535 for value in finger.values()):
                raise ControlError("finger coordinates must be integers between 0 and 65535")
        if not 1 <= args.duration <= 3000:
            raise ControlError("multi-touch duration must be between 1 and 3000 ms")
    if args.time_limit is not None and not 1 <= args.time_limit <= MAX_RECORD_SECONDS:
        raise ControlError(f"--time-limit must be between 1 and {MAX_RECORD_SECONDS} seconds")
    if args.action == "stop-screen-record" and args.output is None:
        args.output = "android-screen.mp4"
    if args.action == "screenshot" and args.output is None:
        args.output = "android-screen.png"


def execute(args: argparse.Namespace) -> object:
    prepare_action(args)
    adb = Adb(resolve_adb(args.adb), args.timeout)
    if args.action in {"connect", "disconnect", "pair"}:
        if __package__:
            from .android_connection import connection_action
        else:
            from android_connection import connection_action
        return connection_action(adb, args.action, args.address, args.pair_code)
    if args.action == "list-targets":
        return list_targets(adb)
    return device_action(args, adb)

def action_args(action: str, *, adb_path: str | None = None, timeout: float = DEFAULT_TIMEOUT, **values: object) -> argparse.Namespace:
    argv = ["--action", action, "--timeout", str(timeout)]
    if adb_path:
        argv += ["--adb", adb_path]
    for name, value in values.items():
        if name == "muted" and isinstance(value, bool):
            argv.append("--muted=" + str(value).lower())
        elif isinstance(value, bool):
            if value:
                argv.append("--" + name.replace("_", "-"))
        elif value is not None:
            encoded = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            argv.append("--" + name.replace("_", "-") + "=" + encoded)
    return build_parser().parse_args(argv)
