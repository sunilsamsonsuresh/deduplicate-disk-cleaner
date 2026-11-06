# Design & Plan

## Goals

1. Scan an external disk full of recovered files.
2. Find duplicates, from "byte-for-byte identical" down to "looks like the same photo".
3. Let the user review the list **before** anything happens.
4. Remove duplicates in a way that **cannot lose the last copy** of anything.
5. Organize photos/videos into `Category/Year/Year-Month [Event]` folders, reusing
   meaningful existing folder names ("Goa Trip 2015") as event names.
6. Everything is undoable.

## Safety principles (non-negotiable)

| Rule | How it is enforced |
|---|---|
| Nothing is ever deleted by `dedupe` | Duplicates are **moved** into a quarantine folder on the same disk (`_DiskCleaner/Quarantine/<run>/…`, original relative path preserved). Permanent deletion is a separate `purge` command that requires typing `PURGE`. |
| Dry-run first | Every destructive command (`dedupe`, `organize apply`, `purge`, `clean-empty-dirs`) only prints what it would do unless `--apply` is passed, and then asks for typed confirmation. |
| Never remove the last copy | Each duplicate group must keep ≥1 member. Before each move the tool re-checks the kept file exists and re-hashes **both** files; if anything changed since the report, that file is skipped. Groups where the user marked every member REMOVE are refused. |
| Never overwrite | Moves refuse to overwrite; name clashes get ` (2)`, ` (3)` suffixes. |
| Crash-safe log | Every move is appended + fsync'd to a JSONL manifest *before* returning, so an interrupted run is still fully undoable with `undo`. |
| Cross-disk moves | If source and target are on different volumes the tool copies, verifies the hash, then removes the source. |
| Writable check | Refuses to start if the disk is read-only (e.g. NTFS on macOS) instead of failing mid-way. |

## Duplicate tiers

Detection is staged so expensive work is only done when needed:

1. **exact** – same size → same partial hash (first+last 64 KiB) → same full BLAKE2b hash.
   Byte-identical. Safe to remove. *Default action: REMOVE.*
2. **pixel** – images with identical decoded pixels but different bytes (e.g. one copy
   lost its EXIF during recovery). Only computed for images sharing the same
   width×height. *Default action: REMOVE (configurable)*; the copy with EXIF/more
   metadata is kept.
3. **similar** – perceptual 128-bit difference hash (dHash) within a Hamming distance
   threshold (resized copies, thumbnails, re-compressed versions). Found with a BK-tree
   so it scales to 100k+ photos. *Default action: REVIEW (never removed unless the user
   edits the CSV to REMOVE)*; highest resolution is kept.

Tiers chain: each exact group is collapsed to its keeper before the pixel tier runs,
and pixel groups to their keeper before the similar tier, so a file is only ever
"removed" by one tier.

### Choosing which copy to keep

Score each path (higher = keep):
- `-10` path contains recovery-tool folder patterns (`recup_dir`, `FOUND.000`,
  `$RECYCLE.BIN`, `Lost Files`, `Recovered`, …; configurable)
- `+10` path under a user-specified `preferred_folders` entry
- `-3` generic/recovered file name (`f12345678.jpg`, `FILE0001.CHK`, `IMG_1 (1).jpg`,
  `Copy of …`, `… - Copy`)
- `+2` lives in a meaningful named folder (an "event" folder)
- tie-breakers: oldest modified time, then shortest path, then alphabetical (deterministic)

The reason is written in the report so the user sees *why*.

## Dating files (for organizing)

Priority, first hit wins:
1. EXIF `DateTimeOriginal` / `DateTimeDigitized` / `DateTime` (JPEG, TIFF, PNG, HEIC*)
2. Video container creation time (MP4/MOV/M4V/3GP `mvhd` atom, parsed natively)
3. File name (`IMG_20150612_143000`, `2015-06-12 14.30.00`, `IMG-20150612-WA0001`,
   `FB_IMG_1434112200000`, …)
4. Folder names (`Goa Trip 2015`, `2015/06`, `June 2015`) — year, maybe month
5. File modified time — optional, and ignorable after a cut-off date because recovery
   tools often stamp every file with the recovery date.
6. Otherwise → `Unknown Date/<original folder name>/`

Every date is validated against `min_year` and "not in the future".

## Layout

```
<root>/_Organized/
  Photos/2015/2015-06 Goa Trip/IMG_1234.jpg
  Photos/2015/2015-07/IMG_2000.jpg
  Photos/2015/Unknown Month/…          (year known from folder only)
  Videos/2016/2016-12/VID_….mp4
  Photos/Unknown Date/Old Laptop/…
```

Only `photos` and `videos` are organized by default (documents/archives usually have
meaningful folder structures already); configurable.

## Workflow / commands

```
scan               incremental index (SQLite) of the disk – resumable
status             index summary, most common modified-dates (spots the recovery date)
duplicates         find duplicates → CSV + HTML (thumbnails) report
dedupe [--apply]   quarantine duplicates from the (optionally edited) CSV
organize plan      compute target folders → CSV report
organize apply     execute the plan (move or copy)
undo <manifest>    reverse any apply run
purge              permanently delete quarantined files (typed confirmation)
clean-empty-dirs   remove folders left empty
```

## Architecture

```
diskcleaner/
  commands.py           argparse commands, confirmations
  config.py             TOML config loading + validation + defaults
  scan_disk.py          filesystem walk, metadata extraction, incremental updates
  file_index.py         SQLite index (relative paths, cached hashes/metadata)
  file_types.py         file type from the extension (and from the first bytes)
  file_hashes.py        partial/full file hashes
  media_info.py         image size, EXIF dates, pixel hash, dHash, MP4 date parsing
  dates.py              filename/folder date parsing, event names, date resolution
  find_duplicates.py    tiered grouping, keeper scoring, BK-tree
  reports.py            JSON / CSV / HTML / console summaries
  remove_duplicates.py  verified quarantine moves (from the CSV or from a text list)
  organize.py           organize plan + apply, empty-folder clean-up
  selected_folders.py   folder-by-folder scope
  safe_moves.py         never-overwrite moves, verified copies, path guards
  undo_log.py           JSONL manifests (undo logs) and undo
  helpers.py            logging, progress bars, sizes, parallel_map
```

State (index DB, reports, manifests) lives in `state_dir` (default `./state` in the
project) — off the disk being cleaned, so a mistake on the disk can't destroy the log.
