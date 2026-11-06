"""Find the date of a file. Also find the folder names that are good event names
(for example "Goa Trip 2015")."""

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# Month names (short and long) and their numbers.
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
# All month names for the regular expressions below, longest first.
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class DateInfo:
    year: int | None
    month: int | None
    day: int | None
    source: str  # exif | video-metadata | filename | folder | mtime | unknown

    @property
    def known(self) -> bool:
        return self.year is not None

    def iso(self) -> str:
        if not self.year:
            return ""
        if not self.month:
            return f"{self.year:04d}"
        if not self.day:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


UNKNOWN = DateInfo(None, None, None, "unknown")


def _valid(y: int, m: int | None = None, d: int | None = None, *, min_year: int, today: dt.date) -> bool:
    """True if the date is possible: not before min_year and not after today."""
    if y < min_year or y > today.year:
        return False
    try:
        if m and d:
            return dt.date(y, m, d) <= today
        if m:
            return 1 <= m <= 12 and (y, m) <= (today.year, today.month)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# File name dates
# ---------------------------------------------------------------------------
# YYYY MM DD with optional separators: 20150612, 2015-06-12, 2015_06_12, 2015.06.12
_FN_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])(?!\d)")
# DD-MM-YYYY (common on some phones / Windows exports) - separators required
_FN_DMY = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[-_.](0[1-9]|1[0-2])[-_.]((?:19|20)\d{2})(?!\d)")
# Unix timestamps (seconds or milliseconds): 1434112200 / 1434112200000
_FN_EPOCH = re.compile(r"^(?:fb_img_|received_|img_|image_|signal-)?(\d{10}|\d{13})$", re.I)
# PhotoRec names like f12345678. The digits are disk sectors, NOT dates.
_PHOTOREC = re.compile(r"^[ft]\d{5,}$", re.I)


def date_from_filename(name: str, *, min_year: int = 1990, today: dt.date | None = None) -> DateInfo | None:
    """Find a date in the file name. Give None if there is no date."""
    today = today or dt.date.today()
    stem = PurePosixPath(name).stem
    if _PHOTOREC.match(stem):
        return None
    m = _FN_EPOCH.match(stem)
    if m:
        n = int(m.group(1))
        secs = n / 1000 if len(m.group(1)) == 13 else n
        try:
            d = dt.datetime.fromtimestamp(secs, dt.UTC).date()
        except (OverflowError, OSError, ValueError):
            d = None
        if d and _valid(d.year, d.month, d.day, min_year=max(min_year, 2000), today=today):
            return DateInfo(d.year, d.month, d.day, "filename")
    for m in _FN_YMD.finditer(stem):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid(y, mo, d, min_year=min_year, today=today):
            return DateInfo(y, mo, d, "filename")
    for m in _FN_DMY.finditer(stem):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid(y, mo, d, min_year=min_year, today=today):
            return DateInfo(y, mo, d, "filename")
    return None


# ---------------------------------------------------------------------------
# Folder name dates
# ---------------------------------------------------------------------------
_DIR_YM = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_. ](0[1-9]|1[0-2])(?!\d)")
_DIR_MONTH_YEAR = re.compile(rf"(?<![a-z])({_MONTH_RE})(?![a-z])[\s,._-]*((?:19|20)\d{{2}})(?!\d)", re.I)
_DIR_YEAR_MONTH = re.compile(rf"(?<!\d)((?:19|20)\d{{2}})[\s,._-]*({_MONTH_RE})(?![a-z])", re.I)
_DIR_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_DIR_MONTH_ONLY = re.compile(r"^(0[1-9]|1[0-2])(?:[\s._-].*)?$")


