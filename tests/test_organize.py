import shutil

from diskcleaner import hashing, organizer
from diskcleaner.index import Index
from diskcleaner.safeops import undo
from diskcleaner.scanner import scan
from tests.conftest import make_image


def _plan(cfg):
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        return {i.src: i for i in organizer.build_plan(cfg, idx)}


def test_plan_targets(cfg):
    plan = _plan(cfg)
    dst = {k: v.dst for k, v in plan.items()}
    assert dst["Goa Trip 2015/IMG_0001.jpg"] == "_Organized/Photos/2015/2015-06 Goa Trip/IMG_0001.jpg"
    assert dst["Family/2016/07/beach.png"] == "_Organized/Photos/2016/2016-07 Family/beach.png"
    assert dst["Camera/IMG_20170812_101500.jpg"] == "_Organized/Photos/2017/2017-08/IMG_20170812_101500.jpg"
    assert dst["DCIM/100CANON/MVI_0001.mp4"] == "_Organized/Videos/2018/2018-12/MVI_0001.mp4"
    # recovered copy with the same name & date gets a unique name, never a clash
    assert dst["Recovered/Goa Trip 2015/IMG_0001.jpg"] == "_Organized/Photos/2015/2015-06 Goa Trip/IMG_0001 (2).jpg"
    # mtime is the recovery date (ignored) and nothing else dates this one
    assert dst["Thumbs/IMG_0001_small.jpg"] == "_Organized/Photos/Unknown Date/Unsorted/IMG_0001_small.jpg"
    # documents are not organized by default
    assert not any(k.startswith("Docs/") for k in plan)
    assert plan["Goa Trip 2015/IMG_0001.jpg"].date_source == "exif"
    assert len({v.dst.lower() for v in plan.values()}) == len(plan)


def test_apply_and_undo_move(cfg):
    root = cfg.root
    before = {p.relative_to(root).as_posix(): hashing.full_hash(p) for p in root.rglob("*") if p.is_file()}
    items = list(_plan(cfg).values())
    with Index(cfg.db_path) as idx:
        res = organizer.apply_plan(cfg, items, idx)
    assert res.done == len(items) and not res.skipped
    assert (root / "_Organized/Photos/2015/2015-06 Goa Trip/IMG_0001.jpg").exists()
    assert not (root / "Goa Trip 2015/IMG_0001.jpg").exists()
    # same multiset of contents - nothing lost or altered
    after = sorted(hashing.full_hash(p) for p in root.rglob("*") if p.is_file())
    assert after == sorted(before.values())

    u = undo(cfg, res.manifest)
    assert u.restored == len(items) and not u.problems
    restored = {p.relative_to(root).as_posix(): hashing.full_hash(p) for p in root.rglob("*") if p.is_file()}
    assert restored == before
    assert not (root / "_Organized").exists() or not any((root / "_Organized").rglob("*.*"))


def test_apply_copy_mode_and_undo(cfg):
    items = list(_plan(cfg).values())
    res = organizer.apply_plan(cfg, items, mode="copy")
    assert res.done == len(items)
    assert (cfg.root / "Goa Trip 2015/IMG_0001.jpg").exists()  # original untouched
    assert (cfg.root / "_Organized/Photos/2015/2015-06 Goa Trip/IMG_0001.jpg").exists()
    u = undo(cfg, res.manifest)
    assert u.copies_removed == len(items)
    assert not (cfg.root / "_Organized/Photos/2015/2015-06 Goa Trip/IMG_0001.jpg").exists()
    assert (cfg.root / "Goa Trip 2015/IMG_0001.jpg").exists()


def test_existing_destination_never_overwritten(cfg):
    plan = _plan(cfg)
    item = plan["Camera/IMG_20170812_101500.jpg"]
    blocker = cfg.root / item.dst
    make_image(blocker, seed=99)  # a different file appears at the destination
    blocker_hash = hashing.full_hash(blocker)
    res = organizer.apply_plan(cfg, [item])
    assert res.done == 1
    assert hashing.full_hash(blocker) == blocker_hash
    assert (blocker.parent / "IMG_20170812_101500 (2).jpg").exists()


def test_identical_destination_is_left_alone(cfg):
    plan = _plan(cfg)
    item = plan["Camera/IMG_20170812_101500.jpg"]
    (cfg.root / item.dst).parent.mkdir(parents=True)
    shutil.copy2(cfg.root / item.src, cfg.root / item.dst)
    res = organizer.apply_plan(cfg, [item])
    assert res.done == 0 and res.identical_skipped == 1
    assert (cfg.root / item.src).exists()


def test_destination_outside_organized_refused(cfg):
    item = _plan(cfg)["Camera/IMG_20170812_101500.jpg"]
    item.dst = "Somewhere/else.jpg"
    res = organizer.apply_plan(cfg, [item])
    assert res.done == 0 and (cfg.root / item.src).exists()


def test_already_organized_files_are_not_replanned(cfg):
    items = list(_plan(cfg).values())
    organizer.apply_plan(cfg, items)
    assert _plan(cfg) == {}


