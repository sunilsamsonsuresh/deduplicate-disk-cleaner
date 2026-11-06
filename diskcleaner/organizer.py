"""Organize photos/videos into  <dest>/<Category>/<Year>/<Year-Month [Event]>/ ."""

from __future__ import annotations

import csv
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import hashing
from .config import Config
from .dates import clean_event_name, event_name_for, resolve_date
from .filetypes import CATEGORY_LABEL, SNIFF_EXT
from .index import Index
from .scope import WHOLE_DISK, Scope
from .safeops import Manifest, SafetyError, resolve_inside, safe_copy, safe_move, unique_path
from .utils import log, progress

PLAN_FIELDS = ["src", "dst", "category", "date", "date_source", "event"]
JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}


@dataclass
class PlanItem:
    src: str
    dst: str
    category: str
    date: str
    date_source: str
    event: str


def _safe_component(s: str) -> str:
    s = "".join(ch for ch in s if ch not in '\\/:*?"<>|' and ord(ch) >= 32).strip(" .")
    return s[:80] or "Misc"


def _date_folders(layout: str, category: str, date, event: str | None) -> str:
    """'2015/2015-06 Goa Trip' (layout filled in), or 'Unknown Date/<event>' without a date."""
    if not date.known:
        return f"Unknown Date/{_safe_component(event or 'Unsorted')}"
    if date.month:
        month_dir = f"{date.year:04d}-{date.month:02d}" + (f" {event}" if event else "")
    else:
        month_dir = event or "Unknown Month"
    sub = layout.format(category=category, year=f"{date.year:04d}", month_dir=_safe_component(month_dir))
    return "/".join(p for p in sub.split("/") if p)


def target_dir(cfg: Config, rel_path: str, category: str, date, event: str | None) -> str:
    """Folder in the uniform tree: '_Organized/Photos/2015/2015-06 Goa Trip'."""
    label = CATEGORY_LABEL.get(category, category.title())
    if not date.known:
        return f"{cfg.organize_destination}/{label}/{_date_folders('', label, date, event)}"
    return f"{cfg.organize_destination}/{_date_folders(cfg.organize_layout, label, date, event)}"


def inside_roots(cfg: Config) -> list[str]:
    """Expand [organize].organize_inside into concrete folders (relative to root).
    'Existing Photos/*' -> every sub-folder of 'Existing Photos' separately."""
    excl = {e.lower() for e in cfg.exclude}
    out: list[str] = []
    for entry in cfg.organize_inside:
        entry = entry.strip().strip("/")
        if entry.endswith("/*"):
            base = cfg.root / entry[:-2]
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in excl:
                        out.append(d.relative_to(cfg.root).as_posix())
        elif (cfg.root / entry).is_dir():
            out.append(entry)
    # deepest first, so a nested entry wins over its parent
    return sorted(set(out), key=lambda r: (-r.count("/"), r))


def _root_for(path: str, roots: list[str]) -> str | None:
    low = path.lower()
    for r in roots:
        if low.startswith(r.lower() + "/"):
            return r
    return None


def target_dir_inside(cfg: Config, root: str, date, event: str | None) -> str:
    """Date folders inside an already-organized folder: '<root>/2019/2019-06 Birthday'."""
    if not date.known:
        return f"{root}/Unknown Date" + (f"/{_safe_component(event)}" if event else "")
    return f"{root}/{_date_folders(cfg.organize_layout_inside, '', date, event)}"


def build_plan(cfg: Config, index: Index, scope: Scope = WHOLE_DISK) -> list[PlanItem]:
    dest_prefix = cfg.organize_destination.lower() + "/"
    q_prefix = cfg.quarantine_dir.lower() + "/"
    extra_generic = set(cfg.generic_folder_names)
    roots = inside_roots(cfg)
    reserved: set[str] = set()
    items: list[PlanItem] = []
    for row in index.all_files():
        low = row.path.lower()
        if row.category not in cfg.organize_categories or not scope.contains(row.path):
            continue
        if low.startswith(dest_prefix) or low.startswith(q_prefix):
            continue
        root = _root_for(row.path, roots)
        date = resolve_date(
            rel_path=row.path, meta_date=row.meta_date, meta_source=row.meta_date_source,
            mtime_ns=row.mtime_ns, min_year=cfg.min_year, use_mtime=cfg.use_file_mtime,
            ignore_mtime_after=cfg.ignore_mtime_after, ignore_mtime_dates=cfg.ignore_mtime_dates,
        )
        parent = str(PurePosixPath(row.path).parent)
        if root:
            # only folders *below* the organized folder can be event names
            parent = parent[len(root) + 1:] if len(parent) > len(root) else ""
        event = event_name_for(parent, extra_generic) if (cfg.keep_event_names and parent) else None
        if event:
            event = clean_event_name(event) or None
        if root:
            dst_dir = target_dir_inside(cfg, root, date, event)
        else:
            dst_dir = target_dir(cfg, row.path, row.category, date, event)
        pp = PurePosixPath(row.path)
        name = pp.name
        if pp.suffix.lower() in SNIFF_EXT and pp.suffix.lower() != row.ext:
            # recovered without a usable extension: give it the real one so it opens
            name = pp.stem + row.ext if pp.suffix else pp.name + row.ext
        if f"{dst_dir}/{name}".lower() == low:
            continue  # already in the right place
        dst_full = unique_path(cfg.root / dst_dir / name, reserved)
        reserved.add(str(dst_full).lower())
        items.append(PlanItem(
            src=row.path,
            dst=dst_full.relative_to(cfg.root).as_posix(),
            category=row.category,
            date=date.iso(),
            date_source=date.source,
            event=event or "",
        ))
    return items


