"""Tiered duplicate detection and keeper selection.

Tier 1  exact    byte-identical (size -> partial hash -> full BLAKE2b hash)
Tier 2  pixel    images whose decoded pixels are identical (metadata differs)
Tier 3  similar  perceptually similar images (dHash + BK-tree), REVIEW by default

Each tier only sees the *keepers* of the previous tier, so one file is never
proposed for removal by two tiers.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import hashing, media
from .config import Config
from .dates import is_meaningful_folder
from .filetypes import DECODABLE_IMAGE_EXT
from .index import FileRow, Index
from .scope import WHOLE_DISK, Scope
from .utils import log, parallel_map

KEEP, REMOVE, REVIEW = "KEEP", "REMOVE", "REVIEW"
# Copy lies outside the selected folders: reported, but not touched in this run.
SKIP = "SKIP"

_GENERIC_NAME = [
    re.compile(r"^[ft]\d{5,}$", re.I),                  # PhotoRec f12345678
    re.compile(r"^file\d+$", re.I),                     # FILE0001 (chkdsk)
    re.compile(r"^sector\d+$", re.I),                   # sector255116120 (sector-carved)
    re.compile(r"^_+"),                                 # _MG_1234 deleted-file markers
    re.compile(r".*\s\(\d+\)$"),                        # "IMG_1 (1)"
    re.compile(r".*[\s_-]copy(\s?\(?\d+\)?)?$", re.I),  # "IMG_1 - Copy", "IMG_1_copy2"
    re.compile(r"^copy of ", re.I),
    re.compile(r"^(recovered|restored|lost)[\s_-]", re.I),
]


@dataclass
class Member:
    row: FileRow
    action: str = KEEP
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    distance: int | None = None  # similar tier: Hamming distance to keeper
    in_scope: bool = True

    @property
    def path(self) -> str:
        return self.row.path


@dataclass
class Group:
    id: str
    tier: str  # exact | pixel | similar
    key: str
    members: list[Member]

    @property
    def keeper(self) -> Member:
        return next(m for m in self.members if m.action == KEEP)

    @property
    def removable_bytes(self) -> int:
        return sum(m.row.size for m in self.members if m.action == REMOVE)


@dataclass
class DuplicateResult:
    groups: list[Group]
    read_errors: list[str]
    scope: Scope = WHOLE_DISK

    def by_tier(self, tier: str) -> list[Group]:
        return [g for g in self.groups if g.tier == tier]


# ---------------------------------------------------------------------------
# Keeper scoring
# ---------------------------------------------------------------------------
def path_score(rel: str, cfg: Config) -> tuple[int, list[str]]:
    score, why = 0, []
    low = "/" + rel.lower()
    for pat in cfg.low_priority_patterns:
        if pat and pat in low:
            score -= 10
            why.append(f"recovery-style location ({pat})")
            break
    for pf in cfg.preferred_folders:
        if low.startswith("/" + pf.lower() + "/"):
            score += 10
            why.append(f"preferred folder ({pf})")
            break
    if low.startswith("/" + cfg.organize_destination.lower() + "/"):
        score += 5
        why.append("already organized")
    p = PurePosixPath(rel)
    if any(rx.match(p.stem) for rx in _GENERIC_NAME):
        score -= 3
        why.append("generic/recovered file name")
    if p.parent.name and is_meaningful_folder(p.parent.name, set(cfg.generic_folder_names)):
        score += 2
        why.append(f"named folder '{p.parent.name}'")
    return score, why


def _choose_keeper(members: list[Member], tier: str, cfg: Config) -> None:
    for m in members:
        m.score, m.reasons = path_score(m.path, cfg)
        if m.row.has_exif and tier in ("pixel", "similar"):
            m.reasons.append("has EXIF metadata")
            if tier == "similar":
                m.score += 8
    if tier == "exact":
        key = lambda m: (-m.score, m.row.mtime_ns, len(m.path), m.path)
    elif tier == "pixel":
        # Same pixels: the only real difference is the metadata, so the copy that
        # still has EXIF (date taken, camera, GPS) always wins; location breaks ties.
        key = lambda m: (not m.row.has_exif, -m.score, -m.row.size, len(m.path), m.path)
    else:  # similar: never throw away resolution
        key = lambda m: (-m.row.pixels, -m.score, -m.row.size, len(m.path), m.path)
    members.sort(key=key)
    for i, m in enumerate(members):
        m.action = KEEP if i == 0 else REMOVE


# ---------------------------------------------------------------------------
# Hashes, cached in the index
# ---------------------------------------------------------------------------
HASHERS = {
    "partial_hash": lambda path, row: hashing.partial_hash(path, row.size),
    "full_hash": lambda path, row: hashing.full_hash(path),
    "pixel_hash": lambda path, row: media.pixel_hash(path),
    "dhash": lambda path, row: media.dhash128(path),
}


def compute_hashes(cfg: Config, index: Index, rows: list[FileRow], column: str,
                   errors: list[str], desc: str = "") -> None:
    """Fill row.<column> for every row. Values already in the index are reused;
    missing ones are computed on cfg.workers threads and saved to the index.
    Rows that cannot be read keep None and are listed in `errors`."""
    hasher = HASHERS[column]
    todo = [r for r in rows if not getattr(r, column)]
    work = lambda r: hasher(cfg.root / r.path, r)
    for i, (row, value, exc) in enumerate(parallel_map(work, todo, cfg.workers, desc)):
        if exc is not None:
            errors.append(f"{row.path}: {type(exc).__name__}: {exc}")
            log.debug("%s failed for %s: %s", column, row.path, exc)
            continue
        setattr(row, column, value)
        index.set_hash(row.path, column, value)
        if i % 200 == 0:
            index.commit()
    index.commit()


def _buckets(rows, key) -> list[list[FileRow]]:
    """Group rows by key(row); only groups with 2+ rows. Rows whose key is None are dropped."""
    out: dict = defaultdict(list)
    for r in rows:
        k = key(r)
        if k is not None:
            out[k].append(r)
    return [lst for lst in out.values() if len(lst) > 1]


# ---------------------------------------------------------------------------
# BK-tree for Hamming-distance search
# ---------------------------------------------------------------------------
class BKTree:
    def __init__(self):
        self.root = None  # (value_int, idx, children{dist: node})

    def add(self, value: int, idx: int) -> None:
        node = [value, idx, {}]
        if self.root is None:
            self.root = node
            return
        cur = self.root
        while True:
            d = (cur[0] ^ value).bit_count()
            nxt = cur[2].get(d)
            if nxt is None:
                cur[2][d] = node
                return
            cur = nxt

    def search(self, value: int, radius: int) -> list[tuple[int, int]]:
        out, stack = [], [self.root] if self.root else []
        while stack:
            v, idx, children = stack.pop()
            d = (v ^ value).bit_count()
            if d <= radius:
                out.append((idx, d))
            for cd, child in children.items():
                if d - radius <= cd <= d + radius:
                    stack.append(child)
        return out


class _UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def _apply_scope(members: list[Member], scope: Scope) -> None:
    """Copies outside the selected folders are never proposed for removal."""
    for m in members:
        m.in_scope = scope.contains(m.path)
        if not m.in_scope and m.action != KEEP:
            m.action = SKIP


def _candidate_rows(cfg: Config, index: Index) -> list[FileRow]:
    rows = [r for r in index.all_files() if r.size >= max(1, cfg.min_size_bytes)]
    quarantine_prefix = cfg.quarantine_dir.lower() + "/"
    return [r for r in rows if not r.path.lower().startswith(quarantine_prefix)]


def exact_groups(cfg: Config, index: Index) -> list[Group]:
    """Only the byte-identical tier, whole disk (used by the `folders` overview)."""
    groups, _ = _exact_tier(cfg, index, _candidate_rows(cfg, index), [], WHOLE_DISK)
    return groups


def find_duplicates(cfg: Config, index: Index, scope: Scope = WHOLE_DISK) -> DuplicateResult:
    """Find duplicates of the files inside `scope`, searching the whole disk for copies."""
    rows = _candidate_rows(cfg, index)
    errors: list[str] = []
    groups: list[Group] = []

    representatives: list[FileRow] = rows
    if cfg.find_exact:
        exact_groups, representatives = _exact_tier(cfg, index, rows, errors, scope)
        groups += exact_groups
    if cfg.find_pixel_identical:
        pixel_groups, representatives = _pixel_tier(cfg, index, representatives, errors, scope)
        groups += pixel_groups
    if cfg.find_similar:
        groups += _similar_tier(cfg, index, representatives, errors, scope)
    index.commit()
    return DuplicateResult(groups=groups, read_errors=errors, scope=scope)


def _touches_scope(rows: list[FileRow], scope: Scope) -> bool:
    return scope.whole_disk or any(scope.contains(r.path) for r in rows)


def _exact_tier(cfg, index, rows, errors, scope):
    """Same size -> same first/last 64 KiB -> same full hash."""
    wanted = lambda lists: [r for lst in lists if _touches_scope(lst, scope) for r in lst]
    # Only sizes shared with a file in the selected folders need hashing.
    candidates = wanted(_buckets(rows, lambda r: r.size))
    compute_hashes(cfg, index, candidates, "partial_hash", errors, "Quick-hashing same-size files")
    candidates = wanted(_buckets(candidates, lambda r: (r.size, r.partial_hash) if r.partial_hash else None))
    compute_hashes(cfg, index, candidates, "full_hash", errors, "Full-hashing candidates")

    groups = []
    for lst in sorted(_buckets(candidates, lambda r: r.full_hash), key=lambda l: (-l[0].size, l[0].full_hash)):
        if not _touches_scope(lst, scope):
            continue
        members = [Member(r) for r in lst]
        _choose_keeper(members, "exact", cfg)
        _apply_scope(members, scope)
        groups.append(Group(id=f"E{len(groups) + 1:05d}", tier="exact", key=lst[0].full_hash, members=members))
    return groups, _representatives(rows, groups)


def _representatives(rows, groups):
    """What the next tier looks at: files not in any group, plus each group's keeper."""
    grouped = {m.path for g in groups for m in g.members}
    return [r for r in rows if r.path not in grouped] + [g.keeper.row for g in groups]


