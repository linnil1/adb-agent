"""ADB host connection actions, independent of device/default-target selection."""
from __future__ import annotations

import ipaddress
import re
import time

if __package__:
    from .android_core import Adb, ControlError, list_targets, validate_serial
else:
    from android_core import Adb, ControlError, list_targets, validate_serial


def validate_address(address: str) -> str:
    error = "address must be HOST:PORT or [IPv6]:PORT with port 1-65535"
    if address.startswith("["):
        match = re.fullmatch(r"\[([0-9a-fA-F:.]+)\]:([0-9]{1,5})", address)
        if not match:
            raise ControlError(error)
        try:
            host = "[" + str(ipaddress.IPv6Address(match[1])) + "]"
        except ValueError:
            raise ControlError(error) from None
        port = int(match[2])
    else:
        match = re.fullmatch(r"([A-Za-z0-9.-]{1,253}):([0-9]{1,5})", address)
        if not match:
            raise ControlError(error)
        host, port = match[1].lower(), int(match[2])
        if re.fullmatch(r"[0-9.]+", host):
            try:
                host = str(ipaddress.IPv4Address(host))
            except ValueError:
                raise ControlError(error) from None
        elif any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                 for label in host.rstrip(".").split(".")):
            raise ControlError(error)
    if not 1 <= port <= 65535:
        raise ControlError(error)
    return f"{host}:{port}"


def connection_action(adb: Adb, action: str, address: str,
                      pair_code: str | None = None) -> dict[str, object]:
    if action == "pair":
        # stdin keeps the code out of subprocess arguments; never return ADB's
        # pairing transcript, which some versions echo on error.
        try:
            output = adb.run_input(["pair", address], (pair_code + "\n").encode("ascii"))
        except ControlError:
            raise ControlError("ADB pairing failed; check the pairing address/code and phone prompt") from None
        if "successfully paired to " not in output.decode("utf-8", "replace").lower():
            raise ControlError("ADB pairing failed; check the pairing address/code and phone prompt")
        return {"action": action, "address": address, "paired": True}
    if action == "disconnect":
        already_disconnected = False
        try:
            adb.run(["disconnect", address])
        except ControlError as error:
            if "no such device" not in str(error).lower():
                raise
            already_disconnected = True
        if any(device["target"] == address for device in list_targets(adb)):
            raise ControlError("disconnect was sent but the endpoint is still listed")
        return {"action": action, "address": address, "disconnected": True,
                "already_disconnected": already_disconnected}
    if action != "connect":
        raise ControlError("unsupported connection action")
    output = adb.run(["connect", address]).decode("utf-8", "replace").strip()
    match = re.search(r"(?:^|\n)(?:already )?connected to ([^\s]+)", output)
    if not match:
        raise ControlError("ADB connection failed: " + output[:200])
    target = match[1]
    validate_serial(target)
    deadline = time.monotonic() + min(5, adb.timeout)
    state = "missing"
    while True:
        state = next((device["state"] for device in list_targets(adb) if device["target"] == target), "missing")
        if state == "device":
            return {"action": action, "address": address, "target": target, "state": state, "ready": True}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError(f"ADB connected but the endpoint is not ready (state={state})")
        time.sleep(min(0.2, remaining))
