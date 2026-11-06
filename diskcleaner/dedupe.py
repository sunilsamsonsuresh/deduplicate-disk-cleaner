"""Apply a (possibly user-edited) duplicates CSV: move REMOVE files to quarantine,
after re-verifying each one against a file that is definitely being kept."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import hashing, media
from .config import Config
from .index import Index
from .scope import WHOLE_DISK, Scope
from .safeops import Manifest, SafetyError, resolve_inside, safe_move
from .utils import log, parallel_map

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
    refused_groups: list[str] = field(default_factory=list)  # human-readable reasons
    review_count: int = 0
    skip_count: int = 0
    outside_scope: int = 0  # REMOVE rows ignored because they are outside --folder

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
    """Read the duplicates CSV. With a scope, only files inside those folders are
    removed (even if the CSV says REMOVE elsewhere)."""
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

    # path -> groups (tier, gid) in which it is marked REMOVE
    removed_in: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for gid, members in groups.items():
        if included(members):
            for m in members:
                if m["action"] == "REMOVE":
                    removed_in[m["path"]].append((m["tier"], gid))

    def survives(path: str, tier_rank: int, depth: int = 0) -> bool:
        """Does this file's content survive the plan? Either it is not removed at all,
        or it is only removed by a *later* tier whose own keeper survives (e.g. the
        keeper of an exact group being a pixel-identical copy of another kept file)."""
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
    # Exact-tier removals first: they are verified against a keeper that a later
    # tier may itself move away.
    if not scope.whole_disk:
        inside = [r for r in plan.removals if scope.contains(r.path)]
        plan.outside_scope = len(plan.removals) - len(inside)
        plan.removals = inside
    plan.removals.sort(key=lambda r: TIER_ORDER[r.tier])
    return plan


class _Verifier:
    """Re-computes hashes of the kept file once per run."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache: dict[tuple[str, str], str | None] = {}

    def _h(self, kind: str, path: Path) -> str | None:
        key = (kind, str(path))
        if key not in self.cache:
            fn = {"exact": hashing.full_hash, "pixel": media.pixel_hash, "similar": media.dhash128}[kind]
            self.cache[key] = fn(path)
        return self.cache[key]

    def check(self, tier: str, src: Path, keeper: Path) -> str | None:
        """None if it is safe to remove src, otherwise the reason it isn't."""
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
            self._h("similar", keeper)  # raises if the kept image is unreadable
        return None


def _precheck(cfg: Config, verifier: _Verifier, item: PlannedRemoval) -> str | None:
    """Everything that can be checked before moving: None if it is safe to remove, else why not.
    This is the slow part (it re-reads both files), so it runs on several threads."""
    src = resolve_inside(cfg.root, item.path)
    keeper = resolve_inside(cfg.root, item.keeper)
    if item.path.lower().startswith(cfg.quarantine_dir.lower() + "/"):
        return "already in quarantine"
    if not src.is_file():
        return "no longer exists"
    return verifier.check(item.tier, src, keeper)


def apply_plan(cfg: Config, plan: DedupePlan, index: Index | None = None, csv_name: str = "") -> DedupeResult:
    """Move every planned removal to the quarantine folder.

    The safety checks run in parallel (cfg.workers threads); the moves themselves
    happen one at a time, in plan order, each written to the undo log before and
    after. A file's kept copy is never moved earlier in the plan than the file
    itself (load_plan guarantees this), so checking ahead of the moves is safe;
    the kept copy's existence is checked once more right before each move."""
    res = DedupeResult()
    verifier = _Verifier(cfg)
    with Manifest(cfg, "dedupe", {"plan": csv_name}) as man:
        run_dir_rel = f"{cfg.quarantine_dir}/{man.run_id}"
        res.manifest = man.path
        res.quarantine_run = cfg.root / run_dir_rel
        checks = parallel_map(lambda it: _precheck(cfg, verifier, it), plan.removals, cfg.workers,
                              "Checking and moving duplicates to quarantine")
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
# Remove the files named in a plain text list (e.g. downloaded movies)
# ---------------------------------------------------------------------------
def read_path_list(list_path: Path) -> list[str]:
    """One path per line, relative to the disk root. Blank lines and # comments are ignored."""
    paths = []
    for line in list_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in paths:
            paths.append(line)
    return paths


def quarantine_list(cfg: Config, paths: list[str], index: Index | None = None, list_name: str = "") -> DedupeResult:
    """Move each listed file to the quarantine folder, with an undo log - the same
    way duplicates are moved, but without a kept copy (you chose these yourself)."""
    res = DedupeResult()
    with Manifest(cfg, "remove-list", {"list": list_name}) as man:
        run_dir_rel = f"{cfg.quarantine_dir}/{man.run_id}"
        res.manifest = man.path
        res.quarantine_run = cfg.root / run_dir_rel
        for rel in paths:
            try:
                src = resolve_inside(cfg.root, rel)  # refuses paths outside the disk
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
