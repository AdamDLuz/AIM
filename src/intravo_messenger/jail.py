"""Keep file paths inside a machine's GitHub folder.

A shell task is not sandboxed: the shared secret is permission to run as the
user who started the daemon. Uploads, downloads, and returned artifacts are
sandboxed, because those paths are chosen by the other machine.
"""

from __future__ import annotations

import os
from pathlib import Path

_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class PathJailError(ValueError):
    pass


def is_inside(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or a path under it.

    Comparison is case-insensitive on Windows. Symlinks are resolved first,
    so a link that points out of `parent` is outside.
    """
    child_text = os.path.normcase(str(child.resolve()))
    parent_text = os.path.normcase(str(parent.resolve()))
    if child_text == parent_text:
        return True
    prefix = parent_text if parent_text.endswith(os.sep) else parent_text + os.sep
    return child_text.startswith(prefix)


def _check_component(part: str) -> None:
    if part in (".", ".."):
        return
    if part.endswith(" ") or part.endswith("."):
        raise PathJailError(f"illegal path component: {part!r}")
    if "\x00" in part or any(ch in part for ch in '<>:"|?*'):
        raise PathJailError(f"illegal character in path component: {part!r}")
    stem = part.split(".", 1)[0].lower()
    if stem in _RESERVED:
        raise PathJailError(f"reserved device name in path: {part}")


def _components(candidate: Path) -> list[str]:
    parts: list[str] = []
    for part in candidate.parts:
        if part in ("/", "\\"):
            continue
        # Windows drive anchor, for example 'C:\\'.
        if len(part) == 3 and part[1] == ":" and part[2] in ("\\", "/"):
            continue
        if len(part) == 2 and part[1] == ":":
            continue
        parts.append(part)
    return parts


def resolve_inside(root: Path, rel: str | Path) -> Path:
    """Resolve `rel` against `root` and require the result to stay inside it."""
    if rel is None:
        raise PathJailError("missing path")
    raw = str(rel).strip()
    if raw == "":
        return root.resolve()
    if "\x00" in raw:
        raise PathJailError("nul in path")
    candidate = Path(raw)
    for part in _components(candidate):
        _check_component(part)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    if not is_inside(resolved, root):
        raise PathJailError(f"path escapes {root}")
    return resolved


def assert_outside_home(path: Path, home: Path) -> None:
    """Refuse to read or write the messenger's own config and secret store."""
    if is_inside(path, home):
        raise PathJailError("path is inside the messenger home directory")
