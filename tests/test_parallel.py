"""Parallel hashing: same results whatever the number of workers."""

import time

import pytest

from diskcleaner import reports, helpers
from diskcleaner.config import ConfigError, load_config
from diskcleaner.find_duplicates import find_duplicates
from diskcleaner.file_index import Index
from diskcleaner.scan_disk import scan
from tests.conftest import write_config


def test_parallel_map_keeps_order_and_reports_errors():
    def work(n):
        time.sleep(0.002 * (10 - n))  # later items finish first
        if n == 3:
            raise ValueError("bad sector")
        return n * n

    out = list(helpers.parallel_map(work, range(10), workers=4))
    assert [item for item, _, _ in out] == list(range(10))
    assert [v for _, v, e in out if e is None] == [n * n for n in range(10) if n != 3]
    (bad,) = [e for _, _, e in out if e is not None]
    assert isinstance(bad, ValueError)
    assert list(helpers.parallel_map(work, [], workers=4)) == []


def _config(folder, root, extra=""):
    folder.mkdir()
    return write_config(folder, root, extra=extra)


def _report(cfg, tmp_path, name):
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        res = find_duplicates(cfg, idx)
    p = tmp_path / name
    reports.write_duplicates_csv(res, p)
    return p.read_text()


def test_workers_do_not_change_results(disk, tmp_path):
    root, _ = disk
    one = load_config(_config(tmp_path / "a", root, extra="[scan]\nworkers = 1\n"))
    many = load_config(_config(tmp_path / "b", root, extra="[scan]\nworkers = 8\n"))
    assert one.workers == 1 and many.workers == 8
    assert one.db_path != many.db_path  # each starts with an empty index
    assert _report(one, tmp_path, "one.csv") == _report(many, tmp_path, "many.csv")


def test_workers_setting_is_checked(disk, tmp_path):
    root, _ = disk
    with pytest.raises(ConfigError, match="workers"):
        load_config(_config(tmp_path / "c", root, extra="[scan]\nworkers = 0\n"))
    assert load_config(_config(tmp_path / "d", root)).workers == 4  # old configs: default


# ---- the disk mounted under a new name (e.g. /Volumes/Untitled -> /Volumes/disk4s1) ----
def _hash_count(cfg):
    with Index(cfg.db_path) as idx:
        return idx.conn.execute("SELECT COUNT(*) FROM files WHERE full_hash IS NOT NULL").fetchone()[0]


def test_same_disk_at_new_location_keeps_cached_hashes(disk, tmp_path):
    root, cfg_path = disk
    cfg = load_config(cfg_path)
    _report(cfg, tmp_path, "before.csv")
    cached = _hash_count(cfg)
    assert cached > 0

    moved = root.rename(root.parent / "disk4s1")  # same files, new mount point
    cfg_path.write_text(cfg_path.read_text().replace(str(root), str(moved)))
    cfg = load_config(cfg_path)
    with Index(cfg.db_path) as idx:
        res = scan(cfg, idx)
        assert idx.get_meta("root") == str(moved)
    assert res.new_or_changed == 0 and _hash_count(cfg) == cached


def test_different_disk_starts_fresh(disk, tmp_path):
    root, cfg_path = disk
    cfg = load_config(cfg_path)
    _report(cfg, tmp_path, "before.csv")
    other = tmp_path / "OtherDisk"
    other.mkdir()
    (other / "something.txt").write_text("a different disk")
    cfg_path.write_text(cfg_path.read_text().replace(str(root), str(other)))
    cfg = load_config(cfg_path)
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        assert [r.path for r in idx.all_files()] == ["something.txt"]
    assert _hash_count(cfg) == 0


def test_file_left_out_of_listing_is_not_forgotten(disk, tmp_path, monkeypatch):
    """Some NTFS drivers drop the odd file from folder listings; it is still there."""
    from diskcleaner import scan_disk

    root, cfg_path = disk
    cfg = load_config(cfg_path)
    _report(cfg, tmp_path, "before.csv")
    hidden = "Goa Trip 2015/IMG_0001.jpg"
    before = Index(cfg.db_path).get(hidden)
    assert before.full_hash

    real = scan_disk._list_files
    monkeypatch.setattr(scan_disk, "_list_files", lambda c, e: [p for p in real(c, e) if p != hidden])
    (root / "Docs/report.pdf").unlink()  # really deleted: must be forgotten
    with Index(cfg.db_path) as idx:
        res = scan(cfg, idx)
        assert res.not_listed == 1 and res.removed_from_index == 1
        after = idx.get(hidden)
        assert after is not None and after.full_hash == before.full_hash
        assert idx.get("Docs/report.pdf") is None