def test_empty_dir_cleanup(cfg):
    items = list(_plan(cfg).values())
    organizer.apply_plan(cfg, items)
    (cfg.root / "Camera" / ".DS_Store").write_bytes(b"junk")
    empties = organizer.find_empty_dirs(cfg)
    rel = {d.relative_to(cfg.root).as_posix() for d in empties}
    assert {"Camera", "DCIM/100CANON", "DCIM", "Family/2016/07", "Family/2016", "Family"} <= rel
    assert "Docs" not in rel and ".hidden" not in rel
    organizer.remove_empty_dirs(empties)
    assert not (cfg.root / "Camera").exists()
    assert (cfg.root / "Docs/report.pdf").exists()
    assert (cfg.root / ".hidden/secret.jpg").exists()


def test_extensionless_file_gets_real_extension_when_organized(cfg):
    p = make_image(cfg.root / "Files Lost Original Directory/DIR2/x.jpg", seed=5, exif_date="2012:03:04 10:00:00")
    p.rename(p.with_name("sector2538737"))
    item = _plan(cfg)["Files Lost Original Directory/DIR2/sector2538737"]
    assert item.dst == "_Organized/Photos/2012/2012-03/sector2538737.jpg"  # recovery folder is not an event name


def _device_disk(cfg):
    """Existing Photos/<device>/... like the user's disk, plus a recovery folder."""
    ep = cfg.root / "Existing Photos"
    make_image(ep / "Samsung A8/DCIM/Camera/IMG_1.jpg", seed=31, exif_date="2019:06:02 10:00:00")
    make_image(ep / "Samsung A8/Pictures/Screenshots/shot.jpg", seed=32, exif_date="2019:07:01 10:00:00")
    make_image(ep / "Windows Folders/Win 10/Birthday/IMG_5.jpg", seed=33, exif_date="2016:03:09 10:00:00")
    make_image(ep / "ChromeOS Photos/Netherlands '22/a.jpg", seed=34, exif_date="2022:10:06 10:00:00")
    make_image(ep / "Samsung A8/2018/2018-01/old.jpg", seed=35, exif_date="2018:01:05 10:00:00")
    make_image(ep / "Samsung A8/nodate.jpg", seed=36)
    (ep / "System Volume Information").mkdir()
    make_image(cfg.root / "Files Lost Original Directory/DIR69/sector1.jpg", seed=37, exif_date="2013:04:14 12:35:05")
    cfg.organize_inside = ["Existing Photos/*"]


def test_organize_inside_each_device_folder(cfg):
    _device_disk(cfg)
    dst = {k: v.dst for k, v in _plan(cfg).items()}
    ep = "Existing Photos"
    assert dst[f"{ep}/Samsung A8/DCIM/Camera/IMG_1.jpg"] == f"{ep}/Samsung A8/2019/2019-06/IMG_1.jpg"
    assert dst[f"{ep}/Samsung A8/Pictures/Screenshots/shot.jpg"] == f"{ep}/Samsung A8/2019/2019-07/shot.jpg"
    assert dst[f"{ep}/Windows Folders/Win 10/Birthday/IMG_5.jpg"] == f"{ep}/Windows Folders/2016/2016-03 Birthday/IMG_5.jpg"
    assert dst[f"{ep}/ChromeOS Photos/Netherlands '22/a.jpg"] == f"{ep}/ChromeOS Photos/2022/2022-10 Netherlands '22/a.jpg"
    assert dst[f"{ep}/Samsung A8/nodate.jpg"] == f"{ep}/Samsung A8/Unknown Date/nodate.jpg"
    # already in the right place -> not in the plan at all
    assert f"{ep}/Samsung A8/2018/2018-01/old.jpg" not in dst
    # recovery folders still go to the uniform tree
    assert dst["Files Lost Original Directory/DIR69/sector1.jpg"] == "_Organized/Photos/2013/2013-04/sector1.jpg"
    # the device folder name itself is never used as an event name
    assert not any("Samsung A8/2019/2019-06 Samsung" in d for d in dst.values())


def test_organize_inside_apply_and_undo(cfg):
    _device_disk(cfg)
    items = list(_plan(cfg).values())
    res = organizer.apply_plan(cfg, items)
    assert res.done == len(items) and not res.skipped
    assert (cfg.root / "Existing Photos/Samsung A8/2019/2019-06/IMG_1.jpg").exists()
    assert undo(cfg, res.manifest).restored == len(items)
    assert (cfg.root / "Existing Photos/Samsung A8/DCIM/Camera/IMG_1.jpg").exists()


def test_organize_inside_never_crosses_folders(cfg):
    _device_disk(cfg)
    item = _plan(cfg)["Existing Photos/Samsung A8/DCIM/Camera/IMG_1.jpg"]
    item.dst = "Existing Photos/Windows Folders/2019/2019-06/IMG_1.jpg"  # tampered plan
    res = organizer.apply_plan(cfg, [item])
    assert res.done == 0 and (cfg.root / item.src).exists()
