"""Safe file moves. Never overwrite a file. Check each copy between disks.
Keep all paths inside the disk."""

import os
import shutil
import uuid
from pathlib import Path

from . import file_hashes
from .config import Config


class SafetyError(Exception):
    """A check failed. The tool stops this step to keep your files safe."""


# ---------------------------------------------------------------------------
# Path guards
# ---------------------------------------------------------------------------
def resolve_inside(root: Path, rel: str) -> Path:
    """Change a relative path from a report to a full path.
    Refuse a path that goes outside the disk root."""
    if not rel or rel.startswith(("/", "\\")):
        raise SafetyError(f"path must be relative to the disk root: {rel!r}")
    p = Path(rel)
    if ".." in p.parts:
        raise SafetyError(f"path may not contain '..': {rel!r}")
    full = root / p
    try:
        full.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SafetyError(f"path escapes disk root: {rel!r}") from exc
    return full


def check_writable(cfg: Config) -> None:
    """Stop before any change if the disk is read-only.
    (For example, macOS can only read an NTFS disk if it has no extra driver.)"""
    probe = cfg.root / f".diskcleaner_write_test_{uuid.uuid4().hex}"
    try:
        with open(probe, "wb") as fh:
            fh.write(b"ok")
        probe.unlink()
    except OSError as exc:
        raise SafetyError(
            f"The disk at {cfg.root} is not writable ({exc.strerror}).\n"
            "If it is NTFS-formatted, macOS can only read it. Options: use a Windows PC, "
            "install an NTFS driver, or copy the files to an exFAT/APFS disk first."
        ) from exc


def same_device(a: Path, b: Path) -> bool:
    """True if a and b are on the same disk. (If b does not exist yet, use its nearest parent folder.)"""
    while not b.exists():
        if b.parent == b:
            return False
        b = b.parent
    return os.stat(a).st_dev == os.stat(b).st_dev


def unique_path(dst: Path, reserved: set[str] | None = None) -> Path:
    """Give dst, or 'name (2).ext', 'name (3).ext' and so on: the first name that is free.
    A name is not free if it is on the disk or in `reserved`. The check ignores upper
    and lower case, because exFAT and APFS usually ignore case."""
    if reserved is None:
        reserved = set()
    cand, n = dst, 1
    while cand.exists() or str(cand).lower() in reserved:
        n += 1
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
    return cand


# ---------------------------------------------------------------------------
# Moves & copies
# ---------------------------------------------------------------------------
def _fsync_dir(d: Path) -> None:
    """Tell the disk to save the folder change now."""
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def safe_move(src: Path, dst: Path) -> None:
    """Move src to dst. Never overwrite a file.
    On the same disk: rename (one step). On a different disk: copy, check the hash, then remove src."""
    if not src.is_file():
        raise SafetyError(f"source missing: {src}")
    if dst.exists():
        raise SafetyError(f"refusing to overwrite existing file: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if same_device(src, dst.parent):
        os.rename(src, dst)
        _fsync_dir(dst.parent)
        return
    safe_copy(src, dst)
    os.unlink(src)


def safe_copy(src: Path, dst: Path) -> None:
    """Copy src to dst with its dates. Check that the copy has the same hash. Never overwrite a file."""
    if dst.exists():
        raise SafetyError(f"refusing to overwrite existing file: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".diskcleaner-partial")
    try:
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        if file_hashes.full_hash(src) != file_hashes.full_hash(tmp):
            raise SafetyError(f"copy verification failed for {src}")
        os.rename(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def prune_empty_parents(path: Path, stop: Path) -> None:
    """Remove the empty folders above path, up to stop (but not stop itself)."""
    p = path.parent
    try:
        stop = stop.resolve()
        while True:
            rp = p.resolve()
            if rp == stop or stop not in rp.parents:
                return
            entries = list(p.iterdir())
            if any(e.name != ".DS_Store" for e in entries):
                return
            for e in entries:
                e.unlink()
            p.rmdir()
            p = p.parent
    except OSError:
        return
