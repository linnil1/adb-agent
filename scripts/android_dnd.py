"""Read and verify Android Do Not Disturb controls independently of volume."""
from __future__ import annotations

import re
import time

if __package__:
    from .android_core import Adb, ControlError, shell
else:
    from android_core import Adb, ControlError, shell


DND_MODES = {"off": "ZEN_MODE_OFF", "priority": "ZEN_MODE_IMPORTANT_INTERRUPTIONS",
             "alarms": "ZEN_MODE_ALARMS", "none": "ZEN_MODE_NO_INTERRUPTIONS"}


def parse_dnd_state(output: str) -> dict[str, object]:
    zen = re.search(r"^\s*- zen mode:\s*(\w+)", output, re.M)
    internal = re.search(r"^\s*- mode \(internal\) = (NORMAL|VIBRATE|SILENT)\b", output, re.M)
    external = re.search(r"^\s*- mode \(external\) = (NORMAL|VIBRATE|SILENT)\b", output, re.M)
    raw = zen[1] if zen else None
    mode = next((name for name, value in DND_MODES.items() if value == raw), None)
    return {"mode": mode, "zen_mode": raw,
            "ringer_mode": internal[1].lower() if internal else None,
            "ringer_mode_external": external[1].lower() if external else None}


def supports_dnd(adb: Adb, serial: str) -> bool:
    # The no-argument command prints help but commonly exits with status 255.
    output = shell(adb, serial, ["cmd notification 2>&1 || true"]).decode("utf-8", "replace")
    return re.search(r"^\s*set_dnd\s+", output, re.M) is not None


def _read_state(adb: Adb, serial: str, *, timeout: float | None = None) -> dict[str, object]:
    output = shell(adb, serial, ["dumpsys", "audio"], timeout=timeout).decode("utf-8", "replace")
    return parse_dnd_state(output)


def get_dnd(adb: Adb, serial: str) -> dict[str, object]:
    errors = []
    try:
        state = _read_state(adb, serial)
    except ControlError as error:
        state = parse_dnd_state("")
        errors.append(f"DND state: {error}")
    if state["mode"] is None:
        errors.append("DND mode is unavailable or unrecognized")
    try:
        supported = supports_dnd(adb, serial)
    except ControlError as error:
        supported = None
        errors.append(f"DND command support: {error}")
    return {"target": serial, **state, "supported": supported, "errors": errors}


def set_dnd(adb: Adb, serial: str, mode: str) -> dict[str, object]:
    if mode not in DND_MODES:
        raise ControlError("DND mode must be off, priority, alarms, or none")
    if not supports_dnd(adb, serial):
        raise ControlError("this device does not advertise cmd notification set_dnd")
    if _read_state(adb, serial)["mode"] is None:
        raise ControlError("cannot read DND state; no change was sent")
    response = shell(adb, serial, ["cmd", "notification", "set_dnd", mode]).decode("utf-8", "replace").strip()
    if re.search(r"(?:^|\n)\s*(?:error|unknown command|exception|usage)\b", response, re.I):
        raise ControlError("DND command returned: " + response[:200])
    deadline = time.monotonic() + min(5.0, adb.timeout)
    observed = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            remaining = max(0.001, min(adb.timeout, deadline - time.monotonic()))
            observed = _read_state(adb, serial, timeout=remaining)
            if time.monotonic() <= deadline and observed["mode"] == mode:
                return {"target": serial, "requested": {"mode": mode}, "verified": True,
                        **observed, "supported": True, "errors": []}
        except ControlError as error:
            last_error = str(error)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.2, remaining))
    detail = last_error or str(observed)
    raise ControlError(f"set-dnd was sent but not verified: {detail}; read get-dnd before retrying")
