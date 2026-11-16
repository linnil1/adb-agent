"""Wait for an exact selector in valid accessibility snapshots."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

if __package__:
    from .android_core import Adb, ControlError, dump_ui, parse_ui_xml
else:
    from android_core import Adb, ControlError, dump_ui, parse_ui_xml


def wait_for_ui(adb: Adb, serial: str, *, text: str | None = None,
                description: str | None = None, resource_id: str | None = None,
                state: str = "present", wait_timeout: float = 30.0) -> dict[str, object]:
    selectors = {name: value for name, value in {
        "text": text, "description": description, "resource_id": resource_id
    }.items() if value is not None}
    started = time.monotonic()
    deadline = started + wait_timeout
    attempts = successful_captures = 0
    last_error = None
    deadline_reached = False
    while time.monotonic() < deadline:
        attempts += 1
        try:
            xml = dump_ui(adb, serial, deadline=deadline)
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                raise ControlError("UI capture returned malformed XML") from None
            if root.tag != "hierarchy":
                raise ControlError("UI capture returned no hierarchy root")
            if time.monotonic() >= deadline:
                deadline_reached = True
                break
            successful_captures += 1
            last_error = None
            elements = [
                element for element in parse_ui_xml(xml, visible_only=True)
                if all(element.get(name) == value for name, value in selectors.items())
            ]
            matched = elements if state != "enabled" else [
                element for element in elements if "enabled" in element["states"]
            ]
            if (state == "gone" and not elements) or (state != "gone" and matched):
                return {"target": serial, "action": "wait-for-ui", "state": state,
                        "selectors": selectors, "matched": True, "attempts": attempts,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "elements": [] if state == "gone" else matched}
        except ControlError as error:
            if time.monotonic() >= deadline and (
                str(error) == "UI capture deadline reached" or str(error).startswith("adb timed out after ")
            ):
                deadline_reached = True
            else:
                last_error = str(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    if not successful_captures and (last_error or not deadline_reached):
        raise ControlError("could not inspect UI while waiting: " + (last_error or "capture exceeded wait timeout"))
    detail = f"; last capture failed: {last_error}" if last_error else ""
    raise ControlError(f"wait-for-ui timed out after {wait_timeout:g}s waiting for {state}{detail}")
