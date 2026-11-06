import os

import pytest

from diskcleaner.config import ConfigError, load_config
from diskcleaner.safeops import (
    Manifest,
    SafetyError,
    check_writable,
    read_manifest,
    resolve_inside,
    safe_copy,
    safe_move,
    unique_path,
)
from tests.conftest import write_config


def test_unique_path(tmp_path):
    (tmp_path / "a.jpg").write_text("x")
    assert unique_path(tmp_path / "a.jpg").name == "a (2).jpg"
    assert unique_path(tmp_path / "b.jpg").name == "b.jpg"
    assert unique_path(tmp_path / "b.jpg", {str(tmp_path / "B.JPG").lower()}).name == "b (2).jpg"


def test_safe_move_never_overwrites(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("A")
    b.write_text("B")
    with pytest.raises(SafetyError):
        safe_move(a, b)
    assert a.read_text() == "A" and b.read_text() == "B"
    safe_move(a, tmp_path / "sub" / "c")
    assert (tmp_path / "sub" / "c").read_text() == "A" and not a.exists()


def test_safe_copy_verifies(tmp_path):
    a = tmp_path / "a"
    a.write_bytes(os.urandom(5000))
    safe_copy(a, tmp_path / "x" / "b")
    assert (tmp_path / "x" / "b").read_bytes() == a.read_bytes()
    assert not list(tmp_path.rglob("*.diskcleaner-partial"))


def test_resolve_inside(tmp_path):
    assert resolve_inside(tmp_path, "a/b.jpg") == tmp_path / "a/b.jpg"
    for bad in ("../x", "/etc/passwd", "a/../../x", ""):
        with pytest.raises(SafetyError):
            resolve_inside(tmp_path, bad)


def test_manifest_written_incrementally(cfg):
    m = Manifest(cfg, "test")
    m.planned("move", "a", "b")
    # readable before close (fsync'd) - survives a crash
    header, ops = read_manifest(m.path)
    assert header["action"] == "test" and ops[0]["src"] == "a"
    m.done("move", "a", "b")
    m.close()
    assert len(read_manifest(m.path)[1]) == 1


def test_read_only_disk_detected(cfg):
    os.chmod(cfg.root, 0o555)
    try:
        if os.access(cfg.root, os.W_OK):  # running as root - can't simulate
            pytest.skip("cannot simulate read-only as this user")
        with pytest.raises(SafetyError, match="not writable"):
            check_writable(cfg)
    finally:
        os.chmod(cfg.root, 0o755)


def test_config_validation(tmp_path):
    root = tmp_path / "disk"
    root.mkdir()
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="set \\[disk\\].root"):
        load_config(write_config(tmp_path, "/Volumes/CHANGE_ME"))
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(write_config(tmp_path, tmp_path / "missing"))
    with pytest.raises(ConfigError, match="move' or 'copy"):
        load_config(write_config(tmp_path, root, organize='mode = "shred"'))
    with pytest.raises(ConfigError, match="YYYY|2026-10-31"):
        load_config(write_config(tmp_path, root, organize='ignore_mtime_after = "yesterday"'))
    with pytest.raises(ConfigError, match="quarantine_dir"):
        load_config(write_config(tmp_path, root, extra='[safety]\nquarantine_dir = "../out"'))
    cfg = load_config(write_config(tmp_path, root))
    assert cfg.quarantine_path == root.resolve() / "_DiskCleaner/Quarantine"


def test_example_config_is_valid(tmp_path):
    from pathlib import Path

    example = (Path(__file__).resolve().parent.parent / "config.example.toml").read_text()
    root = tmp_path / "disk"
    root.mkdir()
    p = tmp_path / "c.toml"
    p.write_text(example.replace("/Volumes/CHANGE_ME", str(root)))
    cfg = load_config(p)
    assert cfg.organize_categories == ["photos", "videos"] and cfg.similar_action == "REVIEW"
