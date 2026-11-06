"""Use the duplicates CSV (you can edit it) to move the REMOVE files to the quarantine.
Before each move, the tool checks the file again against a copy that it keeps."""

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import file_hashes, media_info
from .config import Config
from .file_index import Index
from .selected_folders import WHOLE_DISK, Scope
from .safe_moves import SafetyError, resolve_inside, safe_move
from .undo_log import Manifest
from .helpers import log, parallel_map

REQUIRED_COLUMNS = {"group_id", "tier", "action", "path"}
TIER_ORDER = {"exact": 0, "pixel": 1, "similar": 2}


@dataclass
class PlannedRemoval:
    group_id: str
    tier: str
    path: str
    keeper: str
    size: int


@dataclass
class DedupePlan:
    removals: list[PlannedRemoval]
    refused_groups: list[str] = field(default_factory=list)  # the reasons, in words
    review_count: int = 0
    skip_count: int = 0
    outside_scope: int = 0  # REMOVE rows that the tool ignores, because they are outside --folder

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.removals)


@dataclass
class DedupeResult:
    moved: int = 0
    moved_bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    manifest: Path | None = None
    quarantine_run: Path | None = None


def load_plan(csv_path: Path, cfg: Config, only_tiers: set[str] | None = None,
              scope: Scope = WHOLE_DISK) -> DedupePlan:
    """Read the duplicates CSV. With selected folders, remove only the files in these folders
    (also if the CSV says REMOVE for other files)."""
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise SafetyError(f"{csv_path.name} is missing columns: {sorted(missing)}")
        rows = list(reader)

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        r["action"] = (r.get("action") or "").strip().upper()
        r["path"] = (r.get("path") or "").strip()
        r["tier"] = (r.get("tier") or "").strip().lower()
        groups[r["group_id"].strip()].append(r)

    def included(members: list[dict]) -> bool:
        return not only_tiers or members[0]["tier"] in only_tiers

    # for each path: the groups (tier, group id) where the path has REMOVE
    removed_in: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for gid, members in groups.items():
        if included(members):
            for m in members:
                if m["action"] == "REMOVE":
                    removed_in[m["path"]].append((m["tier"], gid))

    def survives(path: str, tier_rank: int, depth: int = 0) -> bool:
        """Does the content of this file stay on the disk after the plan?
        Yes if the plan does not remove the file. Also yes if only a *later* tier removes it,
        and the keeper of that group stays. (For example: the keeper of an exact group
        is a pixel copy of another kept file.)"""
        if path not in removed_in:
            return True
        if depth > 3:
            return False
        for tier, gid in removed_in[path]:
            rank = TIER_ORDER.get(tier, -1)
            if rank <= tier_rank:
                return False
            ks = [m["path"] for m in groups[gid] if m["action"] == "KEEP" and m["path"] != path]
            if not any(survives(k, rank, depth + 1) for k in ks):
                return False
        return True

    plan = DedupePlan(removals=[])
    plan.review_count = sum(1 for r in rows if r["action"] == "REVIEW")
    plan.skip_count = sum(1 for r in rows if r["action"] == "SKIP")
    seen = set()
    for gid, members in groups.items():
        tier = members[0]["tier"]
        if tier not in TIER_ORDER or any(m["tier"] != tier for m in members):
            plan.refused_groups.append(f"{gid}: unknown or mixed tier")
            continue
        if not included(members):
            continue
        bad = [m["action"] for m in members if m["action"] not in ("KEEP", "REMOVE", "REVIEW", "SKIP")]
        if bad:
            plan.refused_groups.append(f"{gid}: unknown action(s) {bad} - use KEEP, REMOVE, REVIEW or SKIP")
            continue
        removes = [m for m in members if m["action"] == "REMOVE"]
        if not removes:
            continue
        keep_rows = [m for m in members if m["action"] == "KEEP"]
        direct = [m["path"] for m in keep_rows if m["path"] not in removed_in]
        chained = [m["path"] for m in keep_rows if survives(m["path"], TIER_ORDER[tier])]
        keepers = direct or chained
        if not keepers:
            plan.refused_groups.append(f"{gid}: no copy would be kept - group skipped for safety")
            continue
        keeper = keepers[0]
        for m in removes:
            if m["path"] in seen or m["path"] == keeper:
                continue
            seen.add(m["path"])
            try:
                size = int(m.get("size_bytes") or 0)
            except ValueError:
                size = 0
            plan.removals.append(PlannedRemoval(gid, tier, m["path"], keeper, size))
    # Do the exact moves first. Their keeper can be a file that a later tier moves away.
    if not scope.whole_disk:
        inside = [r for r in plan.removals if scope.contains(r.path)]
        plan.outside_scope = len(plan.removals) - len(inside)
        plan.removals = inside
    plan.removals.sort(key=lambda r: TIER_ORDER[r.tier])
    return plan


