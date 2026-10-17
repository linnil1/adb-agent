"""Screen state inspection and verified power/rotation control over ADB."""
from __future__ import annotations

import re
import time

if __package__:
    from .android_core import Adb, ControlError, shell
else:
    from android_core import Adb, ControlError, shell


def parse_screen_state(power: str, display: str, rotation: str) -> dict[str, object]:
    state: dict[str, object] = {
        "interactive": None, "wakefulness": "unknown", "display_state": "unknown",
        "width": None, "height": None, "rotation": None,
        "rotation_mode": "unknown", "locked_rotation": None,
    }
    wake = re.search(r"^\s*mWakefulness=(\w+)", power, re.M)
    if wake:
        state["wakefulness"] = wake[1].lower()
    interactive = re.search(r"^\s*mInteractive=(true|false)\b", power + "\n" + display, re.M)
    if interactive:
        state["interactive"] = interactive[1] == "true"
    elif state["wakefulness"] in {"awake", "dreaming", "asleep", "dozing"}:
        state["interactive"] = state["wakefulness"] in {"awake", "dreaming"}

    # Prefer effective logical geometry over the panel's physical/base geometry.
    candidates: list[str] = []
    primary = False
    for line in display.splitlines():
        display_id = re.match(r"\s*mDisplayId=\s*(\d+)\s*$", line)
        if display_id:
            primary = display_id[1] == "0"
        if "DisplayInfo{" not in line:
            continue
        explicit_id = re.search(r"\bdisplayId[ =]+(\d+)\b", line)
        if (explicit_id and explicit_id[1] == "0") or (
            not explicit_id and (primary or "FLAG_DEFAULT_DISPLAY" in line)
        ):
            candidates.append(line)
    if candidates:
        info = min(candidates, key=lambda line:
                   0 if "mOverrideDisplayInfo=" in line else
                   2 if "mBaseDisplayInfo=" in line else 1)
        size = re.search(r"\breal (\d+) x (\d+)", info)
        angle = re.search(r"\brotation ([0-3])\b", info)
        # Window-manager override geometry can carry a stale power state.
        power_info = next((line for line in candidates if "mBaseDisplayInfo=" in line), info)
        power_state = re.search(r"\bstate ([A-Z_]+)\b", power_info)
        if size:
            state["width"], state["height"] = int(size[1]), int(size[2])
        if angle:
            state["rotation"] = int(angle[1]) * 90
        if power_state:
            state["display_state"] = power_state[1].lower().replace("_", "-")
    else:
        # Some older dumps have device info only. Its state is useful, but its
        # physical dimensions/rotation are not necessarily the logical display's.
        for line in display.splitlines():
            if "DisplayDeviceInfo{" in line and "FLAG_DEFAULT_DISPLAY" in line:
                power_state = re.search(r"\bstate ([A-Z_]+)\b", line)
                if power_state:
                    state["display_state"] = power_state[1].lower().replace("_", "-")
                break

    mode = rotation.strip()
    if mode == "free":
        state["rotation_mode"] = "auto"
    else:
        lock = re.fullmatch(r"lock ([0-3])", mode)
        if lock:
            state["rotation_mode"] = "locked"
            state["locked_rotation"] = int(lock[1]) * 90
    return state


def screen_status(adb: Adb, serial: str, *, deadline: float | None = None) -> dict[str, object]:
    values, errors = {}, []
    commands = {
        "power": ["dumpsys", "power"],
        "display": ["dumpsys", "display"],
        "rotation": ["wm", "user-rotation"],
    }
    for name, command in commands.items():
        try:
            timeout = None
            if deadline is not None:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise ControlError("screen verification deadline reached")
                timeout = min(adb.timeout, timeout)
            values[name] = shell(adb, serial, command, timeout=timeout).decode("utf-8", "replace")
        except ControlError as error:
            values[name] = ""
            errors.append(f"{name}: {error}")
    if len(errors) == len(commands):
        raise ControlError("cannot read screen state: " + "; ".join(errors))
    return {"target": serial, **parse_screen_state(**values), "errors": errors}


def control_screen(adb: Adb, serial: str, action: str,
                   rotation: str | None = None) -> dict[str, object]:
    if action in {"wake-screen", "sleep-screen"}:
        desired = action == "wake-screen"
        shell(adb, serial, ["input", "keyevent", "KEYCODE_WAKEUP" if desired else "KEYCODE_SLEEP"])
    elif action == "rotate-screen":
        if rotation not in {"auto", "0", "90", "180", "270"}:
            raise ControlError("rotation must be auto, 0, 90, 180, or 270")
        command = ["wm", "user-rotation"]
        command += ["free"] if rotation == "auto" else ["lock", str(int(rotation) // 90)]
        response = shell(adb, serial, command).decode("utf-8", "replace").strip()
        if response:
            # Some wm versions return an error message with a zero exit code.
            raise ControlError("rotation command returned: " + response[:200])
    else:
        raise ControlError("unsupported screen action")

    deadline = time.monotonic() + min(5.0, adb.timeout)
    observed = None
    while True:
        try:
            latest = screen_status(adb, serial, deadline=deadline)
        except ControlError:
            if observed is None:
                raise
            break
        if latest.get("errors") and observed is not None:
            observed = {**observed, "errors": latest["errors"]}
            break
        observed = latest
        if action in {"wake-screen", "sleep-screen"}:
            confirmed = observed["interactive"] is desired
            if confirmed:
                return {**observed, "action": action, "verified": True}
        else:
            setting_applied = (
                observed["rotation_mode"] == "auto" if rotation == "auto" else
                observed["rotation_mode"] == "locked" and observed["locked_rotation"] == int(rotation)
            )
            matches = None if rotation == "auto" or observed["rotation"] is None else observed["rotation"] == int(rotation)
            if setting_applied and (rotation == "auto" or matches):
                return {**observed, "action": action, "requested_rotation": rotation,
                        "setting_applied": True, "rotation_matches": matches}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
        if time.monotonic() >= deadline:
            break
    if action == "rotate-screen" and setting_applied:
        return {**observed, "action": action, "requested_rotation": rotation,
                "setting_applied": True, "rotation_matches": matches}
    field = "interactive" if action != "rotate-screen" else "rotation_mode"
    raise ControlError(f"{action} was sent but not verified ({field}={observed[field]})")