def date_from_folders(rel_parent: str, *, min_year: int = 1990, today: dt.date | None = None,
                      max_levels: int = 4) -> DateInfo | None:
    """Find a year (and a month, if possible) in the nearest folder names, deepest first.
    Examples: 'Goa Trip 2015', '2015-06 Goa', 'June 2015', '2015/06', '2015/June'."""
    today = today or dt.date.today()
    parts = [p for p in PurePosixPath(rel_parent).parts if p not in ("", ".", "/")]
    parts = parts[-max_levels:]
    for idx in range(len(parts) - 1, -1, -1):
        name = parts[idx]
        full = _FN_YMD.search(name)
        if full:
            y, mo, d = int(full.group(1)), int(full.group(2)), int(full.group(3))
            if _valid(y, mo, d, min_year=min_year, today=today):
                return DateInfo(y, mo, d, "folder")
        for rx, yi, mi in ((_DIR_YM, 1, 2), (_DIR_MONTH_YEAR, 2, 1), (_DIR_YEAR_MONTH, 1, 2)):
            m = rx.search(name)
            if m:
                y = int(m.group(yi))
                mraw = m.group(mi).lower()
                mo = int(mraw) if mraw.isdigit() else MONTHS.get(mraw[:3]) or MONTHS.get(mraw)
                if mo and _valid(y, mo, min_year=min_year, today=today):
                    return DateInfo(y, mo, None, "folder")
        m = _DIR_YEAR.search(name)
        if m and _valid(int(m.group(1)), min_year=min_year, today=today):
            y = int(m.group(1))
            mo = None
            # "2015/06" or "2015/June": the month is the next folder down
            if idx + 1 < len(parts):
                child = parts[idx + 1]
                mm = _DIR_MONTH_ONLY.match(child)
                if mm:
                    mo = int(mm.group(1))
                else:
                    key = re.split(r"[\s._-]", child.lower())[0]
                    mo = MONTHS.get(key)
            if mo and not _valid(y, mo, min_year=min_year, today=today):
                mo = None
            return DateInfo(y, mo, None, "folder")
    return None


# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------
GENERIC_FOLDER_NAMES = {
    "dcim", "camera", "camera roll", "pictures", "my pictures", "photos", "my photos",
    "images", "image", "img", "pics", "videos", "my videos", "movies", "video",
    "downloads", "download", "desktop", "documents", "my documents", "new folder",
    "untitled folder", "misc", "miscellaneous", "other", "others", "stuff", "temp", "tmp",
    "backup", "backups", "old", "new", "files", "data", "media", "whatsapp", "whatsapp images",
    "whatsapp video", "whatsapp animated gifs", "sent", "private", "screenshots", "screenshot",
    "recovered", "recovered files", "recovered data", "found", "lost", "lost files",
    "raw", "jpg", "jpeg", "png", "mp4", "mov", "gif", "heic", "100apple", "thumbnails",
    ".thumbnails", "lost+found", "users", "user", "home", "volumes", "telegram", "instagram",
    "facebook", "snapseed", "edited", "export", "exports", "import", "imports", "iphone",
    "android", "phone", "mobile", "sdcard", "sd card", "internal storage", "storage",
    "copy", "copies", "duplicates", "unsorted", "sorted", "all", "pictures library",
    "unknown", "untitled", "_organized", "organized", "thumbs", "thumb", "thumbnail", "cache",
    "caches", "previews", "preview", "trash", "deleted", "restored", "recovery",
    "files lost original directory", "lost original directory", "found files", "reconstructed",
    "reconstructed files", "more lost files", "existing files", "deleted files",
}
_GENERIC_PATTERNS = [
    re.compile(r"^\d{3}[a-z_]{0,8}$", re.I),           # 100CANON, 101MSDCF, 100_FUJI
    re.compile(r"^recup_dir(\.\d+)?$", re.I),          # PhotoRec
    re.compile(r"^found\.\d+$", re.I),                 # chkdsk
    re.compile(r"^(dir|folder|new folder|untitled folder)\s*\(?\d*\)?$", re.I),
    re.compile(r"^[\d\s\-_.()]+$"),                    # pure numbers / dates
    re.compile(r"^(img|dsc|dscn|dcim|pict|mvi|vid)[_\-\s]?\d*$", re.I),
    re.compile(rf"^({_MONTH_RE})$", re.I),             # bare month names
    re.compile(r"^\$", re.I),
    re.compile(r"^(lost partition|deep scan|raw files|orphan).*", re.I),
]