class _Verifier:
    """Calculates the hashes again for the safety check. It calculates each file only one time per run."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache: dict[tuple[str, str], str | None] = {}

    def _h(self, kind: str, path: Path) -> str | None:
        key = (kind, str(path))
        if key not in self.cache:
            fn = {"exact": file_hashes.full_hash, "pixel": media_info.pixel_hash, "similar": media_info.dhash128}[kind]
            self.cache[key] = fn(path)
        return self.cache[key]

    def check(self, tier: str, src: Path, keeper: Path) -> str | None:
        """Give None if it is safe to remove src. If it is not safe, give the reason."""
        if not keeper.is_file():
            return "kept copy no longer exists"
        if src.resolve() == keeper.resolve():
            return "file is its own keeper"
        if tier == "exact":
            if src.stat().st_size != keeper.stat().st_size:
                return "size differs from kept copy (file changed since the report?)"
            if not self.cfg.verify_before_move:
                return None
            if self._h("exact", src) != self._h("exact", keeper):
                return "content differs from kept copy (file changed since the report?)"
        elif tier == "pixel":
            if self._h("pixel", src) != self._h("pixel", keeper):
                return "pixels differ from kept copy"
        else:
            self._h("similar", keeper)  # this gives an error if the tool cannot read the kept image
        return None


def _precheck(cfg: Config, verifier: _Verifier, item: PlannedRemoval) -> str | None:
    """Do all the checks that are possible before the move. Give None if it is safe, or the reason if not.
    This is the slow part (it reads both files again). Thus it runs on more than one thread."""
    src = resolve_inside(cfg.root, item.path)
    keeper = resolve_inside(cfg.root, item.keeper)
    if item.path.lower().startswith(cfg.quarantine_dir.lower() + "/"):
        return "already in quarantine"
    if not src.is_file():
        return "no longer exists"
    return verifier.check(item.tier, src, keeper)


def apply_plan(cfg: Config, plan: DedupePlan, index: Index | None = None, csv_name: str = "") -> DedupeResult:
    """Move each planned file to the quarantine folder.

    The safety checks run at the same time, on cfg.workers threads.
    The moves run one at a time, in plan order. The undo log gets each move before and after it.
    The plan never moves a kept copy before the file that it protects (load_plan makes sure of this).
    Thus it is safe to do the checks before the moves.
    Right before each move, the tool checks again that the kept copy is there."""
    res = DedupeResult()
    verifier = _Verifier(cfg)
    with Manifest(cfg, "dedupe", {"plan": csv_name}) as man:
        run_dir_rel = f"{cfg.quarantine_dir}/{man.run_id}"
        res.manifest = man.path
        res.quarantine_run = cfg.root / run_dir_rel

        def check(item):
            return _precheck(cfg, verifier, item)

        checks = parallel_map(check, plan.removals, cfg.workers, "Checking and moving duplicates to quarantine")
        for item, problem, exc in checks:
            try:
                if exc is not None:
                    raise exc
                if problem:
                    res.skipped.append(f"{item.path}: {problem}")
                    continue
                src = cfg.root / item.path
                if not (cfg.root / item.keeper).is_file():
                    res.skipped.append(f"{item.path}: kept copy no longer exists")
                    continue
                dst_rel = f"{run_dir_rel}/{item.path}"
                dst = cfg.root / dst_rel
                if dst.exists():
                    res.skipped.append(f"{item.path}: quarantine target already exists")
                    continue
                size = src.stat().st_size
                info = dict(tier=item.tier, group=item.group_id, keeper=item.keeper, size=size)
                man.planned("move", item.path, dst_rel, **info)
                safe_move(src, dst)
                man.done("move", item.path, dst_rel, **info)
                res.moved += 1
                res.moved_bytes += size
                if index is not None:
                    index.remove(item.path)
            except Exception as exc:
                res.skipped.append(f"{item.path}: {type(exc).__name__}: {exc}")
                log.warning("dedupe skipped %s: %s", item.path, exc)
        man.note(moved=res.moved, moved_bytes=res.moved_bytes, skipped=len(res.skipped))
    if index is not None:
        index.commit()
    return res


# ---------------------------------------------------------------------------
# Remove the files in a text list (for example downloaded movies)
# ---------------------------------------------------------------------------
def read_path_list(list_path: Path) -> list[str]:
    """Read one path on each line, relative to the disk root. Ignore empty lines and # comments."""
    paths = []
    for line in list_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in paths:
            paths.append(line)
    return paths


def quarantine_list(cfg: Config, paths: list[str], index: Index | None = None, list_name: str = "") -> DedupeResult:
    """Move each file in the list to the quarantine folder, with an undo log.
    This is the same as for duplicates, but there is no kept copy (you chose these files)."""
    res = DedupeResult()
    with Manifest(cfg, "remove-list", {"list": list_name}) as man:
        run_dir_rel = f"{cfg.quarantine_dir}/{man.run_id}"
        res.manifest = man.path
        res.quarantine_run = cfg.root / run_dir_rel
        for rel in paths:
            try:
                src = resolve_inside(cfg.root, rel)  # this stops paths outside the disk
                if rel.lower().startswith(cfg.quarantine_dir.lower() + "/"):
                    res.skipped.append(f"{rel}: already in quarantine")
                    continue
                if not src.is_file():
                    res.skipped.append(f"{rel}: no longer exists")
                    continue
                dst_rel = f"{run_dir_rel}/{rel}"
                size = src.stat().st_size
                man.planned("move", rel, dst_rel, size=size)
                safe_move(src, cfg.root / dst_rel)
                man.done("move", rel, dst_rel, size=size)
                res.moved += 1
                res.moved_bytes += size
                if index is not None:
                    index.remove(rel)
            except Exception as exc:
                res.skipped.append(f"{rel}: {type(exc).__name__}: {exc}")
                log.warning("remove-list skipped %s: %s", rel, exc)
        man.note(moved=res.moved, moved_bytes=res.moved_bytes, skipped=len(res.skipped))
    if index is not None:
        index.commit()
    return res
