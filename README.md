# Disk Cleaner

Disk Cleaner removes duplicate files from a recovered disk. It also sorts your photos and videos into year and month folders.

Recovery software often gives you the same photo three or four times, for example `recup_dir.12/f1234567.jpg` and `Lost Files/IMG_0001 (1).jpg`. The folders are gone. All files show the same "modified" date: the day of the recovery.

The tool does these four things:

1. **Scan.** It makes a list of all files on the disk. It reads the date and the size of each photo and video.
2. **Find duplicates.** It finds copies in three levels of certainty. It writes a report with small pictures, so you can look before anything changes.
3. **Remove duplicates safely.** It moves the copies to a quarantine folder. It does not delete them. Before each move, it checks the file against the copy that it keeps. You can undo each move.
4. **Organize.** It sorts photos and videos into folders like `Photos/2015/2015-06 Goa Trip/`. It uses your old folder names when they are good names.

> **The tool deletes nothing until you run `purge --apply` and type `PURGE`.**
> You can reverse all other changes with `undo`.

---

## 1. Setup (one time)

You need Python 3.11 or newer. To check, run `python3 --version`.

```bash
cd deduplicate-disk-cleaner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Make your config file:

```bash
python -m diskcleaner init --root "/Volumes/MyPassport"
```

This makes `config.toml` from `config.example.toml`. External disks are in `/Volumes`. You can edit `config.toml` at any time:

```toml
[disk]
root = "/Volumes/MyPassport"
```

> **macOS notes**
> - The first time, macOS can ask: "Terminal would like to access files on a removable volume". Click **Allow**.
> - **macOS can only read NTFS disks** if you do not have an extra driver. The tool checks this. It stops before it changes anything. Scans and reports still work.

## 2. The commands

Run all commands from the project folder. The venv must be active.

| What you want | Command | Does it change files? |
|---|---|---|
| Make the list of files | `python -m diskcleaner scan` | no |
| See a summary and the recovery date | `python -m diskcleaner status` | no |
| See which folders have extra copies | `python -m diskcleaner folders` | no |
| Find duplicates in one folder | `python -m diskcleaner duplicates --folder "recup_dir.1"` | no |
| Find duplicates on the whole disk | `python -m diskcleaner duplicates` | no |
| See where other copies of a file are | `python -m diskcleaner where "Photos/IMG_0001.jpg"` | no |
| See what the removal will do | `python -m diskcleaner dedupe` | no (dry run) |
| Move duplicates to the quarantine | `python -m diskcleaner dedupe --apply` | yes, you can undo it |
| Move the files in a text list to the quarantine | `python -m diskcleaner remove-list movies.txt --apply` | yes, you can undo it |
| Make the organize plan | `python -m diskcleaner organize` | no |
| Organize | `python -m diskcleaner organize --apply` | yes, you can undo it |
| Remove empty folders | `python -m diskcleaner clean-empty-dirs --apply` | yes |
| List or undo changes | `python -m diskcleaner undo` / `undo last --apply` | reverses a change |
| Delete the quarantine for good | `python -m diskcleaner purge --apply` | **yes, permanent** |

A command that changes files first only shows what it will do. This is a "dry run". To make the change, add `--apply`. Then the command asks you to type `yes`.

## 3. Step by step

### a) Scan and check the dates

```bash
python -m diskcleaner scan
python -m diskcleaner status
```

`status` shows the most common "modified" dates. Sometimes one date has most of the files, for example `2026-10-01 … 87%`. That is the day of the recovery, so that date is not useful. Put it in `config.toml`. Then the tool does not use it to date files:

```toml
[organize]
ignore_mtime_after = "2026-10-01"
```

### b) Choose a folder to start with

```bash
python -m diskcleaner folders              # the top folders
python -m diskcleaner folders --depth 2    # one level deeper
python -m diskcleaner folders --folder "My Old Laptop" --depth 1   # inside one folder
```

```
Folder                     Files Photo/Vid      Size  Has copy   Extra     Frees
Lost Files                    40        40  218.7 KB        40      40  218.7 KB
My Old Laptop                300       300    1.6 MB       166       0       0 B
recup_dir.1                   38        38  206.4 KB        38      38  206.4 KB
```

- **Has copy** is the number of files with an exact copy somewhere on the disk.
- **Extra** is the number of copies that the tool will remove. It keeps a better copy in a different place.

In this example, `recup_dir.1` and `Lost Files` have only extra copies. Start with them. `My Old Laptop` has the originals.

Then work on one or more folders at a time:

```bash
python -m diskcleaner duplicates --folder "recup_dir.1"
python -m diskcleaner duplicates --folder "Lost Files" --folder "recup_dir.2"
```

You can also put the folders in `config.toml`. Then you do not have to type them each time:

```toml
[scope]
folders = ["recup_dir.1", "Lost Files"]
```

How the folder mode works:

- The tool still looks for copies on the **whole disk**. Thus the report can show you where the other copies are.
- The tool removes or organizes **only files in the selected folders**.
- Copies in other folders show as `SKIP`. The report shows them, but the tool does not touch them.
- `dedupe` uses the same folders as its report. You can make the list smaller with `--folder`.
- Folder names start at the disk root. You can also paste a full path from Finder. A wrong folder name gives an error. It never starts a run on the whole disk.

### c) Look at the report

`duplicates` writes three files to `state/reports/`:

- **`duplicates_<time>.json`: the review file.** It has one entry for each file in the selected folders that has copies. It shows what happens to the file, and **all the other places where the same file is**:

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

  `match` is `identical file (same bytes)`, `identical picture (same pixels)` or `looks similar (distance N)`. The start of the file has a summary for each folder. The JSON file is only for reading. To change a decision, edit the CSV file.

- **`duplicates_<time>.html`:** open it in a browser. Each group shows small pictures. The **kept** copy is green. The copies to **remove** are red. The copies to **review** are amber. The tool does not touch the amber copies unless you change them.

- **`duplicates_<time>.csv`: the plan that you can edit.** Open it in Numbers or Excel. Change the `action` column (`KEEP`, `REMOVE`, `REVIEW` or `SKIP`) for a file. Save it as CSV. The `in_selected_folders` column shows which rows are in the selected folders.

To check one file at any time:

```bash
python -m diskcleaner where "My Old Laptop/Goa Trip 2015/IMG_0003.jpg"
#   identical file:    Lost Files/Deep Scan/IMG_0003 (1).jpg
#   identical file:    recup_dir.5/f6686328.jpg
```

The three levels ("tiers") of duplicates:

| Tier | What it means | Default action |
|---|---|---|
| `exact` | The files have the same bytes. This works for all file types. | REMOVE |
| `pixel` | The images have the same pixels, but the files are different. For example, one copy lost its EXIF data in the recovery. The tool keeps the copy with the metadata. | REMOVE |
| `similar` | The images look the same: a smaller copy, a thumbnail or a copy that was saved again. The tool keeps the biggest picture. | **REVIEW** (the tool does not remove these unless you change them to REMOVE) |

How the tool chooses the copy to keep:

- A copy in a real folder (`Goa Trip 2015`) wins over a copy in a recovery folder (`recup_dir`, `FOUND.000`, `Lost Files`, `Recovered` and so on).
- A real file name wins over a general name (`f1234567.jpg`, `IMG_1 (1).jpg`, `… - Copy`).
- A copy with EXIF wins over a copy without EXIF.
- A copy in `_Organized` wins.
- You can also give folders that you trust in `preferred_folders`.

### d) Remove the duplicates (to the quarantine)

```bash
python -m diskcleaner dedupe                 # dry run: shows what will move
python -m diskcleaner dedupe --apply         # asks you to type "yes"
python -m diskcleaner dedupe --apply --tier exact          # only exact copies
python -m diskcleaner dedupe --apply --plan state/reports/duplicates_….csv   # a CSV that you edited
python -m diskcleaner dedupe --apply --folder "recup_dir.1"   # only this folder
```

The safety checks for each file:

- The kept copy must still be there. The tool calculates the hash of **both** files again, right before the move. If a file changed after the report, the tool skips it.
- If no copy in a group has KEEP (for example, you set all of them to REMOVE or SKIP), the tool refuses the whole group.
- With selected folders, the tool moves only files in these folders. This is also true if the CSV says REMOVE for a file in a different folder.
- The tool **moves** the files to `<disk>/_DiskCleaner/Quarantine/<run>/`. Each file keeps its old folder path, so you can look at them in Finder.
- The tool writes each move to an undo log (`state/manifests/*.jsonl`) *before and after* the move. Thus you can undo a run, also after a crash or a power cut.
- The tool never overwrites a file.

You can also remove files that you choose yourself, for example downloaded movies. Put one path on each line in a text file (relative to the disk root). Then run:

```bash
python -m diskcleaner remove-list movies.txt           # dry run
python -m diskcleaner remove-list movies.txt --apply   # moves them to the quarantine
```

### e) Organize

```bash
python -m diskcleaner organize               # writes state/reports/organize_<time>.csv and a summary
python -m diskcleaner organize --apply       # does the newest plan
python -m diskcleaner organize --apply --mode copy   # copy, do not move (needs free space)
python -m diskcleaner organize --folder "My Old Laptop"   # plan only one folder
```

The result on your disk:

```
_Organized/
  Photos/2015/2015-06 Goa Trip/IMG_1234.jpg     ← date from EXIF, name from the old folder "Goa Trip 2015"
  Photos/2015/2015-07/IMG_2000.jpg
  Photos/2016/Diwali/…                          ← only the year is known (from the folder "Diwali 2016")
  Videos/2018/2018-12/MVI_0001.mp4              ← date from the video file
  Photos/Unknown Date/Kerala Holidays/…         ← no date found
```

How the tool finds the date of a file. It uses the first source that has a date:

1. The EXIF "date taken" (JPEG, PNG, TIFF, HEIC)
2. The video creation time (MP4, MOV, M4V, 3GP)
3. The file name: `IMG_20150612_…`, `2015-06-12 14.30.00`, `IMG-20150612-WA0001` (WhatsApp), `FB_IMG_1434112200000` and so on
4. The folder names: `Goa Trip 2015`, `2015/06`, `June 2015`
5. The "modified" date of the file. The tool does not use it if it is on or after `ignore_mtime_after`, or if it is in `ignore_mtime_dates`. (On these days, a copy gave thousands of files the same date.)

The plan CSV shows the date of each file and its source (`date_source`). You can check them before you apply the plan.

More settings:

- By default, the tool organizes only photos and videos. Documents usually have folders that you want to keep. To add them, use `[organize].categories`.
- If the tool uses a folder name as an event name by mistake (for example `My Old Laptop`), add it to `generic_folder_names`.
- For plain year and month folders without old folder names, set `keep_event_names = false`.
- A folder that is already sorted by device can keep its own date folders. It does not go into `_Organized`. Use `organize_inside = ["Existing Photos/*"]`:

  ```
  Existing Photos/Phone/2019/2019-06/IMG_1234.jpg      ← layout_inside = "{year}/{month_dir}"
  ```

  The tool never moves a file from one of these folders into a different one.

### f) Clean up, check again, then free the space

```bash
python -m diskcleaner clean-empty-dirs --apply   # removes the folders that are now empty
python -m diskcleaner undo                        # lists all recorded changes
python -m diskcleaner undo last --apply           # reverses the newest change
python -m diskcleaner purge                       # shows what is in the quarantine
python -m diskcleaner purge --apply               # type PURGE to delete for good
```

Wait some time before you purge. If you want to be sure, look through the quarantine folder first.

## 4. Good to know

- **Big disks:** the scan reads only new or changed files. The hashes stay in `state/index.sqlite3`. You can stop at any time with Ctrl+C and run the command again.
- **Speed:** the tool calculates hashes only for files that have the same size as another file. It compares pixels only for images that have the same size in pixels. On a USB hard disk, the first `duplicates` run takes about 30 to 60 minutes for each few hundred GB.
- **Parallel work:** the tool reads and compares `[scan].workers` files at the same time (default 4). An old USB disk is usually the limit. (On a disk with about 20 MB/s, 4 workers were about 1.3 times faster.) The results are always the same, for each number of workers.
- **Damaged files:** the tool lists images that it cannot open in `state/reports/unreadable_<time>.txt`. It still checks them for exact copies.
- **The `state/` folder** has the index, the reports and the undo logs. It is *not* on the disk that you clean. This is on purpose. Do not delete it until you are done and have purged.
- **RAW photos** (CR2, NEF, ARW, DNG and so on): the tool finds exact copies. It dates them by file name, folder name or modified date. It does not compare their pixels.
- To work on a different disk, use `-c other_config.toml`. For more details on the screen, use `-v`. The full log is in `state/logs/`.

## 5. For developers

```bash
pip install -r requirements.txt
python -m pytest -q          # 118 tests
```

Design notes: [docs/DESIGN.md](docs/DESIGN.md).

Each file has one job:

```
diskcleaner/
  commands.py           the commands that you type, and the confirm questions
  config.py             read and check config.toml
  scan_disk.py          scan the disk, read photo and video metadata
  file_index.py         the SQLite index of all files
  file_types.py         find the type of a file from its extension
  file_hashes.py        quick and full file hashes
  media_info.py         EXIF dates, pixel hash, look-alike hash, MP4 dates
  dates.py              find the date and the event name of a file
  find_duplicates.py    find duplicates in 3 tiers, choose the copy to keep
  reports.py            the JSON, CSV and HTML reports
  remove_duplicates.py  move duplicates to the quarantine, with checks
  organize.py           sort photos and videos into year and month folders
  selected_folders.py   work on one folder at a time
  safe_moves.py         safe moves and copies (never overwrite)
  undo_log.py           the undo log, and undo
  helpers.py            the log, progress bars, file sizes, parallel work
```
