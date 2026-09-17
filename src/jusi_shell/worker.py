from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

from jusi.plugin_api import OperationRejected, WorkerResult, copy_text, open_text, show_diff, terminal_surface

from .completion import complete_paths
from .control import exchange


def _followup_body(body: str) -> str:
    """Remove this plugin's cell-magic header from a frontend followup."""
    first_line, separator, remainder = body.partition("\n")
    header = first_line.strip()
    if header == "%%shell" or header.startswith(("%%shell ", "%%shell\t")):
        return remainder if separator else ""
    return body


class ShellWorker:
    def __init__(self, context: object) -> None:
        self.context = context
        self.runtime_directory: Path | None = None
        self.socket_path = ""
        self.cwd = ""

    def handle(self, operation: str, payload: dict[str, Any]) -> WorkerResult:
        if operation == "execute":
            return self._execute(payload)
        if operation == "followup":
            self._request({"type": "followup", "body": _followup_body(str(payload.get("body", "")))})
            return WorkerResult({"accepted": True})
        if operation == "complete":
            status = self._request({"type": "status"})
            cwd = str(status.get("cwd", "")).strip() or self.cwd
            return WorkerResult(complete_paths(payload, cwd))
        if operation == "editor_action":
            return self._editor_action(payload)
        raise OperationRejected(f"Unsupported shell operation: {operation}", reason="unsupported")

    def _execute(self, payload: dict[str, Any]) -> WorkerResult:
        if self.runtime_directory is not None:
            raise OperationRejected("Shell client is already initialized", reason="conflict")
        cwd = str(payload.get("cwd", "")).strip() or os.getcwd()
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise OperationRejected(f"Shell working directory does not exist: {cwd}", reason="invalid_request")

        shell_name = str(payload.get("shell", "")).strip()
        if shell_name and shutil.which(shell_name) is None:
            raise OperationRejected(f"Requested shell is not available: {shell_name}", reason="invalid_request")

        self.runtime_directory = Path(tempfile.mkdtemp(prefix="jusi-shell-"))
        self.runtime_directory.chmod(0o700)
        self.socket_path = str(self.runtime_directory / "control.sock")
        self.cwd = cwd
        payload_path = self.runtime_directory / "launch.json"
        fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"body": str(payload.get("body", "")), "shell": shell_name}, stream)
        except BaseException:
            self.close()
            raise

        return WorkerResult(
            {"accepted": True, "cwd": cwd},
            (
                terminal_surface(
                    "shell_terminal",
                    (sys.executable, "-m", "jusi_shell.application", str(payload_path), self.socket_path),
                    cwd=cwd,
                    environment_overrides={"TERM": os.environ.get("JUSI_SHELL_TERM", "").strip() or "xterm-256color"},
                    signal=True,
                ),
            ),
        )

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.runtime_directory is None:
            raise OperationRejected("Shell client is not initialized", reason="conflict")
        deadline = time.monotonic() + 3.0
        while True:
            try:
                result = exchange(self.socket_path, request)
                break
            except (ConnectionError, FileNotFoundError, OSError, ValueError) as exc:
                if time.monotonic() >= deadline:
                    raise OperationRejected("Shell application is not available", reason="conflict") from exc
                time.sleep(0.02)
        if not result.get("ok"):
            raise OperationRejected(str(result.get("error", "Shell request failed")))
        return result

    @staticmethod
    def _editor_action(payload: dict[str, Any]) -> WorkerResult:
        selection = payload.get("selection")
        if not isinstance(selection, dict):
            raise OperationRejected("Shell selection is missing", reason="invalid_request")
        action = str(payload.get("action", ""))
        if action == "show_diff":
            before, after = selection.get("before"), selection.get("after")
            if not isinstance(before, str) or not isinstance(after, str):
                raise OperationRejected("Shell diff selection requires before and after text", reason="invalid_request")
            return show_diff(before, after, filetype="sh")
        text = selection.get("text")
        if not isinstance(text, str):
            raise OperationRejected("Shell selection requires text", reason="invalid_request")
        if action == "copy":
            return copy_text(text, linewise=bool(selection.get("linewise", False)))
        if action == "open":
            return open_text(text, name="shell-selection.sh", filetype="sh")
        raise OperationRejected(f"Unsupported editor action: {action}", reason="unsupported")

    def close(self) -> None:
        if self.runtime_directory is not None:
            shutil.rmtree(self.runtime_directory, ignore_errors=True)
        self.runtime_directory = None
        self.socket_path = ""


def create_worker(context: object) -> ShellWorker:
    return ShellWorker(context)
