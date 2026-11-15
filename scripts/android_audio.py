"""Audio state inspection and verified stream/ringer controls over ADB."""
from __future__ import annotations

import re
import time

if __package__:
    from .android_core import AUDIO_STREAMS, Adb, ControlError, shell
    from .android_dnd import supports_dnd
else:
    from android_core import AUDIO_STREAMS, Adb, ControlError, shell
    from android_dnd import supports_dnd


STREAM_LABELS = {"media": "MUSIC", "ring": "RING", "notification": "NOTIFICATION",
                 "alarm": "ALARM", "voice-call": "VOICE_CALL"}
CONTROL_COMMANDS = ("set-volume", "adj-volume", "adj-mute", "adj-unmute", "set-ringer-mode")


def parse_audio_dump(output: str) -> dict[str, object]:
    streams = {}
    for name, label in STREAM_LABELS.items():
        # Only match stream state sections, never volume-group or event-log entries.
        block = re.search(r"^- STREAM_" + label + r"(?: \([^\n]*\))?:\s*\n"
                          r"((?:[ \t]+[^\n]*\n?)+)", output, re.M)
        text = block[1] if block else ""

        def boolean(field: str) -> bool | None:
            match = re.search(r"^\s*" + field + r": (true|false)\b", text, re.M)
            return match[1] == "true" if match else None
        device = re.search(r"^\s*Devices: ([^\n]+)", text, re.M)
        streams[name] = {"id": AUDIO_STREAMS[name], "muted": boolean("Muted"),
                         "muted_internally": boolean("Muted Internally"),
                         "device": device[1].strip() if device else None}
    internal = re.search(r"^- mode \(internal\) = (NORMAL|VIBRATE|SILENT)\b", output, re.M)
    external = re.search(r"^- mode \(external\) = (NORMAL|VIBRATE|SILENT)\b", output, re.M)
    zen = re.search(r"^- zen mode:(\w+)", output, re.M)
    mic = re.search(r"^\s*mic mute .*?from system=(true|false)\b", output, re.M)
    return {"streams": streams, "ringer_mode": internal[1].lower() if internal else None,
            "ringer_mode_external": external[1].lower() if external else None,
            "zen_mode": zen[1] if zen else None,
            "microphone_muted": mic[1] == "true" if mic else None}


def supported_commands(adb: Adb, serial: str) -> list[str]:
    # Some Android builds return no output for `cmd audio help`, and print help
    # with a nonzero exit status when no subcommand is given.
    output = shell(adb, serial, ["cmd audio 2>&1 || true"]).decode("utf-8", "replace")
    return [name for name in CONTROL_COMMANDS
            if re.search(r"^\s*" + name + r"(?:\s|$)", output, re.M)]


def read_volume(adb: Adb, serial: str, stream: str, *, timeout: float | None = None) -> dict[str, int]:
    output = shell(adb, serial, ["cmd", "media_session", "volume", "--stream",
                               str(AUDIO_STREAMS[stream]), "--get"], timeout=timeout).decode("utf-8", "replace")
    match = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", output)
    if not match:
        raise ControlError(f"cannot read {stream} volume (unsupported or malformed response)")
    level, minimum, maximum = map(int, match.groups())
    # Android reports effective volume zero for a muted stream even when its
    # minimum settable index is positive (for example, alarm or voice-call).
    if not (0 <= minimum <= maximum and (minimum <= level <= maximum or level == 0)):
        raise ControlError(f"invalid {stream} volume range")
    return {"level": level, "min": minimum, "max": maximum}


def get_volume(adb: Adb, serial: str, stream: str | None = None) -> dict[str, object]:
    if stream is not None and stream not in AUDIO_STREAMS:
        raise ControlError("unsupported audio stream")
    errors = []
    try:
        output = shell(adb, serial, ["dumpsys", "audio"]).decode("utf-8", "replace")
    except ControlError as error:
        output = ""
        errors.append(f"audio state: {error}")
    state = parse_audio_dump(output)
    state["streams"] = {name: values for name, values in state["streams"].items()
                        if stream is None or stream == name}
    volumes_read = 0
    for name, values in state["streams"].items():
        values.update(level=None, min=None, max=None)
        try:
            values.update(read_volume(adb, serial, name))
            volumes_read += 1
        except ControlError as error:
            errors.append(str(error))
    try:
        commands = supported_commands(adb, serial)
    except ControlError as error:
        commands = []
        errors.append(f"audio commands: {error}")
    try:
        if supports_dnd(adb, serial):
            commands.append("set_dnd")
    except ControlError as error:
        errors.append(f"DND command support: {error}")
    if not volumes_read and state["ringer_mode"] is None:
        raise ControlError("cannot read audio state: " + ("; ".join(errors) or "unrecognized response"))
    return {"target": serial, **state, "supported_commands": commands, "errors": errors}


