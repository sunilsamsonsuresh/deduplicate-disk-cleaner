"""Walk the disk and keep the SQLite index up to date (incremental & resumable)."""

from __future__ import annotations

import os
import stat as statmod
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import media
from .config import Config
from .filetypes import DECODABLE_IMAGE_EXT, MP4_FAMILY_EXT, SNIFF_EXT, category_for, sniff_ext
from .index import Index
from .utils import log, parallel_map, progress


@dataclass
class ScanResult:
    files_seen: int = 0
    new_or_changed: int = 0
    removed_from_index: int = 0
    skipped_small: int = 0
    zero_byte: int = 0
    metadata_read: int = 0
    metadata_errors: int = 0
    not_listed: int = 0  # files a buggy driver left out of folder listings
    walk_errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _excluded_dir(name: str, rel: str, cfg: Config, excl_names: set[str], excl_rel: set[str]) -> bool:
    if cfg.skip_hidden and name.startswith("."):
        return True
    if name.lower() in excl_names:
        return True
    return rel.lower() in excl_rel


def _list_files(cfg: Config, errors: list[str]) -> list[str]:
    """Relative paths of every file under the configured roots (no stat yet)."""
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
                    continue  # includes macOS '._' AppleDouble files
                if fn.lower() not in excl_names:
                    out.append(f"{rel_dir}/{fn}" if rel_dir else fn)
            bar.update(len(filenames))
    bar.close()
    return out


def iter_files(cfg: Config, errors: list[str]):
    """Yield (relpath, os.stat_result) for every regular file, in path order.
    The stat calls run on cfg.workers threads (each one is a round trip to the disk)."""
    stat = os.stat if cfg.follow_symlinks else os.lstat
    for rel, st, exc in parallel_map(lambda r: stat(cfg.root / r), _list_files(cfg, errors),
                                     cfg.workers, "Scanning files"):
        if exc is not None:
            errors.append(f"{cfg.root / rel}: {getattr(exc, 'strerror', exc)}")
        elif statmod.S_ISREG(st.st_mode):
            yield rel, st


def same_disk(cfg: Config, index: Index, sample: int = 50) -> bool:
    """Is the indexed disk now simply mounted somewhere else? True if (nearly) all
    of an even sample of indexed files are at the new root with the same size and
    modified time. Then the cached hashes are still valid (each file is re-checked
    by size and time during the scan anyway)."""
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
        ok += st.st_size == r["size"] and st.st_mtime_ns == r["mtime_ns"]
    return ok >= 0.9 * len(picked)


def _keep_unlisted_files(cfg: Config, index: Index, scan_id: int) -> int:
    """Before forgetting files that were not listed, look for each one by name.
    Some NTFS drivers for macOS leave the odd file out of folder listings although
    the file is there; such files must not be dropped from the index."""
    missing = [(r["path"], r["ext"], r["category"]) for r in index.conn.execute(
        "SELECT path, ext, category FROM files WHERE scan_id<>?", (scan_id,))]
    kept = 0
    for path, ext, category in missing:
        try:
            st = os.lstat(cfg.root / path)
        except OSError:
            continue  # really gone
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
            # A different disk: cached hashes must not be trusted.
            log.warning("Disk root changed (%s -> %s); rebuilding the index from scratch.", prev_root, cfg.root)
            index.conn.execute("DELETE FROM files")
            index.commit()

    # ---- phase 1: list + stat --------------------------------------------
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

    # ---- phase 2: metadata for new/changed photos & videos (in parallel) --
    media_ext = sorted(DECODABLE_IMAGE_EXT | MP4_FAMILY_EXT)
    marks = ",".join("?" * len(media_ext))
    # Non-media files need no metadata - mark them done in one statement.
    index.conn.execute(f"UPDATE files SET meta_done=1 WHERE meta_done=0 AND ext NOT IN ({marks})", media_ext)
    todo = [(r["path"], r["ext"]) for r in index.conn.execute(
        "SELECT path, ext FROM files WHERE meta_done=0 ORDER BY path")]
    read = lambda item: media.extract_metadata(cfg.root / item[0], item[1])
    for i, ((rel, _ext), md, exc) in enumerate(
            parallel_map(read, todo, cfg.workers, "Reading photo/video metadata")):
        if exc is None:
            res.metadata_read += 1
            index.set_metadata(
                rel, width=md["width"], height=md["height"],
                meta_date=md["date"].isoformat() if md["date"] else None,
                meta_date_source=md["date_source"], has_exif=md["has_exif"], error=None,
            )
        else:  # corrupted / partially recovered files are common
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