def write_plan_csv(items: list[PlanItem], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        w.writeheader()
        for it in items:
            w.writerow(it.__dict__)


def read_plan_csv(path: Path) -> list[PlanItem]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"src", "dst"} - set(reader.fieldnames or [])
        if missing:
            raise SafetyError(f"{path.name} is missing columns {sorted(missing)}")
        return [PlanItem(**{k: (r.get(k) or "").strip() for k in PLAN_FIELDS}) for r in reader]


def plan_summary(items: list[PlanItem]) -> dict:
    return {
        "files": len(items),
        "by_source": Counter(i.date_source for i in items),
        "by_year": Counter((i.date[:4] or "Unknown") for i in items),
        "by_category": Counter(i.category for i in items),
        "with_event": sum(1 for i in items if i.event),
    }


@dataclass
class OrganizeResult:
    done: int = 0
    identical_skipped: int = 0
    skipped: list[str] = field(default_factory=list)
    manifest: Path | None = None


def apply_plan(cfg: Config, items: list[PlanItem], index: Index | None = None, mode: str | None = None,
               plan_name: str = "") -> OrganizeResult:
    mode = mode or cfg.organize_mode
    res = OrganizeResult()
    dest_prefix = cfg.organize_destination.lower() + "/"
    roots = inside_roots(cfg)
    with Manifest(cfg, f"organize-{mode}", {"plan": plan_name}) as man:
        res.manifest = man.path
        for it in progress(items, total=len(items), desc=f"Organizing ({mode})"):
            try:
                dst_root = _root_for(it.dst, roots)
                if dst_root:
                    # never move a file from one organized folder into another
                    if _root_for(it.src, roots) != dst_root:
                        res.skipped.append(f"{it.src}: would move into a different folder ({dst_root}) - refused")
                        continue
                elif not it.dst.lower().startswith(dest_prefix):
                    res.skipped.append(f"{it.src}: destination outside '{cfg.organize_destination}' refused")
                    continue
                src = resolve_inside(cfg.root, it.src)
                dst = resolve_inside(cfg.root, it.dst)
                if not src.is_file():
                    res.skipped.append(f"{it.src}: no longer exists")
                    continue
                if dst.exists():
                    if dst.stat().st_size == src.stat().st_size and hashing.full_hash(dst) == hashing.full_hash(src):
                        res.identical_skipped += 1
                        res.skipped.append(f"{it.src}: identical file already at {it.dst} (a duplicate - left in place)")
                        continue
                    dst = unique_path(dst)
                dst_rel = dst.relative_to(cfg.root).as_posix()
                man.planned(mode, it.src, dst_rel)
                if mode == "move":
                    safe_move(src, dst)
                else:
                    safe_copy(src, dst)
                man.done(mode, it.src, dst_rel)
                res.done += 1
                if index is not None and mode == "move":
                    index.rename(it.src, dst_rel)
            except Exception as exc:
                res.skipped.append(f"{it.src}: {type(exc).__name__}: {exc}")
                log.warning("organize skipped %s: %s", it.src, exc)
        man.note(done=res.done, skipped=len(res.skipped))
    if index is not None:
        index.commit()
    return res


# ---------------------------------------------------------------------------
# Empty folder clean-up
# ---------------------------------------------------------------------------
def find_empty_dirs(cfg: Config) -> list[Path]:
    """Folders that contain nothing (or only .DS_Store/Thumbs.db/desktop.ini), deepest first.
    A folder whose sub-folders are all empty counts as empty too."""
    skip = {cfg.quarantine_path.resolve(), cfg.state_dir.resolve()}
    excl_names = {e.lower() for e in cfg.exclude}
    empty: set[Path] = set()
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(cfg.root, topdown=False):
        d = Path(dirpath)
        if d == cfg.root:
            continue
        parts = d.relative_to(cfg.root).parts
        if any(p.startswith(".") or p.lower() in excl_names for p in parts):
            continue  # system / hidden folders are left alone
        rd = d.resolve()
        if rd in skip or any(s in rd.parents for s in skip):
            continue
        if any(f.lower() not in JUNK_FILES for f in filenames):
            continue
        if all((d / sub) in empty for sub in dirnames):
            empty.add(d)
            out.append(d)
    return out


def remove_empty_dirs(dirs: list[Path]) -> int:
    n = 0
    for d in dirs:  # deepest first
        try:
            for f in d.iterdir():
                if f.is_file() and f.name.lower() in JUNK_FILES:
                    f.unlink()
            d.rmdir()  # fails safely if anything is still inside
            n += 1
        except OSError as exc:
            log.warning("could not remove folder %s: %s", d, exc)
    return n
