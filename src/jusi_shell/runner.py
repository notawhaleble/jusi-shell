from __future__ import annotations

import json
import os
import pty
import selectors
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from jusi.infrastructure.debug_timing import emit_timing
from jusi.infrastructure.plugin_runtime import set_plugin_control_handler


_INTERACTIVE_SHELL_NAMES = {"bash", "zsh", "fish", "sh", "dash", "ksh"}


class ShellRuntime:
    def __init__(self, *, argv: list[str], initial_text: str) -> None:
        self.argv = argv
        self.initial_text = initial_text
        self.master_fd = -1
        self._child_pid = 0
        self._returncode: int | None = None
        self._stop = threading.Event()
        self._stdin_thread: threading.Thread | None = None

    def start(self) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGQUIT, signal.SIG_DFL)
            env = os.environ.copy()
            try:
                os.execvpe(self.argv[0], self.argv, env)
            except Exception as exc:
                os.write(2, (str(exc) + "\n").encode("utf-8", "replace"))
                os._exit(127)
        self.master_fd = master_fd
        self._child_pid = pid
        self._stdin_thread = threading.Thread(target=self._pump_stdin, daemon=True)
        self._stdin_thread.start()
        if self.initial_text.strip():
            self.send_text(self.initial_text)

    def _pump_stdin(self) -> None:
        input_fd = sys.stdin.fileno()
        while not self._stop.is_set() and self.master_fd >= 0:
            try:
                chunk = os.read(input_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            try:
                os.write(self.master_fd, chunk)
            except OSError:
                return

    def _poll_child(self) -> int | None:
        if self._child_pid <= 0 or self._returncode is not None:
            return self._returncode
        try:
            pid, status = os.waitpid(self._child_pid, os.WNOHANG)
        except ChildProcessError:
            self._returncode = self._returncode if self._returncode is not None else 0
            return self._returncode
        if pid == 0:
            return None
        if os.WIFEXITED(status):
            self._returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self._returncode = 128 + os.WTERMSIG(status)
        else:
            self._returncode = 1
        return self._returncode

    def send_text(self, text: str) -> None:
        if self.master_fd < 0:
            return
        data = text.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        try:
            os.write(self.master_fd, data)
        except OSError:
            return

    def interrupt(self) -> None:
        if self._child_pid <= 0 or self._poll_child() is not None:
            return
        try:
            os.killpg(self._child_pid, signal.SIGINT)
        except ProcessLookupError:
            return

    def stop(self) -> None:
        self._stop.set()
        if self._child_pid > 0 and self._poll_child() is None:
            try:
                os.killpg(self._child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        if self._stdin_thread is not None and self._stdin_thread.is_alive():
            self._stdin_thread.join(timeout=0.2)
        self._poll_child()

    def wait_forever(self) -> int:
        if self._child_pid <= 0 or self.master_fd < 0:
            return 1
        selector = selectors.DefaultSelector()
        selector.register(self.master_fd, selectors.EVENT_READ)
        output = sys.stdout.buffer
        try:
            while self._poll_child() is None:
                for _key, _mask in selector.select(timeout=0.1):
                    try:
                        chunk = os.read(self.master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        continue
                    output.write(chunk)
                    output.flush()
            while True:
                try:
                    chunk = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
        finally:
            selector.close()
            self.stop()
        return int(self._returncode or 0)


def _load_payload() -> dict[str, Any]:
    raw = os.environ.get("JUSI_SHELL_PAYLOAD_JSON", "").strip()
    if not raw:
        raise RuntimeError("missing JUSI_SHELL_PAYLOAD_JSON")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid JUSI_SHELL_PAYLOAD_JSON")
    return payload


def _resolve_shell(shell_name: str) -> list[str]:
    requested = shell_name.strip()
    if requested:
        resolved = shutil.which(requested)
        if resolved is None:
            raise RuntimeError(f"requested shell is not available: {requested}")
        argv = [resolved]
    else:
        default_shell = str(os.environ.get("SHELL", "")).strip() or "/bin/sh"
        argv = [default_shell]
    shell_base = os.path.basename(argv[0])
    if shell_base in _INTERACTIVE_SHELL_NAMES:
        argv.append("-i")
    return argv


def _complete_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    current_word = str(payload.get("current_word", "")).strip()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    if "/" in current_word:
        base = Path(current_word).expanduser()
        parent = base.parent if str(base.parent) not in {"", "."} else Path(".")
        prefix = base.name
        try:
            for entry in parent.iterdir():
                if prefix and not entry.name.startswith(prefix):
                    continue
                value = str(parent / entry.name)
                if value in seen:
                    continue
                seen.add(value)
                items.append(
                    {
                        "value": value + ("/" if entry.is_dir() else ""),
                        "label": entry.name,
                        "kind": "dir" if entry.is_dir() else "file",
                        "detail": str(parent),
                        "documentation": None,
                    }
                )
        except OSError:
            return []
        return items

    for raw_path in str(os.environ.get("PATH", "")).split(os.pathsep):
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            for entry in path.iterdir():
                if current_word and not entry.name.startswith(current_word):
                    continue
                if not entry.is_file() or not os.access(entry, os.X_OK):
                    continue
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                items.append(
                    {
                        "value": entry.name,
                        "label": entry.name,
                        "kind": "command",
                        "detail": str(path),
                        "documentation": None,
                    }
                )
        except OSError:
            continue
    return items


def run_shell_runner() -> int:
    runtime: ShellRuntime | None = None
    previous_sigquit = signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    try:
        payload = _load_payload()
        content = str(payload.get("content", ""))
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise RuntimeError("invalid shell payload meta")
        shell_name = str(meta.get("shell", "")).strip()
        argv = _resolve_shell(shell_name)
        emit_timing("shell.runner.start", shell=shell_name or argv[0])
        runtime = ShellRuntime(argv=argv, initial_text=content)
        runtime.start()

        def _handle_control(request: dict[str, Any]) -> dict[str, Any]:
            message_type = str(request.get("message_type", "")).strip()
            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            emit_timing("shell.control.request", message_type=message_type)
            if message_type == "followup":
                runtime.send_text(str(payload.get("cell_text", "")))
                return {}
            if message_type == "complete":
                return {"items": _complete_payload(dict(payload))}
            raise RuntimeError(f"unsupported shell runtime request: {message_type}")

        set_plugin_control_handler(_handle_control)
        return runtime.wait_forever()
    except Exception as exc:
        emit_timing("shell.runner.error", error_type=type(exc).__name__, error_message=str(exc))
        sys.stderr.write(str(exc) + "\n")
        sys.stderr.flush()
        return 2
    finally:
        signal.signal(signal.SIGQUIT, previous_sigquit)
        if runtime is not None:
            runtime.stop()
