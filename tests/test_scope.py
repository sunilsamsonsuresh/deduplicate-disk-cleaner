"""Folder-by-folder runs and the JSON 'where else does this file exist' report."""

import csv
import json

import pytest

from diskcleaner import organizer, reports
from diskcleaner.cli import main
from diskcleaner.config import ConfigError
from diskcleaner.dedupe import apply_plan, load_plan
from diskcleaner.duplicates import KEEP, REMOVE, SKIP, find_duplicates
from diskcleaner.index import Index
from diskcleaner.scanner import scan
from diskcleaner.scope import Scope, resolve_scope


def _find(cfg, folders):
    scope = resolve_scope(cfg, folders)
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        return find_duplicates(cfg, idx, scope)


def _actions(result):
    return {(g.tier, m.path): m.action for g in result.groups for m in g.members}


# ---- scope resolution -----------------------------------------------------
def test_resolve_scope(cfg):
    assert resolve_scope(cfg, None).whole_disk
    assert resolve_scope(cfg, ["."]).whole_disk
    assert resolve_scope(cfg, ["recup_dir.1/"]).folders == ("recup_dir.1",)
    assert resolve_scope(cfg, [str(cfg.root / "Docs")]).folders == ("Docs",)  # absolute path pasted from Finder
    for bad in (["Not A Folder"], ["../x"], ["/tmp"], ["_DiskCleaner/Quarantine"]):
        if bad == ["_DiskCleaner/Quarantine"]:
            (cfg.root / bad[0]).mkdir(parents=True)
        with pytest.raises(ConfigError):
            resolve_scope(cfg, bad)


def test_scope_contains_is_prefix_on_folder_boundaries():
    s = Scope(("Photos/2015",))
    assert s.contains("Photos/2015/a.jpg") and s.contains("photos/2015/x/b.jpg")
    assert not s.contains("Photos/2015 Goa/a.jpg") and not s.contains("Photos/a.jpg")


# ---- duplicate finding with a scope --------------------------------------
def test_scoped_run_only_removes_inside_folder(cfg):
    res = _find(cfg, ["recup_dir.1"])
    a = _actions(res)
    assert a[("exact", "recup_dir.1/f1234567.jpg")] == REMOVE
    assert a[("exact", "Goa Trip 2015/IMG_0001.jpg")] == KEEP          # best copy, elsewhere
    assert a[("exact", "Recovered/Goa Trip 2015/IMG_0001.jpg")] == SKIP  # outside: reported, untouched
    removable = [p for (_t, p), act in a.items() if act == REMOVE]
    assert all(p.startswith("recup_dir.1/") for p in removable)
    assert not any(p.startswith("Docs/") for (_t, p) in a)  # unrelated groups are not reported


def test_scoped_run_on_folder_holding_the_best_copy(cfg):
    res = _find(cfg, ["Goa Trip 2015"])
    a = _actions(res)
    assert REMOVE not in a.values()  # nothing inside is a worse copy of anything
    assert a[("exact", "recup_dir.1/f1234567.jpg")] == SKIP
    assert a[("pixel", "Stripped/IMG_0001.jpg")] == SKIP
    assert a[("similar", "Thumbs/IMG_0001_small.jpg")] == SKIP


def test_json_lists_where_every_copy_is(cfg):
    res = _find(cfg, ["recup_dir.1"])
    cfg.ensure_state_dirs()
    jp = cfg.reports_dir / "d.json"
    reports.write_duplicates_json(res, cfg, jp, cfg.reports_dir / "d.csv")
    doc = json.loads(jp.read_text())
    assert doc["selected_folders"] == ["recup_dir.1"]
    (entry,) = doc["folders"]["recup_dir.1"]
    assert entry["path"] == "recup_dir.1/f1234567.jpg"
    assert entry["action"] == REMOVE
    assert entry["kept_copy"] == "Goa Trip 2015/IMG_0001.jpg"
    assert entry["exists_elsewhere"] and entry["exists_outside_selected_folders"]
    copies = {c["path"]: c for c in entry["other_copies"]}
    assert copies["Goa Trip 2015/IMG_0001.jpg"]["action"] == KEEP
    assert copies["Recovered/Goa Trip 2015/IMG_0001.jpg"]["action"] == SKIP
    assert copies["Recovered/Goa Trip 2015/IMG_0001.jpg"]["match"].startswith("identical file")
    assert doc["summary"]["to_remove"] == 1
    assert doc["summary"]["by_folder"]["recup_dir.1"]["remove_files"] == 1


