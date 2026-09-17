from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _active_word(prefix: str) -> tuple[str, int]:
    line_start = prefix.rfind("\n") + 1
    line = prefix[line_start:]
    index = len(line)
    while index > 0 and not line[index - 1].isspace():
        index -= 1
    return line[index:], line_start + index


def complete_paths(payload: dict[str, Any], cwd: str) -> dict[str, list[dict[str, Any]]]:
    prefix = str(payload.get("prefix", ""))
    cursor_pos = payload.get("cursor_pos")
    if not isinstance(cursor_pos, int) or cursor_pos != len(prefix):
        return {"items": []}

    word, start = _active_word(prefix)
    line_before_word = prefix[prefix.rfind("\n", 0, start) + 1:start]
    if not word or ("/" not in word and not line_before_word.strip()):
        return {"items": []}

    slash = word.rfind("/")
    typed_parent = word[: slash + 1] if slash >= 0 else ""
    name_prefix = word[slash + 1:]
    expanded_parent = Path(os.path.expanduser(typed_parent or "."))
    search_parent = expanded_parent if expanded_parent.is_absolute() else Path(cwd) / expanded_parent

    items: list[dict[str, Any]] = []
    try:
        entries = sorted(search_parent.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return {"items": []}
    for entry in entries:
        if not entry.name.startswith(name_prefix):
            continue
        text = typed_parent + entry.name
        if entry.is_dir():
            text += "/"
        items.append(
            {
                "text": text,
                "label": entry.name,
                "kind": "dir" if entry.is_dir() else "file",
                "detail": str(entry),
                "start": start,
                "end": cursor_pos,
            }
        )
    return {"items": items}
