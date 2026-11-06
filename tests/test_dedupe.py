import csv
from pathlib import Path

import pytest

from diskcleaner import hashing, reports
from diskcleaner.dedupe import apply_plan, load_plan
from diskcleaner.duplicates import find_duplicates
from diskcleaner.index import Index
from diskcleaner.safeops import SafetyError, read_manifest, undo
from diskcleaner.scanner import scan


def _report(cfg) -> Path:
    cfg.ensure_state_dirs()
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        res = find_duplicates(cfg, idx)
    p = cfg.reports_dir / "dups.csv"
    reports.write_duplicates_csv(res, p)
    return p


def _edit_csv(path: Path, fn) -> None:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0].keys())
    rows = [fn(r) or r for r in rows]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _snapshot(root: Path) -> dict:
    """relative path -> content hash, ignoring the tool's own folder."""
    return {
        p.relative_to(root).as_posix(): hashing.full_hash(p)
        for p in root.rglob("*") if p.is_file() and "_DiskCleaner" not in p.parts
    }


def test_dedupe_moves_to_quarantine_and_keeps_one_copy(cfg):
    root = cfg.root
    before = _snapshot(root)
    plan = load_plan(_report(cfg), cfg)
    assert {r.path for r in plan.removals} == {
        "recup_dir.1/f1234567.jpg", "Recovered/Goa Trip 2015/IMG_0001.jpg",
        "Docs/report - Copy.pdf", "Stripped/IMG_0001.jpg",
    }
    # REVIEW (similar) files are never in the plan
    assert "Thumbs/IMG_0001_small.jpg" not in {r.path for r in plan.removals}

    res = apply_plan(cfg, plan)
    assert res.moved == 4 and not res.skipped
    after = _snapshot(root)
    # every distinct content that existed before still exists somewhere outside quarantine...
    lost = set(before.values()) - set(after.values())
    # ...except the stripped copy whose *pixels* are kept in the EXIF original
    assert lost == {before["Stripped/IMG_0001.jpg"]}
    assert (root / "Goa Trip 2015/IMG_0001.jpg").exists()
    # files were moved (not deleted) with their relative path preserved
    q = res.quarantine_run
    assert (q / "recup_dir.1/f1234567.jpg").exists()
    assert (q / "Docs/report - Copy.pdf").read_bytes() == (root / "Docs/report.pdf").read_bytes()
    header, ops = read_manifest(res.manifest)
    assert header["action"] == "dedupe" and len(ops) == 4


def test_undo_restores_everything(cfg):
    before = _snapshot(cfg.root)
    res = apply_plan(cfg, load_plan(_report(cfg), cfg))
    assert res.moved == 4
    u = undo(cfg, res.manifest)
    assert u.restored == 4 and not u.problems
    assert _snapshot(cfg.root) == before
    # empty quarantine folders were tidied away
    assert not any(p.is_file() for p in res.quarantine_run.rglob("*")) if res.quarantine_run.exists() else True
    # undoing twice is harmless
    u2 = undo(cfg, res.manifest)
    assert u2.restored == 0 and u2.already_in_place == 4


def test_file_changed_after_report_is_skipped(cfg):
    plan = load_plan(_report(cfg), cfg)
    # the user edits one "duplicate" after the report was written
    victim = cfg.root / "Docs/report - Copy.pdf"
    data = bytearray(victim.read_bytes())
    data[100_000] ^= 0xFF
    victim.write_bytes(bytes(data))
    res = apply_plan(cfg, plan)
    assert victim.exists()
    assert any("report - Copy.pdf" in s and "differs" in s for s in res.skipped)


def test_keeper_deleted_after_report_blocks_removal(cfg):
    plan = load_plan(_report(cfg), cfg)
    (cfg.root / "Docs/report.pdf").unlink()  # the copy we meant to keep vanished
    res = apply_plan(cfg, plan)
    assert (cfg.root / "Docs/report - Copy.pdf").exists()  # last copy is NOT moved
    assert any("kept copy no longer exists" in s for s in res.skipped)


