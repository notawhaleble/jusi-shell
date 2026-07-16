# jusi-shell

Shell plugin for Jusi.

Current scope:
- `%%shell` starts an interactive shell in a terminal-backed plugin runtime
- `%%shell bash` selects a specific shell executable
- follow-up sends the next cell body into the live shell session
- `jusi-open PATH` requests frontend `open_path` in a top split
- `jusi-open -t PATH` or `jusi-open --tab PATH` requests frontend `open_path` in a new tab
