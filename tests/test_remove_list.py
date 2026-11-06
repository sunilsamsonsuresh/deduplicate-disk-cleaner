"""remove-list: move the files named in a text list to quarantine (e.g. downloaded movies)."""

from diskcleaner import hashing
from diskcleaner.cli import main
from diskcleaner.safeops import undo
from diskcleaner.dedupe import quarantine_list, read_path_list


def _snapshot(root):
    return {p.relative_to(root).as_posix(): hashing.full_hash(p)
            for p in root.rglob("*") if p.is_file() and "_DiskCleaner" not in p.parts}


def test_listed_files_go_to_quarantine_and_undo_brings_them_back(cfg, tmp_path):
    before = _snapshot(cfg.root)
    listing = tmp_path / "movies.txt"
    listing.write_text("# my list\nDCIM/100CANON/MVI_0001.mp4\n\nDocs/report.pdf\nDocs/report.pdf\n")
    paths = read_path_list(listing)
    assert paths == ["DCIM/100CANON/MVI_0001.mp4", "Docs/report.pdf"]  # comments, blanks, repeats dropped

    res = quarantine_list(cfg, paths)
    assert res.moved == 2 and not res.skipped
    assert not (cfg.root / "Docs/report.pdf").exists()
    assert (res.quarantine_run / "Docs/report.pdf").is_file()
    assert (cfg.root / "Camera/IMG_20170812_101500.jpg").is_file()  # not listed: untouched

    u = undo(cfg, res.manifest)
    assert u.restored == 2 and not u.problems
    assert _snapshot(cfg.root) == before


def test_missing_and_outside_paths_are_skipped(cfg):
    res = quarantine_list(cfg, ["no/such/file.mkv", "../outside.mkv", "Docs/report.pdf"])
    assert res.moved == 1
    reasons = " ".join(res.skipped)
    assert "no longer exists" in reasons and len(res.skipped) == 2


def test_cli_dry_run_changes_nothing_then_apply(disk, tmp_path, capsys):
    root, cfg_path = disk
    listing = tmp_path / "movies.txt"
    listing.write_text("DCIM/100CANON/MVI_0001.mp4\n")
    c = ["-c", str(cfg_path), "remove-list", str(listing)]

    assert main(c) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert (root / "DCIM/100CANON/MVI_0001.mp4").is_file()

    assert main(c + ["--apply", "--yes"]) == 0
    assert "Moved 1 files" in capsys.readouterr().out
    assert not (root / "DCIM/100CANON/MVI_0001.mp4").exists()
