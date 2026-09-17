# jusi-shell

An interactive shell plugin for Jusi 1.0.

- `%%shell` starts the configured/default shell.
- `%%shell bash` selects a shell executable.
- `%%shell --cwd PATH` (or `-C PATH`) starts in an optional working directory.
- Follow-up cells are sent literally to the same live shell.
- Path completion follows the shell's current directory and uses explicit Jusi
  1.0 replacement ranges. Absolute input produces absolute completion text;
  relative input stays relative.
- `jusi-open PATH` sends the target-side file contents to the owning editor.
  The legacy `-t`/`--tab` spelling remains accepted, but Jusi 1.0 owns the
  destination window rather than exposing split/tab placement to plugins.

Configuration may select a default executable:

```json
{"shell": {"default": "zsh"}}
```

The plugin is discovered through the `jusi.plugins.v1` entry-point group.