def _control_audio(adb: Adb, serial: str, action: str, *, stream: str | None = None,
                   level: int | None = None, direction: str | None = None,
                   muted: bool | None = None, ringer_mode: str | None = None,
                   commands: list[str] | None = None) -> dict[str, object]:
    if action == "set-ringer-mode":
        if ringer_mode not in {"normal", "vibrate", "silent"}:
            raise ControlError("ringer mode must be normal, vibrate, or silent")
        command = ["set-ringer-mode", ringer_mode.upper()]
        before = None
        requested = {"requested_ringer_mode": ringer_mode}
    else:
        if stream not in AUDIO_STREAMS:
            raise ControlError("an explicit supported audio stream is required")
        before = read_volume(adb, serial, stream)
        stream_id = str(AUDIO_STREAMS[stream])
        if action == "set-volume":
            if type(level) is not int or not before["min"] <= level <= before["max"]:
                raise ControlError(f"{stream} volume must be between {before['min']} and {before['max']}")
            command = ["set-volume", stream_id, str(level)]
            requested = {"requested_level": level}
        elif action == "adjust-volume":
            if direction not in {"raise", "lower"}:
                raise ControlError("volume direction must be raise or lower")
            command = ["adj-volume", stream_id, direction.upper()]
            requested = {"requested_direction": direction}
        elif action == "set-mute":
            if type(muted) is not bool:
                raise ControlError("muted must be true or false")
            command = ["adj-mute" if muted else "adj-unmute", stream_id]
            requested = {"requested_muted": muted}
        else:
            raise ControlError("unsupported audio action")
    if command[0] not in (commands if commands is not None else supported_commands(adb, serial)):
        raise ControlError(f"this device does not advertise cmd audio {command[0]}")
    # Never fall back to media_session writes: on some phones they report
    # success without changing volume. Send each mutation exactly once.
    response = shell(adb, serial, ["cmd", "audio", *command]).decode("utf-8", "replace").strip()
    if re.search(r"(?:^|\n)\s*(?:error|unknown command|exception|usage)\b", response, re.I):
        raise ControlError("audio command returned: " + response[:200])
    deadline = time.monotonic() + min(5.0, adb.timeout)
    observed = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            remaining = max(0.001, min(adb.timeout, deadline - time.monotonic()))
            if action in {"set-mute", "set-ringer-mode"}:
                output = shell(adb, serial, ["dumpsys", "audio"], timeout=remaining).decode("utf-8", "replace")
                observed = parse_audio_dump(output)
                confirmed = (observed["ringer_mode"] == ringer_mode if action == "set-ringer-mode"
                             else observed["streams"][stream]["muted"] is muted)
            else:
                observed = read_volume(adb, serial, stream, timeout=remaining)
                actual = observed["level"]
                if action == "set-volume":
                    confirmed = actual == level
                elif direction == "raise":
                    confirmed = actual > before["level"] or actual == before["level"] == before["max"]
                else:
                    confirmed = actual < before["level"] or (actual == before["level"] and actual <= before["min"])
            if time.monotonic() <= deadline and confirmed:
                result = {"target": serial, "action": action, **requested, "verified": True}
                if action == "set-ringer-mode":
                    return {**result, "ringer_mode": observed["ringer_mode"],
                            "ringer_mode_external": observed["ringer_mode_external"]}
                if action == "set-mute":
                    return {**result, "stream": stream, **observed["streams"][stream]}
                return {**result, "stream": stream, "before": before, **observed}
        except ControlError as error:
            last_error = str(error)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.2, remaining))
    detail = last_error or str(observed)
    raise ControlError(f"{action} was sent but not verified: {detail}; read get-volume before retrying")


