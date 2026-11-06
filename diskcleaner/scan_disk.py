"""Scan the disk and keep the index up to date.
The scan reads only new or changed files. You can stop it and start it again."""

import os
import stat as statmod
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import media_info
from .config import Config
from .file_types import DECODABLE_IMAGE_EXT, MP4_FAMILY_EXT, SNIFF_EXT, category_for, sniff_ext
from .file_index import Index
from .helpers import log, parallel_map, progress


@dataclass
class ScanResult:
    files_seen: int = 0
    new_or_changed: int = 0
    removed_from_index: int = 0
    skipped_small: int = 0
    zero_byte: int = 0
    metadata_read: int = 0
    metadata_errors: int = 0
    not_listed: int = 0  # files that a bad disk driver did not show in the folder list
    walk_errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _excluded_dir(name: str, rel: str, cfg: Config, excl_names: set[str], excl_rel: set[str]) -> bool:
    if cfg.skip_hidden and name.startswith("."):
        return True
    if name.lower() in excl_names:
        return True
    return rel.lower() in excl_rel


def _list_files(cfg: Config, errors: list[str]) -> list[str]:
    """Give the relative path of each file in the folders to scan. (This does not read sizes yet.)"""
    root = cfg.root
    excl_names = {e.lower() for e in cfg.exclude}
    excl_rel = {e.lower().strip("/") for e in cfg.always_excluded_relpaths()}
    starts = [root / inc for inc in cfg.include] if cfg.include else [root]

    def onerror(err: OSError):
        errors.append(f"{err.filename}: {err.strerror}")
        log.warning("Cannot read %s: %s", err.filename, err.strerror)

    out: list[str] = []
    bar = progress(desc="Listing folders", unit="file")
    for start in starts:
        if not start.is_dir():
            errors.append(f"{start}: include folder not found")
            continue
        for dirpath, dirnames, filenames in os.walk(start, onerror=onerror, followlinks=cfg.follow_symlinks):
            dp = Path(dirpath)
            rel_dir = "" if dp == root else dp.relative_to(root).as_posix()
            dirnames[:] = sorted(d for d in dirnames if not _excluded_dir(
                d, f"{rel_dir}/{d}" if rel_dir else d, cfg, excl_names, excl_rel))
            for fn in sorted(filenames):
                if cfg.skip_hidden and fn.startswith("."):
                    continue  # this also skips the macOS '._' files
                if fn.lower() not in excl_names:
                    out.append(f"{rel_dir}/{fn}" if rel_dir else fn)
            bar.update(len(filenames))
    bar.close()
    return out


def iter_files(cfg: Config, errors: list[str]):
    """Give (relative path, os.stat_result) for each normal file, in path order.
    The stat calls use cfg.workers threads, because each call waits for the disk."""
    stat = os.stat if cfg.follow_symlinks else os.lstat

    def read_stat(rel):
        return stat(cfg.root / rel)

    for rel, st, exc in parallel_map(read_stat, _list_files(cfg, errors), cfg.workers, "Scanning files"):
        if exc is not None:
            errors.append(f"{cfg.root / rel}: {getattr(exc, 'strerror', exc)}")
        elif statmod.S_ISREG(st.st_mode):
            yield rel, st


