from __future__ import annotations

import fcntl
import json
import socket
import os
import pty
import selectors
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path
from typing import Any

from jusi.infrastructure.debug_timing import emit_timing
from jusi.infrastructure.plugin_runtime import set_plugin_control_handler


_INTERACTIVE_SHELL_NAMES = {"bash", "zsh", "fish", "sh", "dash", "ksh"}


def _debug_enabled() -> bool:
    return str(os.environ.get("JUSI_SHELL_DEBUG", "")).strip() == "1"


def _debug_log_path() -> str:
    return str(os.environ.get("JUSI_SHELL_DEBUG_FILE", "")).strip() or "/tmp/jusi-shell.log"


def _debug_log(event: str, **payload: object) -> None:
    if not _debug_enabled():
        return
    record = {"ts": round(time.time(), 6), "event": event}
    record.update(payload)
    try:
        with open(_debug_log_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError:
        return


def _safe_preview(data: bytes, limit: int = 80) -> str:
    return data[:limit].decode("utf-8", "replace")


def _read_winsize(fd: int) -> dict[str, int] | None:
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None
    rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
    return {"rows": rows, "cols": cols, "xpixels": xpix, "ypixels": ypix}


def _apply_winsize(fd: int, winsize: dict[str, int] | None) -> None:
    if not winsize:
        return
    rows = int(winsize.get("rows", 0))
    cols = int(winsize.get("cols", 0))
    xpixels = int(winsize.get("xpixels", 0))
    ypixels = int(winsize.get("ypixels", 0))
    if rows <= 0 or cols <= 0:
        return
    packed = struct.pack("HHHH", rows, cols, xpixels, ypixels)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


def _stdin_winsize() -> dict[str, int] | None:
    for fd in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
        winsize = _read_winsize(fd)
        if winsize and winsize["rows"] > 0 and winsize["cols"] > 0:
            return winsize
    return None


def _cc_value(cc: list[object], index: int) -> int | None:
    if index >= len(cc):
        return None
    value = cc[index]
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return value[0] if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tty_mode_flags(fd: int) -> dict[str, object] | None:
    try:
        attrs = termios.tcgetattr(fd)
    except termios.error:
        return None
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    return {
        "iflag": iflag,
        "oflag": oflag,
        "cflag": cflag,
        "lflag": lflag,
        "ispeed": ispeed,
        "ospeed": ospeed,
        "echo": bool(lflag & termios.ECHO),
        "icanon": bool(lflag & termios.ICANON),
        "isig": bool(lflag & termios.ISIG),
        "ixon": bool(iflag & termios.IXON),
        "icrnl": bool(iflag & termios.ICRNL),
        "opost": bool(oflag & termios.OPOST),
        "vintr": _cc_value(cc, termios.VINTR),
        "vquit": _cc_value(cc, termios.VQUIT),
        "verase": _cc_value(cc, termios.VERASE),
        "vkill": _cc_value(cc, termios.VKILL),
        "vmin": _cc_value(cc, termios.VMIN),
        "vtime": _cc_value(cc, termios.VTIME),
    }


def _replace_span(line_text: str, current_word: str) -> tuple[int | None, int | None]:
    if not current_word:
        return None, None
    cursor = len(line_text)
    start = cursor - len(current_word)
    if start < 0:
        return None, None
    if line_text[start:cursor] != current_word:
        start = line_text.rfind(current_word)
        if start < 0:
            return None, None
        cursor = start + len(current_word)
    return start, cursor


def _complete_path_items(current_word: str, *, start_col: int | None, end_col: int | None, cwd: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    base = Path(current_word).expanduser()
    root = Path(cwd).expanduser() if cwd else Path(".")
    if not base.is_absolute():
        base = root / base
    if current_word.endswith("/"):
        parent = base
        prefix = ""
    else:
        parent = base.parent if str(base.parent) not in {"", "."} else root
        prefix = base.name
    try:
        for entry in parent.iterdir():
            if prefix and not entry.name.startswith(prefix):
                continue
            value = str(entry)
            if cwd:
                try:
                    value = os.path.relpath(value, cwd)
                except ValueError:
                    pass
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
                    "start_col": start_col,
                    "end_col": end_col,
                }
            )
    except OSError:
        return []
    return items


