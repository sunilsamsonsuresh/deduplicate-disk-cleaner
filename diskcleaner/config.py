"""Configuration: read config.toml, check every value, and give friendly errors."""

from __future__ import annotations

import datetime as _dt
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUDE = [
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", "$RECYCLE.BIN", "System Volume Information",
    ".DS_Store", "Thumbs.db", "desktop.ini",
]

DEFAULT_LOW_PRIORITY = [
    "recup_dir", "found.0", "lost files", "lost+found", "$recycle.bin",
    "recovered", "recovery", "orphan", "deep scan", "raw files", "lost partition",
    "lost dir", "file recovery", "/trash", ".chk",
    "files lost original directory", "lost original", "reconstructed", "/found files",
]

CATEGORIES = ("photos", "videos", "audio", "documents", "archives", "other")
DEFAULT_WORKERS = 4


class ConfigError(Exception):
    pass


@dataclass
class Config:
    root: Path
    state_dir: Path
    include: list[str] = field(default_factory=list)
    scope_folders: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))

    skip_hidden: bool = True
    min_size_bytes: int = 1
    follow_symlinks: bool = False
    workers: int = DEFAULT_WORKERS

    find_exact: bool = True
    find_pixel_identical: bool = True
    find_similar: bool = True
    similar_threshold: int = 6
    pixel_identical_action: str = "REMOVE"
    similar_action: str = "REVIEW"
    low_priority_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_LOW_PRIORITY))
    preferred_folders: list[str] = field(default_factory=list)

    quarantine_dir: str = "_DiskCleaner/Quarantine"
    verify_before_move: bool = True

    organize_destination: str = "_Organized"
    organize_categories: list[str] = field(default_factory=lambda: ["photos", "videos"])
    organize_mode: str = "move"
    organize_layout: str = "{category}/{year}/{month_dir}"
    organize_inside: list[str] = field(default_factory=list)
    organize_layout_inside: str = "{year}/{month_dir}"
    keep_event_names: bool = True
    use_file_mtime: bool = True
    ignore_mtime_after: _dt.date | None = None
    ignore_mtime_dates: set = field(default_factory=set)
    min_year: int = 1990
    generic_folder_names: list[str] = field(default_factory=list)

    # ---- paths ---------------------------------------------------------
    @property
    def quarantine_path(self) -> Path:
        return self.root / self.quarantine_dir

    @property
    def organize_path(self) -> Path:
        return self.root / self.organize_destination

    @property
    def db_path(self) -> Path:
        return self.state_dir / "index.sqlite3"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def manifests_dir(self) -> Path:
        return self.state_dir / "manifests"

    def ensure_state_dirs(self) -> None:
        for p in (self.state_dir, self.reports_dir, self.manifests_dir):
            p.mkdir(parents=True, exist_ok=True)

    def always_excluded_relpaths(self) -> list[str]:
        """Folders (relative to root) the scanner must never go into."""
        out = [self.quarantine_dir.strip("/")]
        try:
            out.append(str(self.state_dir.resolve().relative_to(self.root.resolve())))
        except ValueError:
            pass
        return out


# Every simple setting: (toml section, toml key, Config attribute, type).
# Defaults come from the Config class above, so they live in one place only.
SETTINGS = [
    ("disk", "include", "include", list),
    ("disk", "exclude", "exclude", list),
    ("scope", "folders", "scope_folders", list),
    ("scan", "skip_hidden", "skip_hidden", bool),
    ("scan", "min_size_bytes", "min_size_bytes", int),
    ("scan", "follow_symlinks", "follow_symlinks", bool),
    ("scan", "workers", "workers", int),
    ("duplicates", "find_exact", "find_exact", bool),
    ("duplicates", "find_pixel_identical", "find_pixel_identical", bool),
    ("duplicates", "find_similar", "find_similar", bool),
    ("duplicates", "similar_threshold", "similar_threshold", int),
    ("duplicates", "pixel_identical_action", "pixel_identical_action", str),
    ("duplicates", "similar_action", "similar_action", str),
    ("duplicates", "low_priority_patterns", "low_priority_patterns", list),
    ("duplicates", "preferred_folders", "preferred_folders", list),
    ("safety", "quarantine_dir", "quarantine_dir", str),
    ("safety", "verify_before_move", "verify_before_move", bool),
    ("organize", "destination", "organize_destination", str),
    ("organize", "categories", "organize_categories", list),
    ("organize", "mode", "organize_mode", str),
    ("organize", "layout", "organize_layout", str),
    ("organize", "organize_inside", "organize_inside", list),
    ("organize", "layout_inside", "organize_layout_inside", str),
    ("organize", "keep_event_names", "keep_event_names", bool),
    ("organize", "use_file_mtime", "use_file_mtime", bool),
    ("organize", "ignore_mtime_after", None, str),   # parsed below
    ("organize", "ignore_mtime_dates", None, list),  # parsed below
    ("organize", "min_year", "min_year", int),
    ("organize", "generic_folder_names", "generic_folder_names", list),
]