def _pixel_tier(cfg, index, rows, errors, scope):
    """Images of the same width x height whose decoded pixels are identical."""
    imgs = [r for r in rows if r.ext in DECODABLE_IMAGE_EXT and r.width and r.height and not r.meta_error]
    candidates = [r for lst in _buckets(imgs, lambda r: (r.width, r.height)) if _touches_scope(lst, scope) for r in lst]
    compute_hashes(cfg, index, candidates, "pixel_hash", errors, "Comparing pixels of same-size images")

    groups = []
    for lst in sorted(_buckets(candidates, lambda r: r.pixel_hash), key=lambda l: (-l[0].pixels, l[0].pixel_hash)):
        if not _touches_scope(lst, scope):
            continue
        members = [Member(r) for r in lst]
        _choose_keeper(members, "pixel", cfg)
        if cfg.pixel_identical_action == REVIEW:
            for m in members:
                if m.action == REMOVE:
                    m.action = REVIEW
        _apply_scope(members, scope)
        groups.append(Group(id=f"P{len(groups) + 1:05d}", tier="pixel", key=lst[0].pixel_hash, members=members))
    return groups, _representatives(rows, groups)


def _similar_tier(cfg, index, rows, errors, scope):
    imgs = [
        r for r in rows
        if r.ext in DECODABLE_IMAGE_EXT and not r.meta_error and (r.width or 0) >= 32 and (r.height or 0) >= 32
    ]
    if not _touches_scope(imgs, scope):
        return []
    compute_hashes(cfg, index, imgs, "dhash", errors, "Computing visual fingerprints")
    hashed: list[tuple[FileRow, int]] = []
    for r in imgs:
        if r.dhash:
            v = int(r.dhash, 16)
            # Flat / blank images (all one colour) match everything - skip them.
            if 8 <= v.bit_count() <= 120:
                hashed.append((r, v))

    tree = BKTree()
    for i, (_r, v) in enumerate(hashed):
        tree.add(v, i)
    uf = _UnionFind(len(hashed))
    for i, (r, v) in enumerate(hashed):
        if not scope.contains(r.path):
            continue  # only link images through files in the selected folders
        for j, _d in tree.search(v, cfg.similar_threshold):
            if j != i:
                uf.union(i, j)
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(hashed)):
        clusters[uf.find(i)].append(i)

    groups = []
    for _root, idxs in sorted(clusters.items(), key=lambda kv: kv[0]):
        if len(idxs) < 2:
            continue
        members = [Member(hashed[i][0]) for i in idxs]
        if not _touches_scope([m.row for m in members], scope):
            continue
        _choose_keeper(members, "similar", cfg)
        kv = int(members[0].row.dhash, 16)
        for m in members:
            m.distance = (int(m.row.dhash, 16) ^ kv).bit_count()
            if m.action == REMOVE:
                # Union-find can chain A~B~C; only members close to the keeper
                # itself may get the configured action, the rest are REVIEW.
                if cfg.similar_action == REVIEW or m.distance > cfg.similar_threshold:
                    m.action = REVIEW
        _apply_scope(members, scope)
        groups.append(Group(id=f"S{len(groups) + 1:05d}", tier="similar", key=members[0].row.dhash, members=members))
    return groups
