from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from jusi.infrastructure.runtime import JUSI_SESSION_CONFIG_ENV

from jusi_shell.kernel import _session_config_from_env, load_ipython_extension
from jusi_shell.plugin import SHELL_BOOTSTRAP_BODY, ShellHandler, display_handler_specs
from jusi_shell import runner


class FakeIPython:
    def __init__(self) -> None:
        self.magics_manager = type("MagicsManager", (), {"magics": {"cell": {}}})()
        self.registered = {}

    def register_magic_function(self, func, *, magic_kind: str, magic_name: str) -> None:
        self.registered[(magic_kind, magic_name)] = func


class ShellContractTest(unittest.TestCase):
    def test_handler_spec_declares_blank_body_bootstrap(self) -> None:
        (spec,) = display_handler_specs()

        self.assertEqual("shell", spec.handler_id)
        self.assertEqual(("jusi_shell.kernel",), spec.kernel_extension_modules)
        self.assertEqual("shell", spec.magic_commands[0].name)
        self.assertIs(spec.magic_commands[0].bootstrap_body, ShellHandler.bootstrap_cell_body)
        self.assertEqual(SHELL_BOOTSTRAP_BODY, ShellHandler.bootstrap_cell_body("%%shell"))

    def test_session_config_reader_uses_jusi_kernel_env(self) -> None:
        with patch.dict(os.environ, {JUSI_SESSION_CONFIG_ENV: json.dumps({"shell": {"default": "bash"}})}, clear=False):
            self.assertEqual({"shell": {"default": "bash"}}, _session_config_from_env())

    def test_magic_uses_configured_default_shell_when_line_is_empty(self) -> None:
        captured = {}

        def fake_display(data, *, raw=False, metadata=None):
            captured["data"] = data
            captured["raw"] = raw
            captured["metadata"] = metadata

        ipython = FakeIPython()
        with patch.dict(os.environ, {JUSI_SESSION_CONFIG_ENV: json.dumps({"shell": {"default": "bash"}})}, clear=False):
            with patch("IPython.display.display", fake_display):
                load_ipython_extension(ipython)
                ipython.registered[("cell", "shell")]("", "pwd")

        payload = next(iter(captured["data"].values()))
        self.assertTrue(captured["raw"])
        self.assertEqual("shell", payload["handler_id"])
        self.assertEqual("shell", payload["magic_name"])
        self.assertEqual("pwd", payload["content"])
        self.assertEqual("bash", payload["meta"]["shell"])
        self.assertNotIn("session_config", payload["meta"])
        self.assertEqual(payload["meta"], next(iter(captured["metadata"].values())))

    def test_terminal_env_is_redacted_bootstrap_env(self) -> None:
        handler = ShellHandler()
        handler._payload = {"content": "pwd", "meta": {"shell": "bash"}}

        with patch.dict(
            os.environ,
            {
                "PATH": "/bin",
                "HOME": "/tmp/home",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "JUSI_SHELL_TERM": "xterm-test",
                "JUSI_PLUGIN_FRONTEND_ACTIONS_FILE": "/tmp/actions.jsonl",
            },
            clear=True,
        ):
            env = handler.terminal_env()

        self.assertEqual("/bin", env["PATH"])
        self.assertEqual("/tmp/home", env["HOME"])
        self.assertEqual("xterm-test", env["TERM"])
        self.assertEqual("jusi_shell.runner:run_shell_runner", env["JUSI_PLUGIN_RUNTIME_CALLABLE"])
        self.assertEqual("/tmp/actions.jsonl", env["JUSI_PLUGIN_FRONTEND_ACTIONS_FILE"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual({"content": "pwd", "meta": {"shell": "bash"}}, json.loads(env["JUSI_SHELL_PAYLOAD_JSON"]))

    def test_runner_winsize_skips_captured_stderr_without_fileno(self) -> None:
        class CapturedStderr:
            def write(self, text: str) -> int:
                return len(text)

            def flush(self) -> None:
                return None

        seen_fds = []

        def fake_read_winsize(fd: int):
            seen_fds.append(fd)
            if fd == 222:
                return {"rows": 24, "cols": 80, "xpixels": 0, "ypixels": 0}
            return None

        with patch.object(runner.sys, "stdin", type("Stream", (), {"fileno": lambda self: 111})()):
            with patch.object(runner.sys, "stdout", type("Stream", (), {"fileno": lambda self: 222})()):
                with patch.object(runner.sys, "stderr", CapturedStderr()):
                    with patch.object(runner, "_read_winsize", fake_read_winsize):
                        self.assertEqual(
                            {"rows": 24, "cols": 80, "xpixels": 0, "ypixels": 0},
                            runner._stdin_winsize(),
                        )

        self.assertEqual([111, 222], seen_fds)


if __name__ == "__main__":
    unittest.main()
