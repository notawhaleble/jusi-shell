from __future__ import annotations

import argparse
import json
import os
import shlex
from typing import Any

from IPython.core.error import UsageError

from jusi.domain.models import JUSI_HANDLER_HANDOFF_MIME
from jusi.infrastructure.runtime import JUSI_SESSION_CONFIG_ENV

from .config import default_shell_from_config, shell_config_from_session


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="%%shell", add_help=False)
    parser.add_argument("shell_name", nargs="?", default="")
    return parser


def load_ipython_extension(ipython: Any) -> None:
    cell_magics = getattr(getattr(ipython, "magics_manager", None), "magics", {}).get("cell", {})
    if "shell" in cell_magics:
        return

    def _jusi_shell_magic(line: str, cell: str) -> None:
        from IPython.display import display

        args = _parser().parse_args(shlex.split(line))
        session_config = _session_config_from_env()
        shell_name = str(args.shell_name or "").strip()
        if not shell_name:
            shell_name = default_shell_from_config(shell_config_from_session(session_config))
        meta = {
            "shell": shell_name,
            "line": line,
        }
        payload = {
            "handler_id": "shell",
            "magic_name": "shell",
            "content": str(cell or ""),
            "meta": meta,
        }
        display(
            {JUSI_HANDLER_HANDOFF_MIME: payload},
            raw=True,
            metadata={JUSI_HANDLER_HANDOFF_MIME: meta},
        )

    ipython.register_magic_function(_jusi_shell_magic, magic_kind="cell", magic_name="shell")


def _session_config_from_env() -> dict[str, object]:
    raw = os.environ.get(JUSI_SESSION_CONFIG_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}