class ShellRuntime:
    def __init__(self, *, argv: list[str], initial_text: str, shell_name: str) -> None:
        self.argv = argv
        self.initial_text = initial_text
        self.shell_name = shell_name
        self.master_fd = -1
        self._child_pid = 0
        self._returncode: int | None = None
        self._stop = threading.Event()
        self._stdin_thread: threading.Thread | None = None
        self._previous_sigwinch: Any = None
        self._saved_stdin_mode: list[object] | None = None
        self._cwd_state_dir = tempfile.mkdtemp(prefix="jusi-shell-state-")
        self._socket_path = os.path.join(self._cwd_state_dir, "runtime.sock")
        self._bash_rcfile = os.path.join(self._cwd_state_dir, "bashrc")
        self._zsh_rcfile = os.path.join(self._cwd_state_dir, ".zshrc")
        self._cwd_lock = threading.Lock()
        self._current_cwd: str | None = None
        self._socket_thread: threading.Thread | None = None

    def _shell_base(self) -> str:
        return os.path.basename(self.argv[0])

    def _prepare_shell_argv(self) -> list[str]:
        base = self._shell_base()
        if base == "bash":
            rcfile = self._write_bash_rcfile()
            return [self.argv[0], "--rcfile", rcfile, "-i"]
        if base == "zsh":
            self._write_zsh_rcfile()
        if base == "fish":
            return [self.argv[0], "-i", "-C", self._fish_init_command()]
        return list(self.argv)

    def _prepare_shell_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._shell_base() == "zsh":
            env["ZDOTDIR"] = self._cwd_state_dir
        return env

    def _socket_client_command(self) -> str:
        python_code = (
            'import json, os, socket; '
            + f's=socket.socket(socket.AF_UNIX); s.connect({self._socket_path!r}); '
            + 'payload=(json.dumps({"type":"cwd","cwd":os.getcwd()}) + "\\n").encode(); '
            + 's.sendall(payload); s.close()'
        )
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(python_code)} >/dev/null 2>&1"

    def _write_bash_rcfile(self) -> str:
        home_rc = os.path.expanduser("~/.bashrc")
        client_command = self._socket_client_command()
        payload = [
            'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi',
            'function __jusi_emit_cwd() { ' + client_command + '; }',
            'PROMPT_COMMAND="__jusi_emit_cwd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"',
            '__jusi_emit_cwd',
        ]
        if not os.path.exists(home_rc):
            payload = payload[1:]
        with open(self._bash_rcfile, "w", encoding="utf-8") as handle:
            handle.write("\n".join(payload) + "\n")
        return self._bash_rcfile

    def _write_zsh_rcfile(self) -> str:
        client_command = self._socket_client_command()
        payload = [
            'if [ -f "$HOME/.zshrc" ]; then . "$HOME/.zshrc"; fi',
            'autoload -Uz add-zsh-hook',
            'function __jusi_emit_cwd() { ' + client_command + '; }',
            'add-zsh-hook precmd __jusi_emit_cwd',
            '__jusi_emit_cwd',
        ]
        with open(self._zsh_rcfile, "w", encoding="utf-8") as handle:
            handle.write("\n".join(payload) + "\n")
        return self._zsh_rcfile

    def _fish_init_command(self) -> str:
        client_command = self._socket_client_command()
        return (
            'function __jusi_emit_cwd --on-event fish_prompt; ' + client_command + '; end; __jusi_emit_cwd'
        )

    def current_cwd(self) -> str | None:
        with self._cwd_lock:
            return self._current_cwd

    def _update_cwd(self, value: str) -> None:
        normalized = value.strip() or None
        with self._cwd_lock:
            self._current_cwd = normalized
        _debug_log("cwd.update", cwd=normalized)

    def _start_socket_server(self) -> None:
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        server.listen()
        server.settimeout(0.2)

        def _serve() -> None:
            try:
                while not self._stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if not self._stop.is_set():
                            _debug_log("cwd.socket.accept_error", error=str(exc))
                        return
                    with conn:
                        try:
                            data = conn.recv(8192)
                        except OSError as exc:
                            _debug_log("cwd.socket.read_error", error=str(exc))
                            continue
                    if not data:
                        continue
                    try:
                        message = json.loads(data.decode("utf-8", "replace").strip())
                    except json.JSONDecodeError as exc:
                        _debug_log("cwd.socket.decode_error", error=str(exc), preview=_safe_preview(data))
                        continue
                    if not isinstance(message, dict):
                        continue
                    if str(message.get("type", "")).strip() == "cwd":
                        self._update_cwd(str(message.get("cwd", "")))
            finally:
                server.close()

        self._socket_thread = threading.Thread(target=_serve, daemon=True)
        self._socket_thread.start()

    def shell_complete(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        line_text = str(payload.get("line_text", ""))
        current_word = str(payload.get("current_word", "")).strip()
        stripped_line = line_text.rstrip()
        trailing_token = stripped_line.rsplit(None, 1)[-1] if stripped_line.split() else ""
        active_token = trailing_token or current_word
        if not active_token:
            return []
        start_col, end_col = _replace_span(stripped_line, active_token)
        cwd = self.current_cwd()
        if "/" in active_token or len(stripped_line.split()) >= 2:
            items = _complete_path_items(active_token, start_col=start_col, end_col=end_col, cwd=cwd)
            _debug_log("shell.complete.path", cwd=cwd or os.getcwd(), token=active_token, item_count=len(items))
            return items
        return []

    def start(self) -> None:
        target_winsize = _stdin_winsize()
        self._start_socket_server()
        argv = self._prepare_shell_argv()
        pid, master_fd = pty.fork()
        if pid == 0:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGQUIT, signal.SIG_DFL)
            if target_winsize:
                try:
                    _apply_winsize(sys.stdin.fileno(), target_winsize)
                except OSError:
                    pass
            env = self._prepare_shell_env()
            try:
                os.execvpe(argv[0], argv, env)
            except Exception as exc:
                os.write(2, (str(exc) + "\n").encode("utf-8", "replace"))
                os._exit(127)
        self.master_fd = master_fd
        self._child_pid = pid
        _apply_winsize(self.master_fd, target_winsize)
        self._enter_raw_input_mode()
        self._install_sigwinch_handler()
        _debug_log(
            "shell.start",
            argv=argv,
            child_pid=self._child_pid,
            stdin_winsize=target_winsize,
            pty_winsize=_read_winsize(self.master_fd),
            stdin_tty_mode=_tty_mode_flags(sys.stdin.fileno()),
            stdout_tty_mode=_tty_mode_flags(sys.stdout.fileno()),
        )
        self._stdin_thread = threading.Thread(target=self._pump_stdin, daemon=True)
        self._stdin_thread.start()
        if self.initial_text.strip():
            self.send_text(self.initial_text, source="initial")

    def _enter_raw_input_mode(self) -> None:
        input_fd = sys.stdin.fileno()
        try:
            original = termios.tcgetattr(input_fd)
        except termios.error:
            _debug_log("stdin.raw_mode_unavailable")
            return
        self._saved_stdin_mode = list(original)
        raw = termios.tcgetattr(input_fd)
        raw[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
        raw[1] &= ~termios.OPOST
        raw[2] |= termios.CS8
        raw[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
        raw[6][termios.VMIN] = 1
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(input_fd, termios.TCSANOW, raw)
        _debug_log("stdin.raw_mode_enabled", stdin_tty_mode=_tty_mode_flags(input_fd))

    def _restore_input_mode(self) -> None:
        if self._saved_stdin_mode is None:
            return
        input_fd = sys.stdin.fileno()
        try:
            termios.tcsetattr(input_fd, termios.TCSANOW, self._saved_stdin_mode)
        except termios.error:
            _debug_log("stdin.raw_mode_restore_error")
        else:
            _debug_log("stdin.raw_mode_restored", stdin_tty_mode=_tty_mode_flags(input_fd))
        self._saved_stdin_mode = None

    def _install_sigwinch_handler(self) -> None:
        def _handle_sigwinch(_signum: int, _frame: object) -> None:
            winsize = _stdin_winsize()
            try:
                _apply_winsize(self.master_fd, winsize)
            except OSError as exc:
                _debug_log("winsize.apply_error", error=str(exc))
                return
            _debug_log("winsize.update", winsize=winsize, pty_winsize=_read_winsize(self.master_fd))

        self._previous_sigwinch = signal.signal(signal.SIGWINCH, _handle_sigwinch)

    def _restore_sigwinch_handler(self) -> None:
        if self._previous_sigwinch is None:
            return
        signal.signal(signal.SIGWINCH, self._previous_sigwinch)
        self._previous_sigwinch = None

    def _pump_stdin(self) -> None:
        input_fd = sys.stdin.fileno()
        while not self._stop.is_set() and self.master_fd >= 0:
            try:
                chunk = os.read(input_fd, 4096)
            except OSError as exc:
                _debug_log("stdin.read_error", error=str(exc))
                return
            if not chunk:
                _debug_log("stdin.eof")
                return
            _debug_log(
                "stdin.read",
                size=len(chunk),
                preview=_safe_preview(chunk),
                hex=chunk[:24].hex(),
            )
            try:
                os.write(self.master_fd, chunk)
                _debug_log(
                    "pty.write",
                    source="stdin",
                    size=len(chunk),
                    preview=_safe_preview(chunk),
                    hex=chunk[:24].hex(),
                )
            except OSError as exc:
                _debug_log("pty.write_error", source="stdin", error=str(exc))
                return

    def _poll_child(self) -> int | None:
        if self._child_pid <= 0 or self._returncode is not None:
            return self._returncode
        try:
            pid, status = os.waitpid(self._child_pid, os.WNOHANG)
        except ChildProcessError:
            self._returncode = self._returncode if self._returncode is not None else 0
            _debug_log("child.waitpid_missing", returncode=self._returncode)
            return self._returncode
        if pid == 0:
            return None
        if os.WIFEXITED(status):
            self._returncode = os.WEXITSTATUS(status)
            _debug_log("child.exited", returncode=self._returncode)
        elif os.WIFSIGNALED(status):
            self._returncode = 128 + os.WTERMSIG(status)
            _debug_log("child.signaled", signal=os.WTERMSIG(status), returncode=self._returncode)
        else:
            self._returncode = 1
            _debug_log("child.unknown_exit", status=status, returncode=self._returncode)
        return self._returncode

    def send_text(self, text: str, *, source: str) -> None:
        if self.master_fd < 0:
            return
        data = text.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        try:
            os.write(self.master_fd, data)
            _debug_log(
                "pty.write",
                source=source,
                size=len(data),
                preview=_safe_preview(data),
                hex=data[:24].hex(),
            )
        except OSError as exc:
            _debug_log("pty.write_error", source=source, error=str(exc))
            return

    def interrupt(self) -> None:
        if self._child_pid <= 0 or self._poll_child() is not None:
            return
        try:
            os.killpg(self._child_pid, signal.SIGINT)
            _debug_log("child.interrupt", child_pid=self._child_pid)
        except ProcessLookupError:
            return

    def stop(self) -> None:
        self._stop.set()
        self._restore_sigwinch_handler()
        self._restore_input_mode()
        if self._socket_thread is not None and self._socket_thread.is_alive():
            self._socket_thread.join(timeout=0.2)
        if self._child_pid > 0 and self._poll_child() is None:
            try:
                os.killpg(self._child_pid, signal.SIGTERM)
                _debug_log("child.term", child_pid=self._child_pid)
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
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass
        shutil.rmtree(self._cwd_state_dir, ignore_errors=True)

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
                    except OSError as exc:
                        _debug_log("pty.read_error", error=str(exc))
                        chunk = b""
                    if not chunk:
                        continue
                    _debug_log(
                        "pty.read",
                        size=len(chunk),
                        preview=_safe_preview(chunk),
                        hex=chunk[:24].hex(),
                    )
                    output.write(chunk)
                    output.flush()
            while True:
                try:
                    chunk = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                _debug_log(
                    "pty.read",
                    size=len(chunk),
                    preview=_safe_preview(chunk),
                    hex=chunk[:24].hex(),
                    phase="drain",
                )
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


def _resolve_shell(shell_name: str) -> tuple[str, list[str]]:
    requested = shell_name.strip()
    if requested:
        resolved = shutil.which(requested)
        if resolved is None:
            raise RuntimeError(f"requested shell is not available: {requested}")
        shell_path = resolved
    else:
        shell_path = str(os.environ.get("SHELL", "")).strip() or "/bin/sh"
    shell_base = os.path.basename(shell_path)
    argv = [shell_path]
    if shell_base in _INTERACTIVE_SHELL_NAMES and shell_base != "bash":
        argv.append("-i")
    return shell_base, argv


def run_shell_runner() -> int:
    runtime: ShellRuntime | None = None
    previous_sigquit = signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        payload = _load_payload()
        content = str(payload.get("content", ""))
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise RuntimeError("invalid shell payload meta")
        shell_name = str(meta.get("shell", "")).strip()
        shell_base, argv = _resolve_shell(shell_name)
        emit_timing("shell.runner.start", shell=shell_name or argv[0])
        _debug_log("runner.start", shell=shell_name, argv=argv, payload_keys=sorted(payload.keys()))
        runtime = ShellRuntime(argv=argv, initial_text=content, shell_name=shell_base)
        runtime.start()

        def _handle_control(request: dict[str, Any]) -> dict[str, Any]:
            message_type = str(request.get("message_type", "")).strip()
            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            emit_timing("shell.control.request", message_type=message_type)
            _debug_log("control.request", message_type=message_type, payload_keys=sorted(payload.keys()))
            if message_type == "followup":
                runtime.send_text(str(payload.get("cell_text", "")), source="followup")
                return {}
            if message_type == "complete":
                items = runtime.shell_complete(dict(payload))
                _debug_log("control.complete", item_count=len(items))
                return {"items": items}
            raise RuntimeError(f"unsupported shell runtime request: {message_type}")

        set_plugin_control_handler(_handle_control)
        return runtime.wait_forever()
    except Exception as exc:
        emit_timing("shell.runner.error", error_type=type(exc).__name__, error_message=str(exc))
        _debug_log("runner.error", error_type=type(exc).__name__, error_message=str(exc))
        sys.stderr.write(str(exc) + "\n")
        sys.stderr.flush()
        return 2
    finally:
        signal.signal(signal.SIGQUIT, previous_sigquit)
        signal.signal(signal.SIGINT, previous_sigint)
        if runtime is not None:
            runtime.stop()
        _debug_log("runner.stop")
