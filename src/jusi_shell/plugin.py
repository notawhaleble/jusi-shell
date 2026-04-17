from __future__ import annotations

import json
import os
import sys
from typing import Any

from jusi.domain.models import ExecutableCell
from jusi.plugins import BaseTerminalHandler, DisplayHandlerSpec, HandlerContext, MagicCommand


SUPPORTED_MAGIC_PREFIXES = ("%%shell",)


def _strip_shell_header(cell_text: str) -> str:
    lines = cell_text.splitlines()
    if lines and any(lines[0].lstrip().startswith(prefix) for prefix in SUPPORTED_MAGIC_PREFIXES):
        return "\n".join(lines[1:]).lstrip("\n")
    return cell_text


class ShellHandler(BaseTerminalHandler):
    def __init__(self) -> None:
        super().__init__()
        self._payload: dict[str, object] | None = None
        self._entry = ""

    def handler_id(self) -> str:
        return "shell"

    def handle(self, context: HandlerContext, cell: ExecutableCell) -> str:
        self.stop()
        self._mode = "ready"
        self._entry = cell.main_lines[0] if cell.main_lines else "%%shell"
        context.append_event(
            {
                "type": "execution_started",
                "cell_id": context.cell_id,
                "kind": cell.kind,
                "syntax": cell.syntax,
                "handler_id": self.handler_id(),
            }
        )
        context.emit_frontend_event(
            "handler_snapshot",
            {
                "handler_id": self.handler_id(),
                "mode": self._mode,
                "entry": self._entry,
                "family": "shell",
                "shell": str(context.meta.get("shell", "")).strip(),
            },
        )
        self._payload = {"content": context.content, "meta": dict(context.meta)}
        self.prepare_transport(context)
        context.set_status("follow-up")
        context.append_event(
            {
                "type": "execution_finished",
                "status": "follow-up",
                "handler_id": self.handler_id(),
            }
        )
        return "follow-up"

    def terminal_command(self) -> tuple[list[str], str]:
        self._mode = "live"
        return [sys.executable, "-m", "jusi", "plugin-runtime"], ""

    def terminal_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["TERM"] = os.environ.get("JUSI_SHELL_TERM", "").strip() or "xterm-256color"
        env["JUSI_PLUGIN_RUNTIME_CALLABLE"] = "jusi_shell.runner:run_shell_runner"
        env["JUSI_SHELL_PAYLOAD_JSON"] = json.dumps(self._payload or {"content": "", "meta": {}})
        return env

    def followup(self, context: HandlerContext, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized["cell_text"] = _strip_shell_header(str(normalized.get("cell_text", "")))
        context.call_backend_action(
            "plugin_runtime_request",
            {"message_type": "followup", "payload": normalized},
        )

    def complete(self, context: HandlerContext, payload: dict[str, Any]):
        response = context.call_backend_action(
            "plugin_runtime_request",
            {"message_type": "complete", "payload": dict(payload)},
        )
        items = response.get("items", ())
        if isinstance(items, list):
            return [dict(item) for item in items if isinstance(item, dict)]
        return ()

    def snapshot(self) -> dict[str, Any]:
        snapshot = {
            "handler_id": self.handler_id(),
            "mode": self._mode,
            "entry": self._entry,
            "family": "shell",
        }
        if self._payload is not None:
            snapshot["payload"] = dict(self._payload)
        return snapshot

    def stop(self) -> None:
        super().stop()
        self._payload = None


def display_handler_specs() -> tuple[DisplayHandlerSpec, ...]:
    return (
        DisplayHandlerSpec(
            handler_id="shell",
            factory=ShellHandler,
            magic_commands=(MagicCommand("shell"),),
            kernel_extension_modules=("jusi_shell.kernel",),
        ),
    )