def clean_event_name(name: str) -> str:
    """Remove dates, years and separators from a folder name: '2015-06 Goa Trip' -> 'Goa Trip'."""
    s = _FN_YMD.sub(" ", name)
    s = _DIR_MONTH_YEAR.sub(" ", s)
    s = _DIR_YEAR_MONTH.sub(" ", s)
    s = _DIR_YM.sub(" ", s)
    s = _DIR_YEAR.sub(" ", s)
    s = re.sub(r"[_]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" -_.,()[]")
    s = re.sub(r"[\\/:*?\"<>|]", "", s)  # remove characters that FAT and NTFS do not allow
    return s[:80].strip()


def is_meaningful_folder(name: str, extra_generic: set[str] | frozenset[str] = frozenset()) -> bool:
    """True if the folder name is a real name (an event), not a general name like "DCIM"."""
    low = name.strip().lower()
    if not low or low.startswith("."):
        return False
    if low in GENERIC_FOLDER_NAMES or low in extra_generic:
        return False
    if any(p.match(name.strip()) for p in _GENERIC_PATTERNS):
        return False
    cleaned = clean_event_name(name)
    if len(cleaned) < 2 or not re.search(r"[A-Za-zÀ-￿]{2,}", cleaned):
        return False
    low_clean = cleaned.lower()
    if low_clean in GENERIC_FOLDER_NAMES or low_clean in extra_generic:
        return False
    if any(p.match(cleaned) for p in _GENERIC_PATTERNS):
        return False
    return True


# Folders that a camera, a phone or a date sort makes *inside* an event folder.
# The event search can go up past these folders. It stops at all other general folders.
_STRUCTURE_PATTERNS = [
    re.compile(r"^\d{3}[a-z_]{0,8}$", re.I),                 # 100CANON, 101APPLE
    re.compile(r"^(dcim|camera|camera roll|private|avchd|bdmv|stream|clip|misc)$", re.I),
    re.compile(r"^[\d\s\-_.]+$"),                             # 2015, 06, 2015-06-12
    re.compile(rf"^(\d{{1,2}}[\s._-]*)?({_MONTH_RE})$", re.I),  # June, 06 June
]


def _is_structure_folder(name: str) -> bool:
    return any(p.match(name.strip()) for p in _STRUCTURE_PATTERNS)


def event_name_for(rel_parent: str, extra_generic: set[str] | frozenset[str] = frozenset(),
                   max_levels: int = 4) -> str | None:
    """Give the nearest folder name that is a real event name.
    The search goes up past camera and date folders ('Goa Trip 2015/DCIM/100CANON' -> 'Goa Trip',
    'Trip/2015/06' -> 'Trip'). It stops at other general folders ('My Laptop/New Folder' -> None)."""
    parts = [p for p in PurePosixPath(rel_parent).parts if p not in ("", ".", "/")]
    for name in list(reversed(parts))[:max_levels]:
        if is_meaningful_folder(name, extra_generic):
            return clean_event_name(name)
        if not _is_structure_folder(name):
            return None
    return None


# ---------------------------------------------------------------------------
# Find the best date
# ---------------------------------------------------------------------------
def resolve_date(
    *,
    rel_path: str,
    meta_date: str | None,
    meta_source: str | None,
    mtime_ns: int,
    min_year: int,
    use_mtime: bool,
    ignore_mtime_after: dt.date | None,
    today: dt.date | None = None,
    ignore_mtime_dates: frozenset[dt.date] | set[dt.date] = frozenset(),
) -> DateInfo:
    """Find the best date for a file. Use the first source that has a date:
    1. metadata (EXIF or video), 2. file name, 3. folder names, 4. modified time."""
    today = today or dt.date.today()
    p = PurePosixPath(rel_path)
    if meta_date:
        try:
            d = dt.datetime.fromisoformat(meta_date).date()
            if _valid(d.year, d.month, d.day, min_year=min_year, today=today):
                return DateInfo(d.year, d.month, d.day, meta_source or "metadata")
        except ValueError:
            pass
    fn = date_from_filename(p.name, min_year=min_year, today=today)
    if fn:
        return fn
    fo = date_from_folders(str(p.parent), min_year=min_year, today=today)
    if fo:
        return fo
    if use_mtime and mtime_ns:
        try:
            d = dt.datetime.fromtimestamp(mtime_ns / 1e9).date()
        except (OverflowError, OSError, ValueError):
            d = None
        if d and _valid(d.year, d.month, d.day, min_year=min_year, today=today):
            if (ignore_mtime_after is None or d < ignore_mtime_after) and d not in ignore_mtime_dates:
                return DateInfo(d.year, d.month, d.day, "mtime")
    return UNKNOWN
