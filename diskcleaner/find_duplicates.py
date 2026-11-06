"""Find duplicate files in three levels ("tiers"), and choose the copy to keep.

Tier 1  exact    the bytes are the same (size -> quick hash -> full BLAKE2b hash)
Tier 2  pixel    images with the same pixels (only the metadata is different)
Tier 3  similar  images that look the same (look-alike hash + BK-tree). Default: REVIEW

Each tier looks only at the kept copies ("keepers") from the tier before it.
Thus two tiers never both propose to remove the same file.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import file_hashes, media_info
from .config import Config
from .dates import is_meaningful_folder
from .file_types import DECODABLE_IMAGE_EXT
from .file_index import FileRow, Index
from .selected_folders import WHOLE_DISK, Scope
from .helpers import log, parallel_map

KEEP, REMOVE, REVIEW = "KEEP", "REMOVE", "REVIEW"
# The copy is outside the selected folders. The report shows it, but this run does not touch it.
SKIP = "SKIP"

_GENERIC_NAME = [
    re.compile(r"^[ft]\d{5,}$", re.I),                  # PhotoRec f12345678
    re.compile(r"^file\d+$", re.I),                     # FILE0001 (chkdsk)
    re.compile(r"^sector\d+$", re.I),                   # sector255116120 (recovered from disk sectors)
    re.compile(r"^_+"),                                 # _MG_1234 (marks a deleted file)
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
    distance: int | None = None  # "similar" tier only: the number of different bits from the keeper
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
# Choose the copy to keep
# ---------------------------------------------------------------------------
def path_score(rel: str, cfg: Config) -> tuple[int, list[str]]:
    """Give points to a path. A higher score means "a better copy to keep". Also give the reasons."""
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


def _order_exact(m: Member):
    # best score first, then the oldest file, then the shortest path
    return (-m.score, m.row.mtime_ns, len(m.path), m.path)


def _order_pixel(m: Member):
    # Same pixels: only the metadata is different. Thus the copy with EXIF
    # (date taken, camera, GPS) always wins. The score decides between copies with EXIF.
    return (not m.row.has_exif, -m.score, -m.row.size, len(m.path), m.path)


def _order_similar(m: Member):
    # Look-alike images: always keep the biggest picture
    return (-m.row.pixels, -m.score, -m.row.size, len(m.path), m.path)


def _choose_keeper(members: list[Member], tier: str, cfg: Config) -> None:
    """Sort the members, best copy first. Mark the first one KEEP and all others REMOVE."""
    for m in members:
        m.score, m.reasons = path_score(m.path, cfg)
        if m.row.has_exif and tier in ("pixel", "similar"):
            m.reasons.append("has EXIF metadata")
            if tier == "similar":
                m.score += 8
    if tier == "exact":
        members.sort(key=_order_exact)
    elif tier == "pixel":
        members.sort(key=_order_pixel)
    else:
        members.sort(key=_order_similar)
    for i, m in enumerate(members):
        m.action = KEEP if i == 0 else REMOVE


# ---------------------------------------------------------------------------
# Hashes (the index keeps them)
# ---------------------------------------------------------------------------
def make_hash(column: str, path, row: FileRow) -> str:
    """Calculate one hash of a file. `column` is the name of the hash in the index."""
    if column == "partial_hash":
        return file_hashes.partial_hash(path, row.size)
    if column == "full_hash":
        return file_hashes.full_hash(path)
    if column == "pixel_hash":
        return media_info.pixel_hash(path)
    if column == "dhash":
        return media_info.dhash128(path)
    raise ValueError(f"unknown hash: {column}")


def compute_hashes(cfg: Config, index: Index, rows: list[FileRow], column: str,
                   errors: list[str], desc: str = "") -> None:
    """Fill row.<column> for each row. Use the value from the index if it is there.
    Calculate the missing values on cfg.workers threads and save them in the index.
    If the tool cannot read a file, the value stays None and the error goes into `errors`."""
    todo = [r for r in rows if not getattr(r, column)]

    def work(row):
        return make_hash(column, cfg.root / row.path, row)

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
    """Put the rows in groups by key(row). Give only groups with 2 or more rows. Ignore rows with key None."""
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
    """A search tree for hashes. It quickly finds all hashes with few different bits.
    Each node is [hash as a number, row number, {distance: child node}]."""

    def __init__(self):
        self.root = None

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
    """Joins items into groups. If A is like B and B is like C, then A, B and C are one group."""

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
# Find the duplicates
# ---------------------------------------------------------------------------
def _apply_scope(members: list[Member], scope: Scope) -> None:
    """The tool never proposes to remove a copy outside the selected folders."""
    for m in members:
        m.in_scope = scope.contains(m.path)
        if not m.in_scope and m.action != KEEP:
            m.action = SKIP


def _candidate_rows(cfg: Config, index: Index) -> list[FileRow]:
    rows = [r for r in index.all_files() if r.size >= max(1, cfg.min_size_bytes)]
    quarantine_prefix = cfg.quarantine_dir.lower() + "/"
    return [r for r in rows if not r.path.lower().startswith(quarantine_prefix)]


def exact_groups(cfg: Config, index: Index) -> list[Group]:
    """Find only the exact copies on the whole disk. (The `folders` command uses this.)"""
    groups, _ = _exact_tier(cfg, index, _candidate_rows(cfg, index), [], WHOLE_DISK)
    return groups


def find_duplicates(cfg: Config, index: Index, scope: Scope = WHOLE_DISK) -> DuplicateResult:
    """Find duplicates of the files in `scope`. Look for copies on the whole disk."""
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
    """True if one or more of the rows are in the selected folders."""
    return scope.whole_disk or any(scope.contains(r.path) for r in rows)


def _exact_tier(cfg, index, rows, errors, scope):
    """Find exact copies: the same size, then the same quick hash, then the same full hash."""

    def wanted(row_groups):
        # all rows of the groups that touch the selected folders
        out = []
        for group_rows in row_groups:
            if _touches_scope(group_rows, scope):
                out.extend(group_rows)
        return out

    # Hash only the sizes that a file in the selected folders also has.
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
    """Give the files for the next tier: the files in no group, plus the keeper of each group."""
    grouped = {m.path for g in groups for m in g.members}
    return [r for r in rows if r.path not in grouped] + [g.keeper.row for g in groups]


def _pixel_tier(cfg, index, rows, errors, scope):
    """Find images with the same width, the same height and the same pixels."""
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
    """Find images that look the same (look-alike hashes with few different bits)."""
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
            # Blank images (one colour only) match all other images. Skip them.
            if 8 <= v.bit_count() <= 120:
                hashed.append((r, v))

    tree = BKTree()
    for i, (_r, v) in enumerate(hashed):
        tree.add(v, i)
    uf = _UnionFind(len(hashed))
    for i, (r, v) in enumerate(hashed):
        if not scope.contains(r.path):
            continue  # join images only through files in the selected folders
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
                # The groups can make a chain (A is like B, B is like C). Only copies near
                # the keeper can get the configured action. All other copies get REVIEW.
                if cfg.similar_action == REVIEW or m.distance > cfg.similar_threshold:
                    m.action = REVIEW
        _apply_scope(members, scope)
        groups.append(Group(id=f"S{len(groups) + 1:05d}", tier="similar", key=members[0].row.dhash, members=members))
    return groups
