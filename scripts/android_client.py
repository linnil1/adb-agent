"""Verified local-server discovery and HTTP client for Android control."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode, urlsplit

if __package__:
    from .android_core import (
        ACTION_FIELDS, PNG_MAGIC, SERVER_API_VERSION, SERVER_NAME, ControlError,
        clear_server_file, prepare_action, server_file,
    )
else:
    from android_core import (
        ACTION_FIELDS, PNG_MAGIC, SERVER_API_VERSION, SERVER_NAME, ControlError,
        clear_server_file, prepare_action, server_file,
    )


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
        if not isinstance(target, str) or not isinstance(revision, int):
            raise ControlError("local MCP server omitted the screenshot target or revision")
        screenshot_url = server_url + "/api/viewer/screenshot?" + urlencode({
            "target": target, "revision": revision,
        })
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
