"""The index: a SQLite database with one row for each file on the disk.

The paths are relative to the disk root (with "/" between folders).
Thus the index stays correct if the disk gets a different mount name.
The index keeps the hashes and the metadata. The tool calculates them again
only when the size or the modified time of a file changes.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    ext          TEXT NOT NULL,
    category     TEXT NOT NULL,
    width        INTEGER,
    height       INTEGER,
    meta_date    TEXT,
    meta_date_source TEXT,
    has_exif     INTEGER NOT NULL DEFAULT 0,
    meta_done    INTEGER NOT NULL DEFAULT 0,
    meta_error   TEXT,
    partial_hash TEXT,
    full_hash    TEXT,
    pixel_hash   TEXT,
    dhash        TEXT,
    scan_id      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_full ON files(full_hash);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class FileRow:
    path: str
    size: int
    mtime_ns: int
    ext: str
    category: str
    width: int | None
    height: int | None
    meta_date: str | None
    meta_date_source: str | None
    has_exif: bool
    meta_done: bool
    meta_error: str | None
    partial_hash: str | None
    full_hash: str | None
    pixel_hash: str | None
    dhash: str | None
    scan_id: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "FileRow":
        d = dict(r)
        d["has_exif"] = bool(d["has_exif"])
        d["meta_done"] = bool(d["meta_done"])
        return cls(**d)

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


class Index:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- meta --------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    # ---- files -------------------------------------------------------
    def get(self, path: str) -> FileRow | None:
        r = self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return FileRow.from_row(r) if r else None

    def all_files(self) -> list[FileRow]:
        return [FileRow.from_row(r) for r in self.conn.execute("SELECT * FROM files ORDER BY path")]

    def upsert_stat(self, path: str, size: int, mtime_ns: int, ext: str, category: str, scan_id: int) -> bool:
        """Add or update the size and the time of a file.
        Return True if the file is new or changed. Then its old hashes and metadata are removed."""
        cur = self.conn.execute("SELECT size, mtime_ns, ext FROM files WHERE path=?", (path,)).fetchone()
        if cur and cur["size"] == size and cur["mtime_ns"] == mtime_ns and cur["ext"] == ext:
            self.conn.execute("UPDATE files SET scan_id=? WHERE path=?", (scan_id, path))
            return False
        self.conn.execute(
            """INSERT OR REPLACE INTO files(path, size, mtime_ns, ext, category, scan_id)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (path, size, mtime_ns, ext, category, scan_id),
        )
        return True

    def set_metadata(self, path: str, *, width, height, meta_date, meta_date_source, has_exif, error) -> None:
        self.conn.execute(
            """UPDATE files SET width=?, height=?, meta_date=?, meta_date_source=?, has_exif=?,
               meta_error=?, meta_done=1 WHERE path=?""",
            (width, height, meta_date, meta_date_source, int(bool(has_exif)), error, path),
        )

    def set_hash(self, path: str, column: str, value: str | None) -> None:
        if column not in ("partial_hash", "full_hash", "pixel_hash", "dhash"):
            raise ValueError(column)
        self.conn.execute(f"UPDATE files SET {column}=? WHERE path=?", (value, path))

    def delete_not_seen(self, scan_id: int) -> int:
        cur = self.conn.execute("DELETE FROM files WHERE scan_id<>?", (scan_id,))
        return cur.rowcount

    def rename(self, old: str, new: str) -> None:
        """Change the path of a moved file. Keep its hashes and metadata (a move does not change the content)."""
        self.conn.execute("DELETE FROM files WHERE path=?", (new,))
        self.conn.execute("UPDATE files SET path=? WHERE path=?", (new, old))

    def remove(self, path: str) -> None:
        self.conn.execute("DELETE FROM files WHERE path=?", (path,))

    def commit(self) -> None:
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
