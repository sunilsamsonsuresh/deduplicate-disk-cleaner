"""The undo log: every move is written to a file, so you can reverse it later."""

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import file_hashes
from .config import Config
from .helpers import log
from .safe_moves import SafetyError, prune_empty_parents, resolve_inside, safe_move


class Manifest:
    """The undo log of one run. It is a JSONL file: one JSON object on each line.
    The log gets each operation *before* it happens ("planned") and again after it ("done").
    Each line goes to the disk at once (fsync). Thus a crash cannot lose a line."""

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
    """Read an undo log. Give the header and the list of operations (each one only once)."""
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
                if key not in seen:  # the 'planned' line and the 'done' line are for the same operation
                    seen.add(key)
                    ops.append(obj)
    return header, ops


def list_manifests(cfg: Config) -> list[Path]:
    """Give all undo logs, oldest first."""
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
    """Reverse the operations in an undo log, last one first."""
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
                    res.already_in_place += 1  # not moved (the run stopped before the move), or undone before
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
                if op["dst"].lower().startswith(cfg.quarantine_dir.lower() + "/"):
                    prune_empty_parents(dst, cfg.quarantine_path)
                else:
                    prune_empty_parents(dst, cfg.organize_path)
            elif op["op"] == "copy":
                # To undo a copy, remove the copy. Do this only if the copy is still the same.
                if not dst.exists():
                    res.already_in_place += 1
                    continue
                if not src.exists():
                    res.problems.append(f"original missing, keeping the copy: {op['dst']}")
                    continue
                if file_hashes.full_hash(src) != file_hashes.full_hash(dst):
                    res.problems.append(f"copy differs from original, keeping it: {op['dst']}")
                    continue
                man.planned("delete-copy", op["dst"], op["src"])
                dst.unlink()
                man.done("delete-copy", op["dst"], op["src"])
                res.copies_removed += 1
                prune_empty_parents(dst, cfg.organize_path)
    return res
