from __future__ import annotations

import json
import socket
from typing import Any


def exchange(socket_path: str, request: dict[str, Any], *, timeout: float = 3.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(socket_path)
        stream = connection.makefile("rwb")
        with stream:
            stream.write(json.dumps(request).encode("utf-8") + b"\n")
            stream.flush()
            raw = stream.readline()
    if not raw:
        raise ConnectionError("shell application closed the control connection")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("invalid shell application response")
    return result
