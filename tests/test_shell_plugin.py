from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from jusi.plugin_api import validate_discovered_entry
from jusi.application.ports import TerminalLaunchSpec
from jusi.infrastructure.terminal_pty import PosixTerminalBroker

from jusi_shell import __version__
from jusi_shell.catalog import catalog_entry
from jusi_shell.completion import complete_paths
from jusi_shell.control import exchange
from jusi_shell.kernel import configure_jusi_runtime_v1, jusi_kernel_adapter_v1, load_ipython_extension
from jusi_shell.worker import ShellWorker, _followup_body


class FakeIPython:
    def __init__(self) -> None:
        self.magics_manager = type("MagicsManager", (), {"magics": {"cell": {}}})()
        self.registered: dict[tuple[str, str], object] = {}

    def register_magic_function(self, function, magic_kind: str, magic_name: str) -> None:  # type: ignore[no-untyped-def]
        self.registered[(magic_kind, magic_name)] = function


class CatalogAndKernelTest(unittest.TestCase):
    def test_catalog_is_a_valid_exact_provider(self) -> None:
        entry = validate_discovered_entry(
            catalog_entry(),
            entry_point_name="jusi_shell",
            distribution="jusi-shell",
            distribution_version=__version__,
        )
        self.assertEqual(["execute", "followup", "complete", "editor_actions"], entry["families"][0]["capabilities"])
        self.assertEqual("jusi_shell.worker:create_worker", entry["worker_entry_point"])

    def test_kernel_adapter_matches_catalog(self) -> None:
        self.assertEqual(
            {
                "plugin_id": "jusi_shell",
                "plugin_version": __version__,
                "families": [{"family_id": "shell", "magic_name": "shell"}],
            },
            jusi_kernel_adapter_v1(),
        )

    def test_magic_accepts_shell_and_working_directory(self) -> None:
        captured: dict[str, object] = {}

        def display(value, *, raw=False):  # type: ignore[no-untyped-def]
            captured.update(value)
            captured["raw"] = raw

        configure_jusi_runtime_v1({"shell": {"default": "zsh"}})
        ipython = FakeIPython()
        with tempfile.TemporaryDirectory() as directory:
            with patch("IPython.display.display", display):
                load_ipython_extension(ipython)
                magic = ipython.registered[("cell", "shell")]
                magic(f"--cwd {directory}", "pwd")  # type: ignore[operator]

            handoff = captured["application/vnd.jusi.handoff.v1+json"]
            self.assertTrue(captured["raw"])
            self.assertEqual("plugin.handoff", handoff["kind"])
            self.assertEqual("zsh", handoff["payload"]["shell"])
            self.assertEqual(directory, handoff["payload"]["cwd"])
            self.assertEqual("pwd", handoff["payload"]["body"])


class CompletionTest(unittest.TestCase):
    def test_absolute_path_stays_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "alpha dir").mkdir()
            prefix = f"cat {directory}/al"
            result = complete_paths({"body": prefix + "suffix", "prefix": prefix, "cursor_pos": len(prefix)}, "/")
            (item,) = result["items"]
            self.assertEqual(f"{directory}/alpha dir/", item["text"])
            self.assertTrue(item["text"].startswith("/"))
            self.assertEqual(len("cat "), item["start"])
            self.assertEqual(len(prefix), item["end"])

    def test_relative_multiline_path_uses_unicode_codepoint_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "δelta.txt").touch()
            prefix = "echo α\ncat δ"
            result = complete_paths({"prefix": prefix, "cursor_pos": len(prefix)}, directory)
            (item,) = result["items"]
            self.assertEqual("δelta.txt", item["text"])
            self.assertEqual(prefix.rfind("δ"), item["start"])
            self.assertEqual(len(prefix), item["end"])

    def test_empty_and_command_prefixes_do_not_offer_paths(self) -> None:
        self.assertEqual([], complete_paths({"prefix": "", "cursor_pos": 0}, "/")["items"])
        self.assertEqual([], complete_paths({"prefix": "ec", "cursor_pos": 2}, "/")["items"])


