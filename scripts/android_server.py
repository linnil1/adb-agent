"""MCP tools, dashboard routes, and viewer state for Android control."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

if __package__:
    from .android_core import (
        PNG_MAGIC, SERVER_API_VERSION, SERVER_NAME, Adb, ControlError,
        action_args, advertised_server_url, clear_server_file,
        description_elements, execute, resolve_adb, resolve_serial,
        write_server_file,
    )
else:
    from android_core import (
        PNG_MAGIC, SERVER_API_VERSION, SERVER_NAME, Adb, ControlError,
        action_args, advertised_server_url, clear_server_file,
        description_elements, execute, resolve_adb, resolve_serial,
        write_server_file,
    )


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
    screenshot_revision = 0
    latest_screenshots: dict[str, dict[str, object]] = {}
    latest_descriptions: dict[str, dict[str, object]] = {}
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
            recorded_values = dict(values)
            if isinstance(result, dict) and isinstance(result.get("target"), str):
                recorded_values["target"] = result["target"]
            record(action, recorded_values, True)
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
        nonlocal screenshot_revision
        with viewer_lock:
            screenshot_revision += 1
            revision = screenshot_revision
            latest_descriptions.pop(target, None)
            latest_screenshots[target] = {
                "revision": revision,
                "target": target,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "png": png,
            }
        publish("screenshot", revision=revision, target=target)
        return revision

    def cache_description(target: str, elements: list[dict[str, object]]) -> None:
        description = {
            "target": target,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "elements": elements,
        }
        with viewer_lock:
            latest_descriptions[target] = description
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
            record("screenshot", {"target": selected}, True)
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
        requested_target = request.query_params.get("target")
        requested_revision = request.query_params.get("revision")
        with viewer_lock:
            screenshot = latest_screenshots.get(requested_target) if requested_target else None
            if screenshot is None and requested_target is None and requested_revision is not None:
                screenshot = next(
                    (
                        item for item in latest_screenshots.values()
                        if str(item.get("revision")) == requested_revision
                    ),
                    None,
                )
            if (
                screenshot is None
                and requested_target is None
                and requested_revision is None
                and latest_screenshots
            ):
                screenshot = max(
                    latest_screenshots.values(), key=lambda item: int(item["revision"])
                )
            png = screenshot.get("png") if screenshot else None
            revision = screenshot.get("revision") if screenshot else None
        if not isinstance(png, bytes):
            return JSONResponse(
                {"ok": False, "error": "no screenshot has been captured"},
                status_code=404,
            )
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
                    "screenshots": [
                        {key: value for key, value in screenshot.items() if key != "png"}
                        for screenshot in latest_screenshots.values()
                    ],
                    "descriptions": list(latest_descriptions.values()),
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
                    requested_target = arguments.get("target")
                    screenshot = (
                        latest_screenshots.get(requested_target)
                        if isinstance(requested_target, str)
                        else None
                    )
                    if screenshot is None:
                        screenshot = max(
                            latest_screenshots.values(), key=lambda item: int(item["revision"])
                        )
                    response["viewer"] = {
                        key: value for key, value in screenshot.items() if key != "png"
                    }
            return JSONResponse(response)
        except ToolError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    write_server_file(advertised_server_url(host, port))
    try:
        server.run(transport="streamable-http", host=host, port=port)
    finally:
        clear_server_file()
