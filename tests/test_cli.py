"""End-to-end runs through the command line entry point."""

import builtins
import sys

from diskcleaner import hashing
from diskcleaner.cli import main


def _contents(root):
    """Content hashes of the user's files (ignoring tool data, the hidden fixture
    and 0-byte files, which are deliberately never treated as duplicates)."""
    return sorted(
        hashing.full_hash(p) for p in root.rglob("*")
        if p.is_file() and p.stat().st_size and "_DiskCleaner" not in p.parts and ".hidden" not in p.parts
    )


def test_full_workflow(disk, capsys, monkeypatch):
    root, cfg_path = disk
    c = ["-c", str(cfg_path)]
    original_contents = set(_contents(root))

    assert main(c + ["scan"]) == 0
    assert main(c + ["status"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-01" in out  # the recovery-day hint

    assert main(c + ["duplicates"]) == 0
    out = capsys.readouterr().out
    assert "Proposed for removal: 4 files" in out

    # dry run changes nothing
    before = _contents(root)
    assert main(c + ["dedupe"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert _contents(root) == before

    # no terminal and no --yes -> refuses
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert main(c + ["dedupe", "--apply"]) == 1
    assert _contents(root) == before

    assert main(c + ["dedupe", "--apply", "--yes"]) == 0
    assert not (root / "recup_dir.1/f1234567.jpg").exists()
    assert (root / "Goa Trip 2015/IMG_0001.jpg").exists()

    # organize: plan, then apply
    assert main(c + ["organize"]) == 0
    assert "Organize plan" in capsys.readouterr().out
    assert main(c + ["organize", "--apply", "--yes"]) == 0
    assert (root / "_Organized/Photos/2015/2015-06 Goa Trip/IMG_0001.jpg").exists()
    assert (root / "_Organized/Videos/2018/2018-12/MVI_0001.mp4").exists()

    assert main(c + ["clean-empty-dirs", "--apply", "--yes"]) == 0
    assert not (root / "Camera").exists()

    # undo both operations (newest first) -> back to the duplicate-free original layout
    assert main(c + ["undo"]) == 0
    assert main(c + ["undo", "last", "--apply", "--yes"]) == 0
    assert (root / "Camera/IMG_20170812_101500.jpg").exists()

    # purge requires typing PURGE (no --yes shortcut)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda *_: "yes")
    assert main(c + ["purge", "--apply"]) == 1
    assert any((root / "_DiskCleaner/Quarantine").rglob("*.jpg"))
    monkeypatch.setattr(builtins, "input", lambda *_: "PURGE")
    assert main(c + ["purge", "--apply"]) == 0
    assert not any((root / "_DiskCleaner/Quarantine").rglob("*.jpg"))

    # every distinct picture/document still exists exactly once (pixel dup aside)
    remaining = _contents(root)
    assert len(remaining) == len(set(remaining))
    assert len(original_contents - set(remaining)) == 1  # only the EXIF-less pixel copy


def test_init_and_missing_config(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    assert main(["-c", str(cfg), "scan"]) == 2
    assert "cp config.example.toml" in capsys.readouterr().err
    assert main(["-c", str(cfg), "init", "--root", str(tmp_path)]) == 0
    assert f'root = "{tmp_path}"' in cfg.read_text()
    assert main(["-c", str(cfg), "init"]) == 1  # never overwrites
