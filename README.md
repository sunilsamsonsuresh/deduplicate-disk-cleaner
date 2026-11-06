# Disk Cleaner — safe de-duplication & photo organizing for a recovered disk

After recovering a formatted disk, recovery software usually leaves you with
the same photo three or four times (`recup_dir.12/f1234567.jpg`,
`Lost Files/IMG_0001 (1).jpg`, ...), with no folders and modified dates that
all say "the day I ran the recovery". This tool:

1. **Scans** the disk and reads each photo/video's metadata (EXIF date, resolution, video creation date).
2. **Finds duplicates** at three levels of certainty and writes a report you can review, with thumbnails.
3. **Removes duplicates safely.** They're moved to a quarantine folder (not deleted), every file is re-checked against the copy being kept, and all of it can be undone.
4. **Organizes** photos & videos into `Photos/2015/2015-06 Goa Trip/` style folders, reusing your existing folder names.

> **Nothing is permanently deleted until you run `purge --apply` and type `PURGE`.**
> Every other change can be reversed with `undo`.

---

## 1. Setup (once)

Requires Python 3.11+ (macOS: `python3 --version`).

```bash
cd deduplicate-disk-cleaner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit **`config.toml`** and set your disk's path (external disks are under `/Volumes`):

```toml
[disk]
root = "/Volumes/MyPassport"
```

(If `config.toml` is missing: `python -m diskcleaner init --root "/Volumes/MyPassport"`.)

> **macOS notes**
> - The first time, macOS may ask "Terminal would like to access files on a removable volume". Click **Allow**.
> - **NTFS disks are read-only on macOS.** The tool checks this and stops before changing anything. Scanning and reports still work.

## 2. The workflow

Run everything from the project folder with the venv active.

| Step | Command | Changes files? |
|---|---|---|
| Index the disk | `python -m diskcleaner scan` | no |
| Overview + recovery-date hint | `python -m diskcleaner status` | no |
| Which folders hold the extra copies? | `python -m diskcleaner folders` | no |
| Find duplicates (one folder) → JSON/CSV/HTML | `python -m diskcleaner duplicates --folder "recup_dir.1"` | no |
| Find duplicates (whole disk) | `python -m diskcleaner duplicates` | no |
| Where else does this file exist? | `python -m diskcleaner where "Photos/IMG_0001.jpg"` | no |
| Preview the removal | `python -m diskcleaner dedupe` | no (dry run) |
| Move duplicates to quarantine | `python -m diskcleaner dedupe --apply` | moves, undoable |
| Plan the organizing | `python -m diskcleaner organize` | no |
| Organize | `python -m diskcleaner organize --apply` | moves, undoable |
| Remove leftover empty folders | `python -m diskcleaner clean-empty-dirs --apply` | yes |
| List / undo operations | `python -m diskcleaner undo` / `undo last --apply` | reverses |
| Free the space for good | `python -m diskcleaner purge --apply` | **permanent** |

### Step by step

**a) Scan and check the dates**

```bash
python -m diskcleaner scan
python -m diskcleaner status
```

`status` lists the most common "modified" dates. If one date accounts for most
files (e.g. `2026-10-01 … 87%`), that's the day the recovery ran, and those dates
mean nothing. Put that date in `config.toml` so it isn't used to date files:

```toml
[organize]
ignore_mtime_after = "2026-10-01"
```

**b) Pick a folder to start with (folder-by-folder mode)**

```bash
python -m diskcleaner folders              # top-level folders
python -m diskcleaner folders --depth 2    # one level deeper
python -m diskcleaner folders --folder "My Old Laptop" --depth 1   # inside one folder
```

```
Folder                     Files Photo/Vid      Size  Has copy   Extra     Frees
Lost Files                    40        40  218.7 KB        40      40  218.7 KB
My Old Laptop                300       300    1.6 MB       166       0       0 B
recup_dir.1                   38        38  206.4 KB        38      38  206.4 KB
```

*Has copy* counts the files that have a byte-identical copy somewhere on the disk.
*Extra* counts the copies that would be removed because a better-placed copy is kept
elsewhere. In the example, `recup_dir.1` and `Lost Files` contain nothing but extra copies, so
they're good first targets. `My Old Laptop` holds the originals.

Then work on one or more folders at a time:

```bash
python -m diskcleaner duplicates --folder "recup_dir.1"
python -m diskcleaner duplicates --folder "Lost Files" --folder "recup_dir.2"
```

You can also list the folders in `config.toml` instead of typing them each time:

```toml
[scope]
folders = ["recup_dir.1", "Lost Files"]
```

How folder mode works:
- The **whole disk** is still searched for copies, so the report can tell you where else each file exists.
- **Only files inside the selected folders** are ever proposed for removal or organized.
- Copies elsewhere show up as `SKIP`. They're listed so you know where they are, but nothing happens to them in this run.
- `dedupe` automatically sticks to the folders its report was made for. `--folder` can narrow it further.
- Folder names are relative to the disk root. You can also paste an absolute path from Finder. A misspelt folder name is an error, never a silent whole-disk run.

**c) Review: where does each file exist?**

`duplicates` (with or without `--folder`) writes three files to `state/reports/`:


```bash
python -m diskcleaner duplicates
```

- `duplicates_<time>.json`: **the review file.** It has one entry for every file in the selected folders that also exists elsewhere, grouped by folder. Each entry shows what will happen to the file and **every other place the same file exists**:

  ```json
  "Lost Files/Deep Scan": [
    {
      "path": "Lost Files/Deep Scan/IMG_0003 (1).jpg",
      "action": "REMOVE",
      "kept_copy": "My Old Laptop/Goa Trip 2015/IMG_0003.jpg",
      "why": "identical file (same bytes) of the kept copy - will be moved to quarantine",
      "other_copies": [
        { "path": "My Old Laptop/Goa Trip 2015/IMG_0003.jpg", "match": "identical file (same bytes)",
          "action": "KEEP", "in_selected_folders": false, "size": "5.8 KB", "resolution": "240x180" },
        { "path": "recup_dir.5/f6686328.jpg", "match": "identical file (same bytes)",
          "action": "SKIP", "in_selected_folders": false }
      ],
      "exists_elsewhere": true,
      "exists_outside_selected_folders": true
    }
  ]
  ```
  `match` is `identical file (same bytes)`, `identical picture (same pixels)` or `looks similar (distance N)`.
  The top of the file has a summary with counts per folder. The JSON is for reading; decisions are changed in the CSV.
- `duplicates_<time>.html`: open it in a browser. Each group shows thumbnails, with the **kept** copy in green and the copies to **remove** in red. Copies marked **review** (amber) are left alone unless you decide otherwise.
- `duplicates_<time>.csv`: the editable plan. Open it in Numbers/Excel and change the `action` column (`KEEP` / `REMOVE` / `REVIEW` / `SKIP`) for any file. Save it as CSV. The `in_selected_folders` column shows which rows are in scope.

To check a single file at any time:

```bash
python -m diskcleaner where "My Old Laptop/Goa Trip 2015/IMG_0003.jpg"
#   identical file:    Lost Files/Deep Scan/IMG_0003 (1).jpg
#   identical file:    recup_dir.5/f6686328.jpg
```

The three duplicate levels:

| Tier | Meaning | Default |
|---|---|---|
| `exact` | Byte-for-byte identical files (any type: photos, videos, documents…) | REMOVE |
| `pixel` | Same image pixels, different file bytes (e.g. one copy lost its EXIF data in recovery). The copy with metadata is kept. | REMOVE |
| `similar` | Looks the same: resized, thumbnail, or re-compressed copies. The highest resolution is kept. | **REVIEW** (never removed unless you change it to REMOVE) |

How the copy to keep is chosen: a copy in a normally named folder (`Goa Trip 2015`) beats
one in a recovery folder (`recup_dir`, `FOUND.000`, `Lost Files`, `Recovered`…).
A real file name beats a generic one (`f1234567.jpg`, `IMG_1 (1).jpg`, `… - Copy`).
Copies with EXIF beat copies without. Copies already inside `_Organized` are preferred.
You can also list folders you trust in `preferred_folders`.

**d) Remove duplicates (to quarantine)**

```bash
python -m diskcleaner dedupe                 # dry run: shows what would move
python -m diskcleaner dedupe --apply         # asks you to type "yes"
python -m diskcleaner dedupe --apply --tier exact          # only byte-identical ones
python -m diskcleaner dedupe --apply --plan state/reports/duplicates_….csv   # a CSV you edited
python -m diskcleaner dedupe --apply --folder "recup_dir.1"   # only this folder, even from a bigger report
```

The safety checks applied to every file:
- The kept copy must still exist. **Both** files are re-hashed right before the move, so a file that changed since the report is skipped.
- If a group has no copy marked KEEP (e.g. you set them all to REMOVE or SKIP), the whole group is refused.
- With folders selected, only files inside them can be moved, even if the CSV says REMOVE somewhere else.
- Files are **moved** to `<disk>/_DiskCleaner/Quarantine/<run>/` under their original folder path, so you can browse them in Finder.
- Each move is written to a log (`state/manifests/*.jsonl`) *before and after* it happens, so even a crash or power cut mid-run can be undone.
- Nothing is ever overwritten.

**e) Organize**

```bash
python -m diskcleaner organize               # writes state/reports/organize_<time>.csv + summary
python -m diskcleaner organize --apply       # executes the newest plan
python -m diskcleaner organize --apply --mode copy   # copy instead of move (needs free space)
python -m diskcleaner organize --folder "My Old Laptop"   # plan only one folder
```

Result (inside your disk):

```
_Organized/
  Photos/2015/2015-06 Goa Trip/IMG_1234.jpg     ← date from EXIF, name from old folder "Goa Trip 2015"
  Photos/2015/2015-07/IMG_2000.jpg
  Photos/2016/Diwali/…                          ← only the year is known (from folder "Diwali 2016")
  Videos/2018/2018-12/MVI_0001.mp4              ← date from the video's own metadata
  Photos/Unknown Date/Kerala Holidays/…         ← no date found anywhere
```

How a file's date is found, best source first:
1. EXIF date taken (JPEG/PNG/TIFF/HEIC)
2. Video creation time (MP4/MOV/M4V/3GP)
3. File name: `IMG_20150612_…`, `2015-06-12 14.30.00`, `IMG-20150612-WA0001` (WhatsApp), `FB_IMG_1434112200000`…
4. Folder names: `Goa Trip 2015`, `2015/06`, `June 2015`
5. File modified date, unless it falls on/after `ignore_mtime_after` or on one of the
   `ignore_mtime_dates` (days when thousands of files were copied at once and all got that date)

The plan CSV shows each file's date and its source (`date_source`), so you can check it before applying.
Only photos and videos are organized by default. Documents usually have folder
structures worth keeping; you can add them through `[organize].categories`.
If a folder name is wrongly used as an event name (e.g. `My Old Laptop`),
add it to `generic_folder_names`.

Plain year/month folders without any old folder names: set `keep_event_names = false`.

Folders that are already sorted by device can keep their own date folders instead of
going into `_Organized` (`organize_inside = ["Existing Photos/*"]`):

```
Existing Photos/Samsung A8/2019/2019-06/IMG_1234.jpg      ← layout_inside = "{year}/{month_dir}"
```

A file is never moved from one of these folders into another.

**f) Tidy up, double-check, then free the space**

```bash
python -m diskcleaner clean-empty-dirs --apply   # removes folders that are now empty
python -m diskcleaner undo                        # lists every recorded operation
python -m diskcleaner undo last --apply           # reverses the most recent one
python -m diskcleaner purge                       # shows what's in quarantine
python -m diskcleaner purge --apply               # type PURGE to delete permanently
```

Wait a while before purging, and look through the quarantine folder first if you want to be sure.

## 3. Good to know

- **Big disks:** scanning is incremental. Hashes are cached in `state/index.sqlite3`, so re-runs only process new or changed files. You can stop with Ctrl+C at any time and re-run.
- **Speed:** only files that share an exact size are hashed, and only images that share a resolution are pixel-compared. On a USB hard disk, expect roughly 30–60 minutes per few hundred GB for the first `duplicates` run.
- **Parallel work:** `[scan].workers` (default 4) files are read and compared at the same time. Image decoding scales with your CPU cores, but an old USB disk is usually the real limit (measured: about 1.3× faster with 4 workers on a disk giving ~20 MB/s). Results are always identical whatever the number.
- **Corrupted files:** partially recovered images that can't be opened are listed in `state/reports/unreadable_<time>.txt`. They're still checked for exact duplicates.
- **`state/` folder:** holds the index, reports and undo logs. It is kept *off* the disk being cleaned on purpose. Don't delete it until you're done (and have purged).
- **RAW photos** (CR2, NEF, ARW, DNG…) are matched as exact duplicates and dated by file name/folder/mtime, but not pixel-compared.
- Use `-c other_config.toml` to work on a different disk, and `-v` for detailed output. A full log is written to `state/logs/`.

## 4. Development

```bash
pip install -r requirements.txt
python -m pytest -q          # 109 tests: dating rules, duplicate tiers, folder scoping, JSON report, safety checks, undo, CLI
```

Design notes: [docs/DESIGN.md](docs/DESIGN.md).

```
diskcleaner/
  cli.py         commands & confirmations
  config.py      config.toml loading/validation
  scanner.py     incremental disk walk + metadata
  index.py       SQLite cache
  hashing.py     BLAKE2b partial/full hashes
  media.py       EXIF, pixel hash, perceptual hash, MP4 dates
  dates.py       date & event-name heuristics
  duplicates.py  tiered detection + keeper scoring
  reports.py     JSON / CSV / HTML reports
  dedupe.py      verified quarantine moves
  organizer.py   Year/Month organizing, empty-folder cleanup
  scope.py       folder-by-folder selection
  safeops.py     never-overwrite moves, manifests, undo
```
