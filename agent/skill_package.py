"""Canonical identity for an installed skill package."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path


def iter_skill_package_entries(root: Path) -> Iterator[Path]:
    """Yield package files and symlinks without following directory links."""
    entries: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        linked_dirs = [name for name in dirs if (current_path / name).is_symlink()]
        dirs[:] = sorted(name for name in dirs if name not in linked_dirs)
        entries.extend(current_path / name for name in linked_dirs)
        entries.extend(current_path / name for name in files)
    yield from sorted(entries, key=lambda item: item.relative_to(root).as_posix())


def skill_package_digest(skill_md: Path) -> str:
    """Hash a skill's complete directory tree, including relative paths.

    SKILL.md alone is insufficient: references, scripts, templates, and assets
    are executable/behavioral parts of the package. Symlinks are not followed
    outside the package; their link target text is hashed instead.
    """
    root = skill_md.parent
    digest = hashlib.sha256()
    for path in iter_skill_package_entries(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + path.readlink().as_posix().encode("utf-8"))
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()
