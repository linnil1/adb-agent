#!/usr/bin/env python3
"""Dependency-free ADB automation CLI inspired by androir-mcp."""

from __future__ import annotations

import sys

if __package__:
    from .android_core import *
    from .android_core import _package_cache
    from .android_client import (
        discover_server, execute_via_server, read_json_response,
        tool_name_for_action, urlrequest, verify_server,
    )
    from .android_server import run_mcp
else:
    from android_core import *
    from android_core import _package_cache
    from android_client import (
        discover_server, execute_via_server, read_json_response,
        tool_name_for_action, urlrequest, verify_server,
    )
    from android_server import run_mcp


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
