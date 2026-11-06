"""The commands that you type.

    python -m diskcleaner [-c config.toml] <command> [options]

A command that changes files first only shows what it will do (a "dry run").
To make the change, add --apply. Then the command asks you to type a word to confirm.
"""

import argparse
import datetime as dt
import json
import shutil
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

from . import __version__, organize, reports, helpers
from .config import Config, ConfigError, load_config
from .remove_duplicates import apply_plan as dedupe_apply
from .remove_duplicates import load_plan as dedupe_load
from .remove_duplicates import quarantine_list, read_path_list
from .find_duplicates import compute_hashes, exact_groups, find_duplicates
from .file_types import DECODABLE_IMAGE_EXT
from .file_index import FileRow, Index
from .selected_folders import WHOLE_DISK, Scope, resolve_scope
from .safe_moves import SafetyError, check_writable
from .undo_log import list_manifests, read_manifest, undo
from .scan_disk import scan
from .helpers import human_size, timestamp

PROJECT_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def confirm(prompt: str, word: str, assume_yes: bool = False) -> bool:
    """Ask the user to type `word`. Give True only if they type it."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"Refusing: confirmation needed but no terminal. Re-run interactively (or pass --yes).")
        return False
    try:
        answer = input(f"{prompt}\nType {word} to continue (anything else cancels): ")
    except EOFError:
        return False
    return answer.strip() == word


def _latest(directory: Path, pattern: str) -> Path | None:
    """Give the newest file that matches pattern, or None."""
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def _print_scan(res) -> None:
    """Print the result of a scan."""
    print(f"Scanned {res.files_seen:,} files in {res.seconds:.0f}s "
          f"({res.new_or_changed:,} new/changed, {res.removed_from_index:,} gone since last scan).")
    if res.metadata_read or res.metadata_errors:
        print(f"Read metadata of {res.metadata_read:,} photos/videos; "
              f"{res.metadata_errors:,} could not be read (possibly corrupted by the recovery).")
    if res.not_listed:
        print(f"{res.not_listed:,} files were left out of folder listings by the disk driver but are "
              "still there - kept in the index.")
    if res.zero_byte:
        print(f"{res.zero_byte:,} empty (0-byte) files found - these are ignored.")
    if res.walk_errors:
        print(f"{len(res.walk_errors):,} files/folders could not be read (see log).")
        for e in res.walk_errors:
            helpers.log.debug("walk error: %s", e)


def _do_scan(cfg: Config, index: Index) -> None:
    print(f"Scanning {cfg.root} ...")
    _print_scan(scan(cfg, index))


def _resolve_manifest(cfg: Config, name: str) -> Path:
    """Find an undo log from a name, a path or 'last'."""
    if name == "last":
        ms = [m for m in list_manifests(cfg) if not m.stem.split("_", 2)[-1].startswith("undo")]
        if not ms:
            raise SafetyError("No manifests found.")
        return ms[-1]
    p = Path(name)
    if p.exists():
        return p
    cand = cfg.manifests_dir / name
    if cand.exists():
        return cand
    cand = cfg.manifests_dir / f"{name}.jsonl"
    if cand.exists():
        return cand
    raise SafetyError(f"Manifest not found: {name}  (run `undo` with no arguments to list them)")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists():
        print(f"{target} already exists - edit it directly.")
        return 1
    text = (PROJECT_DIR / "config.example.toml").read_text(encoding="utf-8")
    if args.root:
        text = text.replace('root = "/Volumes/CHANGE_ME"', f'root = "{args.root}"')
    target.write_text(text, encoding="utf-8")
    print(f"Created {target}. Edit [disk].root (currently {'set' if args.root else 'NOT set'}) and review the options.")
    return 0


def cmd_scan(cfg: Config, args) -> int:
    with Index(cfg.db_path) as index:
        _do_scan(cfg, index)
    return 0


def cmd_status(cfg: Config, args) -> int:
    with Index(cfg.db_path) as index:
        rows = index.all_files()
        if not rows:
            print("Index is empty - run `scan` first.")
            return 0
        print(f"Disk root : {cfg.root}")
        print(f"Last scan : {index.get_meta('last_scan', 'never')}")
        print(f"Files     : {len(rows):,}  ({human_size(sum(r.size for r in rows))})\n")
        cats = Counter(r.category for r in rows)
        sizes = Counter()
        for r in rows:
            sizes[r.category] += r.size
        print(f"{'Category':<12}{'Files':>10}{'Size':>12}")
        for c, n in cats.most_common():
            print(f"{c:<12}{n:>10,}{human_size(sizes[c]):>12}")
        media_rows = [r for r in rows if r.category in ("photos", "videos")]
        with_date = sum(1 for r in media_rows if r.meta_date)
        errs = sum(1 for r in rows if r.meta_error)
        print(f"\nPhotos/videos with an embedded date (EXIF/video): {with_date:,} of {len(media_rows):,}")
        print(f"Photos/videos whose metadata could not be read: {errs:,}")
        days = Counter(dt.datetime.fromtimestamp(r.mtime_ns / 1e9).date() for r in media_rows)
        if days:
            print("\nMost common 'modified' dates of photos/videos. If one day holds a big share,")
            print("it is probably the day you ran the recovery: set [organize].ignore_mtime_after to it.")
            for d, n in days.most_common(8):
                print(f"  {d}  {n:>8,} files  ({n * 100 / len(media_rows):.0f}%)")
        q = cfg.quarantine_path
        if q.exists():
            runs = sorted(p for p in q.iterdir() if p.is_dir())
            if runs:
                total = sum(f.stat().st_size for r in runs for f in r.rglob("*") if f.is_file())
                print(f"\nQuarantine: {len(runs)} run(s), {human_size(total)} at {q}")
    return 0


def _scope_from_args(cfg: Config, args) -> Scope:
    """Use --folder from the command line. If there is no --folder, use [scope].folders from the config."""
    folders = getattr(args, "folder", None) or cfg.scope_folders
    return resolve_scope(cfg, folders)


def cmd_duplicates(cfg: Config, args) -> int:
    cfg.ensure_state_dirs()
    scope = _scope_from_args(cfg, args)
    with Index(cfg.db_path) as index:
        if not args.no_rescan:
            _do_scan(cfg, index)
        print(f"Looking for duplicates of files in: {scope.label()}"
              + ("" if scope.whole_disk else "  (searching the whole disk for copies)"))
        result = find_duplicates(cfg, index, scope)
    ts = timestamp()
    csv_path = cfg.reports_dir / f"duplicates_{ts}.csv"
    html_path = cfg.reports_dir / f"duplicates_{ts}.html"
    json_path = cfg.reports_dir / f"duplicates_{ts}.json"
    reports.write_duplicates_csv(result, csv_path)
    reports.write_duplicates_html(result, cfg, html_path, csv_path)
    reports.write_duplicates_json(result, cfg, json_path, csv_path)
    for e in result.read_errors:
        helpers.log.debug("read error: %s", e)
    reports.print_summary(result)
    bad = reports.unreadable_paths(result)
    if bad:
        bad_path = cfg.reports_dir / f"unreadable_{ts}.txt"
        bad_path.write_text("\n".join(bad) + "\n", encoding="utf-8")
        print(f"List of unreadable files (often only partially recovered): {bad_path}")
    print(f"\nWhere each file's other copies are (JSON, for review):\n  {json_path}")
    print(f"Visual report (open in a browser, with thumbnails):\n  {html_path}")
    print(f"Editable plan (change 'action' to KEEP / REMOVE / REVIEW / SKIP):\n  {csv_path}")
    print("\nNothing has been changed. Next step:\n  python -m diskcleaner dedupe            (dry run)\n"
          "  python -m diskcleaner dedupe --apply    (moves duplicates to quarantine)")
    return 0


def _report_scope(cfg: Config, csv_path: Path) -> Scope | None:
    """Give the folders that a duplicates report was made for (from its JSON file)."""
    j = csv_path.with_suffix(".json")
    if not j.exists():
        return None
    try:
        folders = json.loads(j.read_text(encoding="utf-8")).get("selected_folders", [])
    except (OSError, ValueError):
        return None
    folders = [f for f in folders if f != "(whole disk)"]
    try:
        return resolve_scope(cfg, folders)
    except ConfigError:
        return None  # a folder was moved or renamed after the report. Use the CSV as it is.


def cmd_dedupe(cfg: Config, args) -> int:
    csv_path = Path(args.plan) if args.plan else _latest(cfg.reports_dir, "duplicates_*.csv")
    if not csv_path or not csv_path.exists():
        print("No duplicates report found - run `duplicates` first.")
        return 1
    tiers = {t.strip().lower() for t in args.tier.split(",")} if args.tier else None
    if args.folder:
        scope = resolve_scope(cfg, args.folder)
    else:
        scope = _report_scope(cfg, csv_path) or WHOLE_DISK
    plan = dedupe_load(csv_path, cfg, tiers, scope)
    print(f"Plan: {csv_path}")
    print(f"Folders being cleaned: {scope.label()}")
    for r in plan.refused_groups:
        print(f"  ! {r}")
    by_tier = Counter(r.tier for r in plan.removals)
    print(f"Files to move to quarantine: {len(plan.removals):,} ({human_size(plan.total_bytes)})  "
          + ", ".join(f"{t}: {n}" for t, n in by_tier.items()))
    if plan.review_count:
        print(f"Files marked REVIEW (left untouched): {plan.review_count:,}")
    if plan.skip_count:
        print(f"Copies outside the selected folders (SKIP, left untouched): {plan.skip_count:,}")
    if plan.outside_scope:
        print(f"REMOVE rows ignored because they are outside the selected folders: {plan.outside_scope:,}")
    if not plan.removals:
        print("Nothing to do.")
        return 0
    per_folder = Counter(str(PurePosixPath(r.path).parent) for r in plan.removals)
    print("By folder:")
    for folder, n in per_folder.most_common(15):
        print(f"  {n:>7,}  {folder}")
    if len(per_folder) > 15:
        print(f"  ... and {len(per_folder) - 15:,} more folders")
    if not args.apply:
        print("\nDRY RUN - examples of what would be moved:")
        for r in plan.removals[:15]:
            print(f"  [{r.tier}] {r.path}\n         (kept: {r.keeper})")
        if len(plan.removals) > 15:
            print(f"  ... and {len(plan.removals) - 15:,} more")
        print("\nRe-run with --apply to move them to quarantine (nothing is permanently deleted).")
        return 0
    check_writable(cfg)
    if not confirm(
        f"\nAbout to MOVE {len(plan.removals):,} files ({human_size(plan.total_bytes)}) to\n  {cfg.quarantine_path}\n"
        "Each one is re-verified against its kept copy first. You can undo this later.",
        "yes", args.yes,
    ):
        print("Cancelled. Nothing was changed.")
        return 1
    with Index(cfg.db_path) as index:
        res = dedupe_apply(cfg, plan, index, csv_path.name)
    print(f"\nMoved {res.moved:,} files ({human_size(res.moved_bytes)}) to {res.quarantine_run}")
    if res.skipped:
        print(f"Skipped {len(res.skipped):,} (safety checks):")
        for s in res.skipped[:20]:
            print(f"  - {s}")
        if len(res.skipped) > 20:
            print(f"  ... see {res.manifest}")
    print(f"\nUndo log: {res.manifest}\n  undo:  python -m diskcleaner undo {res.manifest.stem} --apply")
    print("When you are happy, free the space with:  python -m diskcleaner purge --apply")
    return 0


def cmd_remove_list(cfg: Config, args) -> int:
    """Move the files named in a text list (one path per line) to quarantine."""
    list_path = Path(args.list)
    if not list_path.exists():
        print(f"List not found: {list_path}")
        return 1
    paths = read_path_list(list_path)
    found = [p for p in paths if (cfg.root / p).is_file()]
    total = sum((cfg.root / p).stat().st_size for p in found)
    print(f"List: {list_path}")
    print(f"Files to move to quarantine: {len(found):,} ({human_size(total)})")
    if len(found) < len(paths):
        print(f"Not found on the disk (will be skipped): {len(paths) - len(found):,}")
    per_folder = Counter(str(PurePosixPath(p).parent) for p in found)
    print("By folder:")
    for folder, n in per_folder.most_common(15):
        print(f"  {n:>7,}  {folder}")
    if len(per_folder) > 15:
        print(f"  ... and {len(per_folder) - 15:,} more folders")
    if not found:
        print("Nothing to do.")
        return 0
    if not args.apply:
        print("\nDRY RUN - examples of what would be moved:")
        for p in found[:10]:
            print(f"  {p}")
        print("\nRe-run with --apply to move them to quarantine (nothing is permanently deleted).")
        return 0
    check_writable(cfg)
    if not confirm(
        f"\nAbout to MOVE {len(found):,} files ({human_size(total)}) to\n  {cfg.quarantine_path}\nYou can undo this later.",
        "yes", args.yes,
    ):
        print("Cancelled. Nothing was changed.")
        return 1
    with Index(cfg.db_path) as index:
        res = quarantine_list(cfg, paths, index, list_path.name)
    print(f"\nMoved {res.moved:,} files ({human_size(res.moved_bytes)}) to {res.quarantine_run}")
    if res.skipped:
        print(f"Skipped {len(res.skipped):,}:")
        for s in res.skipped[:20]:
            print(f"  - {s}")
    print(f"\nUndo log: {res.manifest}\n  undo:  python -m diskcleaner undo {res.manifest.stem} --apply")
    print("When you are happy, free the space with:  python -m diskcleaner purge --apply")
    return 0


def cmd_folders(cfg: Config, args) -> int:
    """Show the folders, to help you choose what to clean first."""
    scope = _scope_from_args(cfg, args)
    with Index(cfg.db_path) as index:
        if not args.no_rescan:
            _do_scan(cfg, index)
        q = cfg.quarantine_dir.lower() + "/"
        rows = [r for r in index.all_files() if scope.contains(r.path) and not r.path.lower().startswith(q)]
        has_copy: set[str] = set()
        extra: set[str] = set()
        if not args.quick:
            print("Checking for byte-identical copies across the whole disk (cached after the first time) ...")
            for g in exact_groups(cfg, index):
                has_copy.update(m.path for m in g.members)
                extra.update(m.path for m in g.members if m.action == "REMOVE")
    if not rows:
        print("Nothing indexed here.")
        return 0
    base_depth = 0 if scope.whole_disk else min(len(PurePosixPath(f).parts) for f in scope.folders)
    depth = base_depth + args.depth
    stats: dict[str, dict] = {}
    for r in rows:
        parts = PurePosixPath(r.path).parts[:-1]
        key = "/".join(parts[:depth]) if parts else "(files at top level)"
        d = stats.setdefault(key, {"files": 0, "bytes": 0, "media": 0, "copy": 0, "extra": 0, "extra_b": 0})
        d["files"] += 1
        d["bytes"] += r.size
        d["media"] += r.category in ("photos", "videos")
        d["copy"] += r.path in has_copy
        if r.path in extra:
            d["extra"] += 1
            d["extra_b"] += r.size
    print(f"\n{'Folder':<44}{'Files':>8}{'Photo/Vid':>10}{'Size':>10}{'Has copy':>10}{'Extra':>8}{'Frees':>10}")
    for key, d in sorted(stats.items()):
        name = key if len(key) <= 42 else "…" + key[-41:]
        cols = ("?", "?", "?") if args.quick else (f"{d['copy']:,}", f"{d['extra']:,}", human_size(d["extra_b"]))
        print(f"{name:<44}{d['files']:>8,}{d['media']:>10,}{human_size(d['bytes']):>10}"
              f"{cols[0]:>10}{cols[1]:>8}{cols[2]:>10}")
    if not args.quick:
        print("\nHas copy = files with a byte-identical copy somewhere on the disk.")
        print("Extra    = the copies that would be removed (a better-placed copy is kept elsewhere).")
    print('\nClean one folder at a time with:  python -m diskcleaner duplicates --folder "<folder>"')
    return 0


def cmd_where(cfg: Config, args) -> int:
    """Does this file exist anywhere else on the disk? Where?"""
    p = Path(args.path).expanduser()
    full = p if p.is_absolute() else cfg.root / p
    if not full.is_file():
        print(f"Not a file: {full}")
        return 1
    try:
        rel = full.resolve().relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        print(f"{full} is not on the disk {cfg.root}")
        return 1
    errors: list[str] = []
    with Index(cfg.db_path) as index:
        row = index.get(rel)
        if row is None:
            print("This file is not in the index yet - run `scan` first.")
            return 1

        def others(sql, *values):
            # the other files that match the query (never the file itself)
            return [FileRow.from_row(r) for r in index.conn.execute(sql, (*values, rel))]

        same_size = others("SELECT * FROM files WHERE size=? AND path<>?", row.size)
        compute_hashes(cfg, index, [row, *same_size], "full_hash", errors, "Comparing files")
        exact = [o for o in same_size if row.full_hash and o.full_hash == row.full_hash]
        pixel = []
        if row.ext in DECODABLE_IMAGE_EXT and row.width:
            exact_paths = {o.path for o in exact}
            same_dim = [o for o in others("SELECT * FROM files WHERE width=? AND height=? AND path<>?",
                                          row.width, row.height)
                        if o.path not in exact_paths and o.ext in DECODABLE_IMAGE_EXT]
            compute_hashes(cfg, index, [row, *same_dim], "pixel_hash", errors, "Comparing pictures")
            pixel = [o for o in same_dim if row.pixel_hash and o.pixel_hash == row.pixel_hash]
    q = cfg.quarantine_dir.lower() + "/"

    def label(o):
        if o.path.lower().startswith(q):
            return o.path + "   (in quarantine)"
        return o.path

    print(f"{rel}  ({human_size(row.size)})")
    if not exact and not pixel:
        print("  No other copy found on the disk.")
    for o in exact:
        print(f"  identical file:    {label(o)}")
    for o in pixel:
        print(f"  identical picture: {label(o)}  ({human_size(o.size)}, different metadata)")
    return 0


def cmd_organize(cfg: Config, args) -> int:
    cfg.ensure_state_dirs()
    if not args.apply:
        with Index(cfg.db_path) as index:
            if not args.no_rescan:
                _do_scan(cfg, index)
            scope = _scope_from_args(cfg, args)
            print(f"Planning for: {scope.label()}")
            items = organize.build_plan(cfg, index, scope)
        path = cfg.reports_dir / f"organize_{timestamp()}.csv"
        organize.write_plan_csv(items, path)
        s = organize.plan_summary(items)
        print(f"\nOrganize plan: {s['files']:,} files -> {cfg.organize_path}")
        print("How each file was dated:")
        for src, n in s["by_source"].most_common():
            print(f"  {src:<16}{n:>8,}")
        print("Files per year:")
        for y, n in sorted(s["by_year"].items()):
            print(f"  {y:<16}{n:>8,}")
        print(f"Files keeping an event/folder name: {s['with_event']:,}")
        print(f"\nPlan saved (review/edit the 'dst' column if you like):\n  {path}")
        print("Nothing has been changed. To execute:  python -m diskcleaner organize --apply")
        return 0

    plan_path = Path(args.plan) if args.plan else _latest(cfg.reports_dir, "organize_*.csv")
    if not plan_path or not plan_path.exists():
        print("No organize plan found - run `organize` (without --apply) first.")
        return 1
    items = organize.read_plan_csv(plan_path)
    mode = args.mode or cfg.organize_mode
    if not items:
        print("Plan is empty.")
        return 0
    check_writable(cfg)
    if not confirm(
        f"About to {mode.upper()} {len(items):,} files into {cfg.organize_path}\n(plan: {plan_path.name}). "
        "Nothing is overwritten and every step can be undone.",
        "yes", args.yes,
    ):
        print("Cancelled. Nothing was changed.")
        return 1
    with Index(cfg.db_path) as index:
        res = organize.apply_plan(cfg, items, index, mode, plan_path.name)
    print(f"\n{mode.title()}d {res.done:,} files.")
    if res.skipped:
        print(f"Skipped {len(res.skipped):,}:")
        for s in res.skipped[:20]:
            print(f"  - {s}")
    print(f"\nUndo log: {res.manifest}\n  undo:  python -m diskcleaner undo {res.manifest.stem} --apply")
    if mode == "move":
        print("Tip: `python -m diskcleaner clean-empty-dirs` lists folders left empty.")
    return 0


def cmd_undo(cfg: Config, args) -> int:
    if not args.manifest:
        ms = list_manifests(cfg)
        if not ms:
            print("No operations recorded yet.")
            return 0
        print("Recorded operations (newest last):")
        for m in ms:
            header, ops = read_manifest(m)
            print(f"  {m.stem:<40} {header.get('action', '?'):<16} {len(ops):>7,} ops")
        print("\nUndo one with:  python -m diskcleaner undo <name> --apply   (or 'last')")
        return 0
    path = _resolve_manifest(cfg, args.manifest)
    header, ops = read_manifest(path)
    print(f"{path.name}: {header.get('action')} on {header.get('created')} - {len(ops):,} operations")
    if not args.apply:
        print("DRY RUN - re-run with --apply to reverse these operations.")
        return 0
    check_writable(cfg)
    if not confirm("Reverse these operations (files go back to where they were)?", "yes", args.yes):
        print("Cancelled.")
        return 1
    res = undo(cfg, path)
    print(f"Restored {res.restored:,} files; removed {res.copies_removed:,} copies; "
          f"{res.already_in_place:,} were already in place.")
    for p in res.problems[:30]:
        print(f"  ! {p}")
    print("Run `scan` to refresh the index.")
    return 0


def cmd_purge(cfg: Config, args) -> int:
    q = cfg.quarantine_path
    runs = sorted(p for p in q.iterdir() if p.is_dir()) if q.exists() else []
    if args.run:
        runs = [r for r in runs if r.name == args.run]
    if not runs:
        print("Quarantine is empty.")
        return 0
    total_files = total_bytes = 0
    for r in runs:
        files = [f for f in r.rglob("*") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        total_files += len(files)
        total_bytes += size
        print(f"  {r.name:<40}{len(files):>8,} files {human_size(size):>10}")
    if not args.apply:
        print(f"\nDRY RUN - {total_files:,} files ({human_size(total_bytes)}) would be PERMANENTLY deleted.")
        print("Browse the quarantine folder first if you want to double-check:", q)
        return 0
    if not confirm(
        f"\nPERMANENTLY delete {total_files:,} quarantined files ({human_size(total_bytes)})? This cannot be undone.",
        "PURGE",
    ):
        print("Cancelled. Nothing was deleted.")
        return 1
    for r in runs:
        shutil.rmtree(r)
    print(f"Deleted {total_files:,} files, freed {human_size(total_bytes)}.")
    return 0


def cmd_clean_empty_dirs(cfg: Config, args) -> int:
    dirs = organize.find_empty_dirs(cfg)
    if not dirs:
        print("No empty folders.")
        return 0
    for d in dirs[:40]:
        print(f"  {d.relative_to(cfg.root)}")
    if len(dirs) > 40:
        print(f"  ... and {len(dirs) - 40:,} more")
    if not args.apply:
        print(f"\nDRY RUN - {len(dirs):,} empty folders. Re-run with --apply to remove them.")
        return 0
    check_writable(cfg)
    if not confirm(f"Remove {len(dirs):,} empty folders?", "yes", args.yes):
        print("Cancelled.")
        return 1
    print(f"Removed {organize.remove_empty_dirs(dirs):,} folders.")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="diskcleaner", description="Safely de-duplicate and organize a recovered disk.")
    p.add_argument("-c", "--config", default=str(PROJECT_DIR / "config.toml"), help="config file (default: ./config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create config.toml from the example")
    s.add_argument("--root", help="path of the external disk, e.g. /Volumes/MyDisk")

    sub.add_parser("scan", help="index all files on the disk (incremental)")
    sub.add_parser("status", help="summary of the index, quarantine and recovery-date hints")

    s = sub.add_parser("folders", help="overview of folders (files, size, known duplicates) to pick what to clean")
    s.add_argument("--depth", type=int, default=1, help="how many folder levels to show (default 1)")
    s.add_argument("--quick", action="store_true", help="sizes only, skip the duplicate check")
    s.add_argument("--no-rescan", action="store_true", help="use the existing index without rescanning")
    s.add_argument("--folder", action="append", metavar="FOLDER",
                   help="only this folder (relative to the disk root); repeat for several")

    s = sub.add_parser("where", help="show every other place on the disk where a file exists")
    s.add_argument("path", help="file path (relative to the disk root, or absolute)")

    s = sub.add_parser("duplicates", help="find duplicates and write JSON + CSV + HTML reports (changes nothing)")
    s.add_argument("--no-rescan", action="store_true", help="use the existing index without rescanning")
    s.add_argument("--folder", action="append", metavar="FOLDER",
                   help="only this folder (relative to the disk root); repeat for several")

    s = sub.add_parser("dedupe", help="move duplicates from the report to quarantine")
    s.add_argument("--plan", help="duplicates CSV to use (default: newest report)")
    s.add_argument("--tier", help="only these tiers, e.g. 'exact' or 'exact,pixel'")
    s.add_argument("--folder", action="append", metavar="FOLDER",
                   help="only remove files inside this folder (default: the folders the report was made for)")
    s.add_argument("--apply", action="store_true", help="actually move files (otherwise dry run)")
    s.add_argument("--yes", action="store_true", help="skip the typed confirmation")

    s = sub.add_parser("remove-list", help="move the files named in a text list (one per line) to quarantine")
    s.add_argument("list", help="text file with one path per line, relative to the disk root")
    s.add_argument("--apply", action="store_true", help="actually move files (otherwise dry run)")
    s.add_argument("--yes", action="store_true", help="skip the typed confirmation")

    s = sub.add_parser("organize", help="plan (default) or --apply organizing into Year/Month folders")
    s.add_argument("--apply", action="store_true", help="execute the newest (or --plan) organize plan")
    s.add_argument("--plan", help="organize CSV to execute (default: newest)")
    s.add_argument("--mode", choices=["move", "copy"], help="override [organize].mode")
    s.add_argument("--no-rescan", action="store_true", help="plan from the existing index without rescanning")
    s.add_argument("--folder", action="append", metavar="FOLDER",
                   help="only this folder (relative to the disk root); repeat for several")
    s.add_argument("--yes", action="store_true", help="skip the typed confirmation")

    s = sub.add_parser("undo", help="list recorded operations or reverse one")
    s.add_argument("manifest", nargs="?", help="manifest name/path, or 'last'")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("purge", help="permanently delete quarantined duplicates")
    s.add_argument("--run", help="only this quarantine run folder")
    s.add_argument("--apply", action="store_true")

    s = sub.add_parser("clean-empty-dirs", help="remove folders left empty after organizing")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--yes", action="store_true")
    return p


COMMANDS = {
    "scan": cmd_scan, "status": cmd_status, "folders": cmd_folders, "where": cmd_where,
    "duplicates": cmd_duplicates, "dedupe": cmd_dedupe, "remove-list": cmd_remove_list,
    "organize": cmd_organize, "undo": cmd_undo, "purge": cmd_purge, "clean-empty-dirs": cmd_clean_empty_dirs,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    try:
        cfg = load_config(args.config)
        cfg.ensure_state_dirs()
        helpers.setup_logging(cfg.state_dir, args.verbose)
        return COMMANDS[args.command](cfg, args)
    except (ConfigError, SafetyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Everything done so far is recorded in the undo log; "
              "it is safe to re-run.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
