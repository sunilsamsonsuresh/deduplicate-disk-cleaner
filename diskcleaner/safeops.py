"""Safe file operations: never-overwrite moves, verified cross-disk copies,
crash-safe JSONL manifests and undo."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import hashing
from .config import Config
from .utils import log


class SafetyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Path guards
# ---------------------------------------------------------------------------
def resolve_inside(root: Path, rel: str) -> Path:
    """Turn a relative path from a report into an absolute path, refusing anything
    that would point outside the disk root."""
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
    """Fail early (before touching anything) if the disk is read-only - e.g. an NTFS
    disk on macOS, which mounts read-only without extra drivers."""
    probe_dir = cfg.root
    probe = probe_dir / f".diskcleaner_write_test_{uuid.uuid4().hex}"
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
    """True if a and b (or b's nearest existing parent) live on the same volume."""
    while not b.exists():
        if b.parent == b:
            return False
        b = b.parent
    return os.stat(a).st_dev == os.stat(b).st_dev


def unique_path(dst: Path, reserved: set[str] | None = None) -> Path:
    """dst, or 'name (2).ext', 'name (3).ext'... - whichever is free on disk and not
    already reserved (case-insensitively, since exFAT/APFS are usually case-insensitive)."""
    reserved = reserved if reserved is not None else set()
    cand, n = dst, 1
    while cand.exists() or str(cand).lower() in reserved:
        n += 1
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
    return cand


# ---------------------------------------------------------------------------
# Moves & copies
# ---------------------------------------------------------------------------
def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def safe_move(src: Path, dst: Path) -> None:
    """Move src to dst. Never overwrites. Same volume: atomic rename.
    Different volume: copy -> verify hash -> remove source."""
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
    """Copy with metadata, verify the copy hash-for-hash, never overwrite."""
    if dst.exists():
        raise SafetyError(f"refusing to overwrite existing file: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".diskcleaner-partial")
    try:
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        if hashing.full_hash(src) != hashing.full_hash(tmp):
            raise SafetyError(f"copy verification failed for {src}")
        os.rename(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def prune_empty_parents(path: Path, stop: Path) -> None:
    """Remove now-empty folders from path's parent up to (not including) stop."""
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


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
class Manifest:
    """Append-only JSONL log. Each operation is written *before* it happens
    ("planned") and again once complete ("done"), each line fsync'd."""

    def __init__(self, cfg: Config, action: str, extra: dict | None = None):
        cfg.ensure_state_dirs()
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = cfg.manifests_dir / f"{ts}_{action}.jsonl"
        n = 1
        while self.path.exists():
            n += 1
            self.path = cfg.manifests_dir / f"{ts}_{action}_{n}.jsonl"
        self.run_id = self.path.stem
        self._fh = open(self.path, "a", encoding="utf-8")
        self._write({"type": "header", "action": action, "root": str(cfg.root),
                     "created": dt.datetime.now().isoformat(timespec="seconds"), **(extra or {})})

    def _write(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def planned(self, op: str, src_rel: str, dst_rel: str, **info) -> None:
        self._write({"type": "op", "status": "planned", "op": op, "src": src_rel, "dst": dst_rel, **info})

    def done(self, op: str, src_rel: str, dst_rel: str, **info) -> None:
        self._write({"type": "op", "status": "done", "op": op, "src": src_rel, "dst": dst_rel, **info})

    def note(self, **info) -> None:
        self._write({"type": "note", **info})

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_manifest(path: Path) -> tuple[dict, list[dict]]:
    header, ops, seen = {}, [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping corrupt manifest line in %s", path)
                continue
            if obj.get("type") == "header":
                header = obj
            elif obj.get("type") == "op":
                key = (obj["op"], obj["src"], obj["dst"])
                if key not in seen:  # 'planned' and 'done' lines describe the same op
                    seen.add(key)
                    ops.append(obj)
    return header, ops


def list_manifests(cfg: Config) -> list[Path]:
    if not cfg.manifests_dir.exists():
        return []
    return sorted(cfg.manifests_dir.glob("*.jsonl"))


@dataclass
class UndoResult:
    restored: int = 0
    already_in_place: int = 0
    copies_removed: int = 0
    problems: list[str] = field(default_factory=list)


def undo(cfg: Config, manifest_path: Path) -> UndoResult:
    header, ops = read_manifest(manifest_path)
    if not header:
        raise SafetyError(f"{manifest_path} is not a diskcleaner manifest")
    res = UndoResult()
    root = cfg.root
    with Manifest(cfg, "undo", {"undoes": manifest_path.name}) as man:
        for op in reversed(ops):
            src = resolve_inside(root, op["src"])
            dst = resolve_inside(root, op["dst"])
            if op["op"] == "move":
                if src.exists() and not dst.exists():
                    res.already_in_place += 1  # never moved (crash before move) or already undone
                    continue
                if not dst.exists():
                    res.problems.append(f"missing (purged?): {op['dst']}")
                    continue
                if src.exists():
                    res.problems.append(f"original location is occupied, left in place: {op['dst']} -> {op['src']}")
                    continue
                man.planned("move", op["dst"], op["src"])
                safe_move(dst, src)
                man.done("move", op["dst"], op["src"])
                res.restored += 1
                stop = cfg.quarantine_path if op["dst"].lower().startswith(cfg.quarantine_dir.lower() + "/") else cfg.organize_path
                prune_empty_parents(dst, stop)
            elif op["op"] == "copy":
                # A copy is undone by removing the copy - only if it is still identical.
                if not dst.exists():
                    res.already_in_place += 1
                    continue
                if not src.exists():
                    res.problems.append(f"original missing, keeping the copy: {op['dst']}")
                    continue
                if hashing.full_hash(src) != hashing.full_hash(dst):
                    res.problems.append(f"copy differs from original, keeping it: {op['dst']}")
                    continue
                man.planned("delete-copy", op["dst"], op["src"])
                dst.unlink()
                man.done("delete-copy", op["dst"], op["src"])
                res.copies_removed += 1
                prune_empty_parents(dst, cfg.organize_path)
    return res