def validate_volume_request(stream: str, level: int | None, direction: str | None,
                            muted: bool | None, ringer_mode: str | None) -> None:
    if stream not in AUDIO_STREAMS:
        raise ControlError("unsupported audio stream")
    if all(value is None for value in (level, direction, muted, ringer_mode)):
        raise ControlError("set-volume requires level, muted, direction, or ringer-mode")
    if level is not None and (type(level) is not int or level < 0):
        raise ControlError("level must be a non-negative integer")
    if level is not None and direction is not None:
        raise ControlError("level and direction cannot be combined")
    if direction is not None and direction not in {"raise", "lower"}:
        raise ControlError("direction must be raise or lower")
    if muted is not None and type(muted) is not bool:
        raise ControlError("muted must be true or false")
    if ringer_mode is not None and ringer_mode not in {"normal", "vibrate", "silent"}:
        raise ControlError("ringer mode must be normal, vibrate, or silent")


def set_volume(adb: Adb, serial: str, *, stream: str = "media", level: int | None = None,
               direction: str | None = None, muted: bool | None = None,
               ringer_mode: str | None = None) -> dict[str, object]:
    validate_volume_request(stream, level, direction, muted, ringer_mode)
    stream_change = any(value is not None for value in (level, direction, muted))
    status_stream = None if ringer_mode is not None else stream
    before = get_volume(adb, serial, status_stream)
    commands = before["supported_commands"]
    requested = {name: value for name, value in {
        "level": level, "muted": muted, "direction": direction, "ringer_mode": ringer_mode
    }.items() if value is not None}
    if stream_change:
        requested = {"stream": stream, **requested}
    steps = []
    if ringer_mode is not None:
        if before["ringer_mode"] is None:
            raise ControlError("cannot read ringer mode; no change was sent")
        steps.append(("ringer_mode", "set-ringer-mode", {"ringer_mode": ringer_mode}, "set-ringer-mode"))
    if stream_change:
        state = before["streams"][stream]
        if any(state[key] is None for key in ("level", "min", "max")):
            raise ControlError(f"cannot read {stream} volume range; no change was sent")
        if level is not None:
            if not state["min"] <= level <= state["max"]:
                raise ControlError(f"{stream} volume must be between {state['min']} and {state['max']}")
            steps.append(("level", "set-volume", {"stream": stream, "level": level}, "set-volume"))
        if direction is not None:
            steps.append(("direction", "adjust-volume", {"stream": stream, "direction": direction}, "adj-volume"))
        if muted is not None:
            if state["muted"] is None:
                raise ControlError(f"cannot read {stream} mute state; no change was sent")
            steps.append(("muted", "set-mute", {"stream": stream, "muted": muted},
                          "adj-mute" if muted else "adj-unmute"))
    missing = [command for _, _, _, command in steps if command not in commands]
    if missing:
        raise ControlError("this device does not advertise cmd audio " + ", ".join(missing) + "; no change was sent")
    applied = []
    expected_level = None
    for name, action, values, _ in steps:
        try:
            result = _control_audio(adb, serial, action, commands=commands, **values)
        except ControlError as error:
            done = ", ".join(applied) or "none"
            raise ControlError(f"set-volume failed at {name}; verified steps: {done}; {error}") from None
        applied.append(name)
        if name in {"level", "direction"}:
            expected_level = result["level"]
    try:
        observed = get_volume(adb, serial, status_stream)
    except ControlError as error:
        raise ControlError(f"set-volume changes were sent but final status could not be read: {error}") from None
    mismatches = []
    if ringer_mode is not None and observed["ringer_mode"] != ringer_mode:
        mismatches.append("ringer_mode")
    if stream_change:
        state = observed["streams"][stream]
        if muted is not None and state["muted"] is not muted:
            mismatches.append("muted")
        # The level was read back before the explicit mute step. A muted stream
        # reports effective zero, while Android retains the verified index.
        if expected_level is not None and not (muted is True and state["muted"] is True):
            if state["level"] != expected_level:
                mismatches.append("level")
    if mismatches:
        raise ControlError("set-volume changes were sent but final state was not verified for "
                           + ", ".join(mismatches) + "; read get-volume before retrying")
    return {"target": serial, "requested": requested, "verified": True,
            **{key: value for key, value in observed.items() if key != "target"}}
