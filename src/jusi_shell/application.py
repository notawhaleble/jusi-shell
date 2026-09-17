from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import selectors
import shlex
import shutil
import signal
import socket
import struct
import sys
import tempfile
import termios
import threading
from typing import Any


_INTERACTIVE_SHELLS = {"bash", "dash", "fish", "ksh", "sh", "zsh"}


def _winsize(fd: int) -> bytes | None:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None


def _set_winsize(fd: int, value: bytes | None) -> None:
    if value is not None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, value)


class ShellApplication:
    def __init__(self, launch: dict[str, Any], socket_path: str) -> None:
        self.body = str(launch.get("body", ""))
        self.requested_shell = str(launch.get("shell", "")).strip()
        self.socket_path = socket_path
        self.current_cwd = os.getcwd()
        self.master_fd = -1
        self.child_pid = 0
        self.returncode: int | None = None
        self.stop_event = threading.Event()
        self.terminate_requested = threading.Event()
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.server: socket.socket | None = None
        self.server_thread: threading.Thread | None = None
        self.state_directory = tempfile.TemporaryDirectory(prefix="jusi-shell-app-")
        self.saved_terminal: list[Any] | None = None

    def run(self) -> int:
        self._start_control_server()
        shell_path, argv, env = self._shell_command()
        pid, master_fd = pty.fork()
        if pid == 0:
            for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
                signal.signal(signum, signal.SIG_DFL)
            try:
                os.execvpe(shell_path, argv, env)
            except BaseException as exc:
                os.write(2, f"jusi-shell: {exc}\n".encode("utf-8", "replace"))
                os._exit(127)

        self.child_pid, self.master_fd = pid, master_fd
        _set_winsize(master_fd, _winsize(sys.stdin.fileno()))
        self._install_signal_handlers()
        self._set_raw_terminal()
        if self.body.strip():
            self._send(self.body)
        try:
            return self._pump_terminal()
        finally:
            self.close()

    def _shell_command(self) -> tuple[str, list[str], dict[str, str]]:
        requested = self.requested_shell or os.environ.get("SHELL", "").strip() or "/bin/sh"
        shell_path = shutil.which(requested) if not os.path.isabs(requested) else requested
        if not shell_path:
            raise RuntimeError(f"requested shell is not available: {requested}")
        name = os.path.basename(shell_path)
        env = os.environ.copy()
        argv = [shell_path]
        if name == "bash":
            argv += ["--rcfile", self._write_bashrc(), "-i"]
        elif name == "zsh":
            env["ZDOTDIR"] = self.state_directory.name
            self._write_zshrc()
            argv.append("-i")
        elif name == "fish":
            argv += ["-i", "-C", self._fish_init()]
        elif name in _INTERACTIVE_SHELLS:
            argv.append("-i")
        return shell_path, argv, env

    def _client_command(self) -> str:
        code = (
            "import json,os,socket;"
            f"s=socket.socket(socket.AF_UNIX);s.connect({self.socket_path!r});"
            "s.sendall((json.dumps({'type':'cwd','cwd':os.getcwd()})+'\\n').encode());s.close()"
        )
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} >/dev/null 2>&1"

    @staticmethod
    def _open_command() -> str:
        return f"{shlex.quote(sys.executable)} -m jusi.editor_client"

    def _write_bashrc(self) -> str:
        path = Path(self.state_directory.name) / "bashrc"
        path.write_text(
            "\n".join(
                [
                    'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi',
                    'jusi-open() { case "${1-}" in -t|--tab) shift;; esac; [ "$#" -ge 1 ] || return 2; '
                    + self._open_command()
                    + ' "$1"; }',
                    "__jusi_emit_cwd() { " + self._client_command() + "; }",
                    "case \"${PROMPT_COMMAND-}\" in *__jusi_emit_cwd*) ;; *) PROMPT_COMMAND=\"__jusi_emit_cwd${PROMPT_COMMAND:+;$PROMPT_COMMAND}\";; esac",
                    "__jusi_emit_cwd",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return str(path)

    def _write_zshrc(self) -> None:
        path = Path(self.state_directory.name) / ".zshrc"
        path.write_text(
            "\n".join(
                [
                    'if [ -f "$HOME/.zshrc" ]; then . "$HOME/.zshrc"; fi',
                    'jusi-open() { case "${1-}" in -t|--tab) shift;; esac; [ "$#" -ge 1 ] || return 2; '
                    + self._open_command()
                    + ' "$1"; }',
                    "autoload -Uz add-zsh-hook",
                    "__jusi_emit_cwd() { " + self._client_command() + "; }",
                    "add-zsh-hook precmd __jusi_emit_cwd",
                    "__jusi_emit_cwd",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _fish_init(self) -> str:
        return (
            "function jusi-open; "
            "if test (count $argv) -gt 0; and contains -- $argv[1] -t --tab; set -e argv[1]; end; "
            "test (count $argv) -ge 1; or return 2; "
            + self._open_command()
            + " $argv[1]; end; "
            "function __jusi_emit_cwd --on-event fish_prompt; "
            + self._client_command()
            + "; end; __jusi_emit_cwd"
        )

    def _start_control_server(self) -> None:
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen()
        server.settimeout(0.2)
        self.server = server

        def serve() -> None:
            while not self.stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    try:
                        raw = connection.makefile("rb").readline()
                        request = json.loads(raw)
                        response = self._handle_request(request)
                        connection.sendall(json.dumps(response).encode("utf-8") + b"\n")
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        continue

        self.server_thread = threading.Thread(target=serve, name="jusi-shell-control", daemon=True)
        self.server_thread.start()

    def _handle_request(self, request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "invalid request"}
        kind = request.get("type")
        if kind == "cwd":
            value = str(request.get("cwd", "")).strip()
            if value:
                with self.state_lock:
                    self.current_cwd = value
            return {"ok": True}
        if kind == "status":
            with self.state_lock:
                cwd = self.current_cwd
            return {"ok": True, "cwd": cwd}
        if kind == "followup":
            if self.returncode is not None:
                return {"ok": False, "error": "shell has exited"}
            self._send(str(request.get("body", "")))
            return {"ok": True}
        return {"ok": False, "error": f"unsupported request: {kind}"}

    def _send(self, text: str) -> None:
        data = text.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        with self.write_lock:
            if self.master_fd < 0:
                raise OSError("shell terminal is unavailable")
            os.write(self.master_fd, data)

    def _install_signal_handlers(self) -> None:
        def resize(*_: object) -> None:
            try:
                _set_winsize(self.master_fd, _winsize(sys.stdin.fileno()))
            except OSError:
                pass

        def forward(signum: int, _frame: object) -> None:
            try:
                os.killpg(self.child_pid, signum)
            except ProcessLookupError:
                pass

        def terminate(_signum: int, _frame: object) -> None:
            self.terminate_requested.set()
            self.stop_event.set()
            try:
                os.killpg(self.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGWINCH, resize)
        for signum in (signal.SIGINT, signal.SIGQUIT):
            signal.signal(signum, forward)
        signal.signal(signal.SIGTERM, terminate)

    def _set_raw_terminal(self) -> None:
        try:
            self.saved_terminal = termios.tcgetattr(sys.stdin.fileno())
            attrs = termios.tcgetattr(sys.stdin.fileno())
            attrs[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
            attrs[1] &= ~termios.OPOST
            attrs[2] |= termios.CS8
            attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
        except termios.error:
            self.saved_terminal = None

    def _pump_terminal(self) -> int:
        selector = selectors.DefaultSelector()
        selector.register(self.master_fd, selectors.EVENT_READ, "shell")
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "input")
        try:
            while not self.terminate_requested.is_set() and self._poll_child() is None:
                for key, _ in selector.select(timeout=0.1):
                    try:
                        data = os.read(key.fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        if key.data == "input":
                            selector.unregister(key.fd)
                        continue
                    if key.data == "input":
                        with self.write_lock:
                            os.write(self.master_fd, data)
                    else:
                        os.write(sys.stdout.fileno(), data)
            if not self.terminate_requested.is_set():
                while select_ready := selector.select(timeout=0.02):
                    shell_keys = [key for key, _ in select_ready if key.data == "shell"]
                    if not shell_keys:
                        break
                    try:
                        data = os.read(self.master_fd, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
        finally:
            selector.close()
        return int(self.returncode or 0)

    def _poll_child(self) -> int | None:
        if self.child_pid <= 0 or self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = 0
            return self.returncode
        if pid == 0:
            return None
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def close(self) -> None:
        self.stop_event.set()
        if self.server is not None:
            self.server.close()
        child_running = self.child_pid > 0 and self._poll_child() is None
        if child_running:
            try:
                os.killpg(self.child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        if self.saved_terminal is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self.saved_terminal)
            except termios.error:
                pass
        self.state_directory.cleanup()


def _read_launch(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(value, dict):
        raise ValueError("invalid shell launch payload")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jusi_shell.application")
    parser.add_argument("launch_path", type=Path)
    parser.add_argument("socket_path")
    args = parser.parse_args(argv)
    return ShellApplication(_read_launch(args.launch_path), args.socket_path).run()


if __name__ == "__main__":
    raise SystemExit(main())