class WorkerTest(unittest.TestCase):
    def test_followup_removes_its_magic_header(self) -> None:
        worker = ShellWorker(object())
        with patch.object(worker, "_request") as request:
            result = worker.handle("followup", {"body": "%%shell --cwd /tmp/example\nls"})

        self.assertTrue(result.result["accepted"])
        request.assert_called_once_with({"type": "followup", "body": "ls"})

    def test_followup_header_detection_is_exact(self) -> None:
        self.assertEqual("pwd\n", _followup_body("  %%shell\t--cwd /tmp\npwd\n"))
        self.assertEqual("%%shellish\nls", _followup_body("%%shellish\nls"))

    def test_execute_returns_one_terminal_surface_with_requested_cwd(self) -> None:
        worker = ShellWorker(object())
        with tempfile.TemporaryDirectory() as directory:
            result = worker.handle("execute", {"shell": "sh", "cwd": directory, "body": "pwd"})
            self.assertTrue(result.result["accepted"])
            self.assertEqual(directory, result.result["cwd"])
            (surface,) = result.core_requests
            self.assertEqual("shell_terminal", surface.request_id)
            self.assertEqual(directory, surface.cwd)
            self.assertEqual(("input", "resize", "signal"), surface.capabilities)
        worker.close()

    def test_recoverable_bad_cwd_does_not_initialize_worker(self) -> None:
        from jusi.plugin_api import OperationRejected

        worker = ShellWorker(object())
        with self.assertRaises(OperationRejected):
            worker.handle("execute", {"cwd": "/definitely/not/a/jusi-shell-directory"})
        self.assertIsNone(worker.runtime_directory)

    def test_editor_actions_export_content_not_paths(self) -> None:
        worker = ShellWorker(object())
        copied = worker.handle("editor_action", {"action": "copy", "selection": {"text": "/target/file"}})
        opened = worker.handle("editor_action", {"action": "open", "selection": {"text": "echo ok\n"}})
        self.assertEqual("/target/file", copied.result["text"])
        self.assertEqual("echo ok\n", opened.result["text"])
        self.assertEqual("shell-selection.sh", opened.result["name"])


class ApplicationProcessTest(unittest.TestCase):
    def test_real_application_accepts_followup_over_its_control_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_path = Path(directory, "launch.json")
            socket_path = str(Path(directory, "control.sock"))
            launch_path.write_text(json.dumps({"shell": "sh", "body": ""}), encoding="utf-8")
            master, slave = pty.openpty()
            env = os.environ.copy()
            source = str(Path(__file__).resolve().parents[1] / "src")
            env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.Popen(
                [sys.executable, "-m", "jusi_shell.application", str(launch_path), socket_path],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=directory,
                env=env,
                close_fds=True,
            )
            os.close(slave)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(socket_path) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(socket_path))
                self.assertEqual(os.path.realpath(directory), os.path.realpath(exchange(socket_path, {"type": "status"})["cwd"]))
                self.assertTrue(exchange(socket_path, {"type": "followup", "body": "printf JUSI_OK; exit"})["ok"])
                output = b""
                while process.poll() is None and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                process.wait(timeout=2)
                self.assertIn(b"JUSI_OK", output)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                os.close(master)

    @unittest.skipIf(os.name == "nt", "POSIX terminal lifecycle")
    def test_core_sigterm_closes_application_without_kill_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_path = Path(directory, "launch.json")
            socket_path = str(Path(directory, "control.sock"))
            launch_path.write_text(json.dumps({"shell": "sh", "body": ""}), encoding="utf-8")
            source = str(Path(__file__).resolve().parents[1] / "src")
            spec = TerminalLaunchSpec(
                argv=(sys.executable, "-m", "jusi_shell.application", str(launch_path), socket_path),
                cwd=directory,
                env={"TERM": "xterm-256color", "PYTHONPATH": source},
            )
            handle = PosixTerminalBroker().start(spec, rows=24, cols=80)
            deadline = time.monotonic() + 5
            while not os.path.exists(socket_path) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(os.path.exists(socket_path))

            started = time.monotonic()
            self.assertEqual("stopped", handle.stop(timeout=2.0))
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.0)
            self.assertEqual(0, handle.diagnostics.exit_code)
            self.assertIsNone(handle.diagnostics.signal)


if __name__ == "__main__":
    unittest.main()