def test_json_whole_disk_merges_tiers_per_file(cfg):
    res = _find(cfg, None)
    view = reports.per_file_view(res)
    orig = view["Goa Trip 2015/IMG_0001.jpg"]
    assert orig["action"] == KEEP
    matches = {c["path"]: c["match"] for c in orig["other_copies"]}
    assert matches["recup_dir.1/f1234567.jpg"].startswith("identical file")
    assert matches["Stripped/IMG_0001.jpg"].startswith("identical picture")
    assert matches["Thumbs/IMG_0001_small.jpg"].startswith("looks similar")
    assert view["Stripped/IMG_0001.jpg"]["action"] == REMOVE
    assert view["Stripped/IMG_0001.jpg"]["kept_copy"] == "Goa Trip 2015/IMG_0001.jpg"


# ---- dedupe with scope ---------------------------------------------------
def _csv(cfg, res):
    cfg.ensure_state_dirs()
    p = cfg.reports_dir / "dups.csv"
    reports.write_duplicates_csv(res, p)
    return p


def test_scoped_dedupe_leaves_outside_copies(cfg):
    p = _csv(cfg, _find(cfg, ["recup_dir.1"]))
    out = apply_plan(cfg, load_plan(p, cfg))
    assert out.moved == 1
    assert not (cfg.root / "recup_dir.1/f1234567.jpg").exists()
    assert (cfg.root / "Recovered/Goa Trip 2015/IMG_0001.jpg").exists()
    assert (cfg.root / "Goa Trip 2015/IMG_0001.jpg").exists()


def test_folder_filter_narrows_a_whole_disk_plan(cfg):
    p = _csv(cfg, _find(cfg, None))
    plan = load_plan(p, cfg, scope=Scope(("Docs",)))
    assert [r.path for r in plan.removals] == ["Docs/report - Copy.pdf"]
    assert plan.outside_scope == 3


def test_skip_copy_is_never_used_as_keeper(cfg):
    p = _csv(cfg, _find(cfg, ["recup_dir.1"]))
    with open(p, newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:  # user turns the kept copy into SKIP -> only SKIP copies remain outside
        if r["action"] == "KEEP":
            r["action"] = "SKIP"
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    plan = load_plan(p, cfg)
    assert plan.removals == [] and any("no copy would be kept" in g for g in plan.refused_groups)


def test_organize_scope(cfg):
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        items = organizer.build_plan(cfg, idx, resolve_scope(cfg, ["Camera"]))
    assert [i.src for i in items] == ["Camera/IMG_20170812_101500.jpg"]


# ---- CLI -----------------------------------------------------------------
def test_cli_folder_by_folder(disk, capsys):
    root, cfg_path = disk
    c = ["-c", str(cfg_path)]
    assert main(c + ["scan"]) == 0
    assert main(c + ["folders"]) == 0
    out = capsys.readouterr().out
    assert "recup_dir.1" in out and "Goa Trip 2015" in out

    assert main(c + ["duplicates", "--folder", "recup_dir.1"]) == 0
    out = capsys.readouterr().out
    assert "Proposed for removal: 1 files" in out and ".json" in out
    jsons = sorted((cfg_path.parent / "state/reports").glob("duplicates_*.json"))
    assert json.loads(jsons[-1].read_text())["selected_folders"] == ["recup_dir.1"]

    # dedupe picks up the report's folders automatically
    assert main(c + ["dedupe"]) == 0
    out = capsys.readouterr().out
    assert "Folders being cleaned: recup_dir.1" in out and "Files to move to quarantine: 1 " in out
    assert main(c + ["dedupe", "--apply", "--yes"]) == 0
    assert (root / "Recovered/Goa Trip 2015/IMG_0001.jpg").exists()
    assert not (root / "recup_dir.1/f1234567.jpg").exists()

    # "where" shows the remaining copies (the quarantine folder is not indexed)
    capsys.readouterr()
    assert main(c + ["scan"]) == 0
    assert main(c + ["where", "Goa Trip 2015/IMG_0001.jpg"]) == 0
    out = capsys.readouterr().out
    assert "identical file:    Recovered/Goa Trip 2015/IMG_0001.jpg" in out
    assert "identical picture: Stripped/IMG_0001.jpg" in out

    # typo in a folder name is an error, not a silent whole-disk run
    assert main(c + ["duplicates", "--folder", "recup_dir.99"]) == 2


def test_config_scope_folders(disk, capsys):
    root, cfg_path = disk
    cfg_path.write_text(cfg_path.read_text() + '\n[scope]\nfolders = ["Docs"]\n')
    assert main(["-c", str(cfg_path), "duplicates"]) == 0
    out = capsys.readouterr().out
    assert "selected: Docs" in out and "Proposed for removal: 1 files" in out
