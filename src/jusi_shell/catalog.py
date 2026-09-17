"""Discovery-only catalog entry; runtime modules are deliberately not imported."""

from . import __version__


def catalog_entry() -> dict[str, object]:
    return {
        "plugin_id": "jusi_shell",
        "plugin_version": __version__,
        "distribution": "jusi-shell",
        "families": [
            {
                "family_id": "shell",
                "magic_name": "shell",
                "capabilities": ["execute", "followup", "complete", "editor_actions"],
                "presentation": {"syntax": "sh", "indent": "sh"},
            }
        ],
        "kernel_extensions": ["jusi_shell.kernel"],
        "worker_entry_point": "jusi_shell.worker:create_worker",
        "media_types": ["text/x-ansi"],
        "interaction": "terminal_interactive",
    }
