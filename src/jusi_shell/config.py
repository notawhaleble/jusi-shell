from __future__ import annotations

from typing import Any, Mapping


def shell_config_from_session(session_config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(session_config or {}).get("shell", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def default_shell_from_config(shell_config: Mapping[str, Any] | None) -> str:
    raw_config = dict(shell_config or {})
    return str(raw_config.get("default", raw_config.get("default_shell", ""))).strip()
