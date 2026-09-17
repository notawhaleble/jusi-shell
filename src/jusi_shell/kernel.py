from __future__ import annotations

import argparse
import os
import shlex
from typing import Any

from IPython.core.error import UsageError

from . import __version__


HANDOFF_MIME = "application/vnd.jusi.handoff.v1+json"
_runtime_configuration: dict[str, Any] = {}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="%%shell", add_help=False)
    parser.add_argument("-C", "--cwd", default="")
    parser.add_argument("shell_name", nargs="?", default="")
    return parser


def jusi_kernel_adapter_v1() -> dict[str, object]:
    return {
        "plugin_id": "jusi_shell",
        "plugin_version": __version__,
        "families": [{"family_id": "shell", "magic_name": "shell"}],
    }


def configure_jusi_runtime_v1(configuration: dict[str, Any]) -> None:
    global _runtime_configuration
    _runtime_configuration = dict(configuration)


def _default_shell() -> str:
    shell = _runtime_configuration.get("shell")
    if not isinstance(shell, dict):
        return ""
    return str(shell.get("default", shell.get("default_shell", ""))).strip()


def load_ipython_extension(ipython: Any) -> None:
    cell_magics = getattr(getattr(ipython, "magics_manager", None), "magics", {}).get("cell", {})
    if "shell" in cell_magics:
        return

    def _jusi_shell_magic(line: str, cell: str) -> None:
        from IPython.display import display

        args = _parser().parse_args(shlex.split(line))
        shell_name = str(args.shell_name or "").strip() or _default_shell()
        cwd = str(args.cwd or "").strip()
        if cwd:
            cwd = os.path.abspath(os.path.expanduser(cwd))
        payload = {
            "protocol_version": 1,
            "kind": "plugin.handoff",
            "plugin_id": "jusi_shell",
            "plugin_version": __version__,
            "family_id": "shell",
            "magic_name": "shell",
            "payload": {"shell": shell_name, "cwd": cwd, "body": str(cell or "")},
        }
        display({HANDOFF_MIME: payload}, raw=True)

    ipython.register_magic_function(_jusi_shell_magic, magic_kind="cell", magic_name="shell")