def test_all_remove_group_is_refused(cfg):
    p = _report(cfg)
    _edit_csv(p, lambda r: r.update(action="REMOVE") if r["path"].startswith("Docs/") else None)
    plan = load_plan(p, cfg)
    assert not any(r.path.startswith("Docs/") for r in plan.removals)
    assert any("no copy would be kept" in g for g in plan.refused_groups)


def test_user_can_switch_keeper(cfg):
    p = _report(cfg)

    def swap(r):
        if r["path"] == "Docs/report.pdf":
            r["action"] = "REMOVE"
        elif r["path"] == "Docs/report - Copy.pdf":
            r["action"] = "KEEP"

    _edit_csv(p, swap)
    res = apply_plan(cfg, load_plan(p, cfg, {"exact"}))
    assert not (cfg.root / "Docs/report.pdf").exists()
    assert (cfg.root / "Docs/report - Copy.pdf").exists()
    assert res.moved == 3


def test_user_can_opt_in_similar(cfg):
    p = _report(cfg)
    _edit_csv(p, lambda r: r.update(action="REMOVE") if r["path"] == "Thumbs/IMG_0001_small.jpg" else None)
    res = apply_plan(cfg, load_plan(p, cfg, {"similar"}))
    assert res.moved == 1 and not (cfg.root / "Thumbs/IMG_0001_small.jpg").exists()


def test_tier_filter(cfg):
    plan = load_plan(_report(cfg), cfg, {"pixel"})
    assert [r.path for r in plan.removals] == ["Stripped/IMG_0001.jpg"]


def test_chained_keeper_across_tiers(cfg):
    """Exact group's keeper A is itself removed by the pixel tier (in favour of K).
    A's byte-copies must still be removable, and K must survive."""
    p = _report(cfg)
    # Make the stripped copy the pixel-tier keeper, so the exact-tier keeper is removed by tier 2.
    def flip(r):
        if r["tier"] == "pixel":
            r["action"] = "KEEP" if r["path"] == "Stripped/IMG_0001.jpg" else "REMOVE"

    _edit_csv(p, flip)
    plan = load_plan(p, cfg)
    assert not plan.refused_groups
    res = apply_plan(cfg, plan)
    assert not res.skipped, res.skipped
    assert (cfg.root / "Stripped/IMG_0001.jpg").exists()
    assert not (cfg.root / "Goa Trip 2015/IMG_0001.jpg").exists()
    assert not (cfg.root / "recup_dir.1/f1234567.jpg").exists()


def test_paths_outside_root_are_refused(cfg, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    p = cfg.reports_dir / "evil.csv"
    cfg.ensure_state_dirs()
    p.write_text(
        "group_id,tier,action,path,size_bytes\n"
        "E1,exact,KEEP,Docs/report.pdf,1\n"
        "E1,exact,REMOVE,../outside.txt,1\n"
        "E2,exact,KEEP,Docs/report.pdf,1\n"
        f"E2,exact,REMOVE,{outside},1\n",
        encoding="utf-8",
    )
    res = apply_plan(cfg, load_plan(p, cfg))
    assert res.moved == 0 and outside.read_text() == "precious"
    assert len(res.skipped) == 2


def test_bad_csv_rejected(cfg):
    cfg.ensure_state_dirs()
    p = cfg.reports_dir / "bad.csv"
    p.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(SafetyError):
        load_plan(p, cfg)


def test_unknown_action_refused(cfg):
    p = _report(cfg)
    _edit_csv(p, lambda r: r.update(action="DELETE") if r["path"] == "Docs/report - Copy.pdf" else None)
    plan = load_plan(p, cfg)
    assert not any(r.path.startswith("Docs/") for r in plan.removals)
    assert any("unknown action" in g for g in plan.refused_groups)
