"""Folder-by-folder scoping.

The whole disk is always indexed, so we can tell you *where else* a file
exists. A scope only limits which files may be changed (removed / organized)
in this run. Copies outside the scope are reported but never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ConfigError


@dataclass(frozen=True)
class Scope:
    folders: tuple[str, ...] = field(default_factory=tuple)  # relative to root, POSIX

    @property
    def whole_disk(self) -> bool:
        return not self.folders

    def contains(self, rel_path: str) -> bool:
        if not self.folders:
            return True
        low = rel_path.lower()
        return any(low == f.lower() or low.startswith(f.lower() + "/") for f in self.folders)

    def label(self) -> str:
        return "whole disk" if self.whole_disk else ", ".join(self.folders)

    def as_list(self) -> list[str]:
        return list(self.folders) if self.folders else ["(whole disk)"]


WHOLE_DISK = Scope()


def resolve_scope(cfg: Config, folders: list[str] | None) -> Scope:
    """Validate folder names (relative to the disk root, or absolute paths on it).
    A typo must not silently mean 'nothing' or 'everything', so unknown folders are errors."""
    if not folders:
        return WHOLE_DISK
    out: list[str] = []
    root = cfg.root.resolve()
    for f in folders:
        f = f.strip()
        if not f:
            continue
        p = Path(f).expanduser()
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(root)
            except ValueError as exc:
                raise ConfigError(f"Folder {f!r} is not on the disk {cfg.root}") from exc
        else:
            rel = Path(f.strip("/"))
        if ".." in rel.parts:
            raise ConfigError(f"Folder may not contain '..': {f!r}")
        rel_s = rel.as_posix()
        if rel_s in ("", "."):
            return WHOLE_DISK
        if not (root / rel).is_dir():
            raise ConfigError(f"Folder not found on the disk: {rel_s!r}  (paths are relative to {cfg.root})")
        if Scope((cfg.quarantine_dir,)).contains(rel_s):
            raise ConfigError("The quarantine folder cannot be selected")
        if rel_s not in out:
            out.append(rel_s)
    return Scope(tuple(out))
