from __future__ import annotations

import argparse
import shlex
from typing import Any

from IPython.core.error import UsageError

from jusi.domain.models import JUSI_HANDLER_HANDOFF_MIME


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
        shell_name = str(args.shell_name or "").strip()
        payload = {
            "handler_id": "shell",
            "magic_name": "shell",
            "content": str(cell or ""),
            "meta": {
                "shell": shell_name,
                "line": line,
            },
        }
        display(
            {JUSI_HANDLER_HANDOFF_MIME: payload},
            raw=True,
            metadata={JUSI_HANDLER_HANDOFF_MIME: {"shell": shell_name, "line": line}},
        )

    ipython.register_magic_function(_jusi_shell_magic, magic_kind="cell", magic_name="shell")