def _check_type(key: str, val, typ):
    ok = {
        bool: isinstance(val, bool),
        int: isinstance(val, int) and not isinstance(val, bool),
        str: isinstance(val, str),
        list: isinstance(val, list) and all(isinstance(x, str) for x in val),
    }[typ]
    if not ok:
        what = {bool: "true or false", int: "an integer", str: "a string", list: "a list of strings"}[typ]
        raise ConfigError(f"'{key}' must be {what}, got {val!r}")
    return val


def _check_layout(name: str, layout: str) -> None:
    try:
        layout.format(category="x", year="x", month_dir="x")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"[organize].{name} has an unknown placeholder: {exc}") from exc


def _relative_folder(name: str, value: str) -> str:
    value = value.strip("/")
    if not value or ".." in Path(value).parts:
        raise ConfigError(f"{name} must be a relative folder inside the disk root")
    return value


def load_config(path: str | os.PathLike, *, require_root_exists: bool = True) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            "Create one with:  cp config.example.toml config.toml   and set [disk].root"
        )
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    # ---- the disk and the state folder ---------------------------------
    root_s = _check_type("root", raw.get("disk", {}).get("root", ""), str)
    if not root_s or "CHANGE_ME" in root_s:
        raise ConfigError("Please set [disk].root in your config to your external disk path, e.g. /Volumes/MyDisk")
    root = Path(root_s).expanduser()
    if require_root_exists and not root.is_dir():
        raise ConfigError(f"[disk].root does not exist or is not a folder: {root}  (is the disk plugged in?)")
    root = root.resolve() if root.exists() else root

    state_dir = Path(_check_type("dir", raw.get("state", {}).get("dir", "./state"), str)).expanduser()
    if not state_dir.is_absolute():  # relative to the config file's folder
        state_dir = (path.parent / state_dir).resolve()

    # ---- every other setting -------------------------------------------
    cfg = Config(root=root, state_dir=state_dir)
    org = raw.get("organize", {})
    for section, key, attr, typ in SETTINGS:
        values = raw.get(section, {})
        if key in values:
            val = _check_type(key, values[key], typ)
            if attr:
                setattr(cfg, attr, val)

    # ---- extra checks and normalising ----------------------------------
    if not 1 <= cfg.workers <= 32:
        raise ConfigError("[scan].workers must be between 1 and 32")
    if not 0 <= cfg.similar_threshold <= 32:
        raise ConfigError("similar_threshold must be between 0 and 32")
    for key in ("pixel_identical_action", "similar_action"):
        val = getattr(cfg, key).upper()
        if val not in ("REMOVE", "REVIEW"):
            raise ConfigError(f"{key} must be REMOVE or REVIEW")
        setattr(cfg, key, val)
    cfg.low_priority_patterns = [p.lower() for p in cfg.low_priority_patterns]
    cfg.preferred_folders = [p.strip("/") for p in cfg.preferred_folders]

    cfg.quarantine_dir = _relative_folder("quarantine_dir", cfg.quarantine_dir)
    cfg.organize_destination = _relative_folder("[organize].destination", cfg.organize_destination)
    if cfg.organize_destination.lower() == cfg.quarantine_dir.lower():
        raise ConfigError("organize destination and quarantine_dir must differ")

    cfg.organize_categories = [c.lower() for c in cfg.organize_categories]
    bad = [c for c in cfg.organize_categories if c not in CATEGORIES]
    if bad:
        raise ConfigError(f"Unknown organize categories {bad}; allowed: {list(CATEGORIES)}")
    cfg.organize_mode = cfg.organize_mode.lower()
    if cfg.organize_mode not in ("move", "copy"):
        raise ConfigError("[organize].mode must be 'move' or 'copy'")
    _check_layout("layout", cfg.organize_layout)
    _check_layout("layout_inside", cfg.organize_layout_inside)
    for e in cfg.organize_inside:
        if ".." in Path(e).parts or e.strip().startswith("/"):
            raise ConfigError(f"organize_inside entries must be folders relative to the disk root: {e!r}")

    ima = org.get("ignore_mtime_after", "").strip()
    if ima:
        try:
            cfg.ignore_mtime_after = _dt.date.fromisoformat(ima)
        except ValueError as exc:
            raise ConfigError("ignore_mtime_after must look like 2026-10-31") from exc
    try:
        cfg.ignore_mtime_dates = {_dt.date.fromisoformat(x.strip()) for x in org.get("ignore_mtime_dates", [])}
    except ValueError as exc:
        raise ConfigError("ignore_mtime_dates must be a list like [\"2015-10-21\"]") from exc
    cfg.generic_folder_names = [g.lower() for g in cfg.generic_folder_names]
    return cfg