def same_disk(cfg: Config, index: Index, sample: int = 50) -> bool:
    """Find out if the indexed disk is now at a different mount path.
    The tool looks at 50 indexed files from all parts of the list. If 90% or more of them
    are at the new root with the same size and modified time, it is the same disk.
    Then the saved hashes are still correct. (The scan also checks the size and time of each file.)"""
    rows = index.conn.execute("SELECT path, size, mtime_ns FROM files ORDER BY path").fetchall()
    if not rows:
        return False
    picked = rows[:: max(1, len(rows) // sample)][:sample]
    ok = 0
    for r in picked:
        try:
            st = os.lstat(cfg.root / r["path"])
        except OSError:
            continue
        if st.st_size == r["size"] and st.st_mtime_ns == r["mtime_ns"]:
            ok += 1
    return ok >= 0.9 * len(picked)


def _keep_unlisted_files(cfg: Config, index: Index, scan_id: int) -> int:
    """Look for each file that the folder list did not show, by its name.
    Some NTFS drivers for macOS do not show some files in folder lists, but the files are there.
    The index must keep these files."""
    missing = [(r["path"], r["ext"], r["category"]) for r in index.conn.execute(
        "SELECT path, ext, category FROM files WHERE scan_id<>?", (scan_id,))]
    kept = 0
    for path, ext, category in missing:
        try:
            st = os.lstat(cfg.root / path)
        except OSError:
            continue  # the file is really gone
        if statmod.S_ISREG(st.st_mode) and st.st_size >= cfg.min_size_bytes:
            index.upsert_stat(path, st.st_size, st.st_mtime_ns, ext, category, scan_id)
            kept += 1
    if kept:
        log.warning("%d files were missing from folder listings but are still on the disk - kept "
                    "(a known quirk of some NTFS drivers).", kept)
    return kept


def scan(cfg: Config, index: Index) -> ScanResult:
    t0 = time.time()
    res = ScanResult()
    scan_id = int(index.get_meta("scan_id", "0")) + 1
    prev_root = index.get_meta("root")
    if prev_root and prev_root != str(cfg.root):
        if same_disk(cfg, index):
            log.warning("Same disk, new location (%s -> %s); keeping what was already checked.", prev_root, cfg.root)
        else:
            # This is a different disk. Do not use the saved hashes.
            log.warning("Disk root changed (%s -> %s); rebuilding the index from scratch.", prev_root, cfg.root)
            index.conn.execute("DELETE FROM files")
            index.commit()

    # ---- part 1: list the files and read their sizes and times ----------
    for rel, st in iter_files(cfg, res.walk_errors):
        res.files_seen += 1
        if st.st_size == 0:
            res.zero_byte += 1
        if st.st_size < cfg.min_size_bytes:
            res.skipped_small += 1
            continue
        ext = os.path.splitext(rel)[1].lower()
        if ext in SNIFF_EXT:
            try:
                with open(cfg.root / rel, "rb") as fh:
                    ext = sniff_ext(fh.read(16)) or ext
            except OSError:
                pass
        if index.upsert_stat(rel, st.st_size, st.st_mtime_ns, ext, category_for(ext), scan_id):
            res.new_or_changed += 1
        if res.files_seen % 2000 == 0:
            index.commit()
    res.not_listed = _keep_unlisted_files(cfg, index, scan_id)
    res.removed_from_index = index.delete_not_seen(scan_id)
    index.set_meta("scan_id", str(scan_id))
    index.set_meta("root", str(cfg.root))
    index.commit()

    # ---- part 2: read the metadata of new or changed photos and videos ---
    media_ext = sorted(DECODABLE_IMAGE_EXT | MP4_FAMILY_EXT)
    marks = ",".join("?" * len(media_ext))
    # Other files have no metadata to read. Mark them all as done in one step.
    index.conn.execute(f"UPDATE files SET meta_done=1 WHERE meta_done=0 AND ext NOT IN ({marks})", media_ext)
    todo = [(r["path"], r["ext"]) for r in index.conn.execute(
        "SELECT path, ext FROM files WHERE meta_done=0 ORDER BY path")]

    def read_metadata(item):
        rel, ext = item
        return media_info.extract_metadata(cfg.root / rel, ext)

    for i, ((rel, _ext), md, exc) in enumerate(
            parallel_map(read_metadata, todo, cfg.workers, "Reading photo/video metadata")):
        if exc is None:
            res.metadata_read += 1
            index.set_metadata(
                rel, width=md["width"], height=md["height"],
                meta_date=md["date"].isoformat() if md["date"] else None,
                meta_date_source=md["date_source"], has_exif=md["has_exif"], error=None,
            )
        else:  # many recovered files are damaged
            res.metadata_errors += 1
            index.set_metadata(rel, width=None, height=None, meta_date=None, meta_date_source=None,
                               has_exif=False, error=f"{type(exc).__name__}: {exc}"[:300])
            log.debug("metadata failed for %s: %s", rel, exc)
        if i % 500 == 0:
            index.commit()
    index.set_meta("last_scan", time.strftime("%Y-%m-%d %H:%M:%S"))
    index.commit()
    res.seconds = time.time() - t0
    return res
