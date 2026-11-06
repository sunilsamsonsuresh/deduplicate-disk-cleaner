from diskcleaner.duplicates import KEEP, REMOVE, REVIEW, BKTree, find_duplicates
from diskcleaner.index import Index
from diskcleaner.scanner import scan


def _run(cfg):
    with Index(cfg.db_path) as idx:
        res = scan(cfg, idx)
        dups = find_duplicates(cfg, idx)
    return res, dups


def _actions(group):
    return {m.path: m.action for m in group.members}


def test_scan_skips_hidden_and_reads_metadata(cfg):
    res, _ = _run(cfg)
    with Index(cfg.db_path) as idx:
        paths = {r.path for r in idx.all_files()}
        assert ".hidden/secret.jpg" not in paths
        assert "empty1.txt" not in paths  # below min_size_bytes
        row = idx.get("Goa Trip 2015/IMG_0001.jpg")
        assert row.meta_date.startswith("2015-06-12") and row.has_exif
        assert idx.get("DCIM/100CANON/MVI_0001.mp4").meta_date.startswith("2018-12-25")
    assert res.zero_byte == 2


def test_exact_groups_and_keeper(cfg):
    _, dups = _run(cfg)
    exact = dups.by_tier("exact")
    photo = next(g for g in exact if "Goa Trip 2015/IMG_0001.jpg" in _actions(g))
    assert _actions(photo) == {
        "Goa Trip 2015/IMG_0001.jpg": KEEP,
        "recup_dir.1/f1234567.jpg": REMOVE,
        "Recovered/Goa Trip 2015/IMG_0001.jpg": REMOVE,
    }
    doc = next(g for g in exact if "Docs/report.pdf" in _actions(g))
    assert _actions(doc) == {"Docs/report.pdf": KEEP, "Docs/report - Copy.pdf": REMOVE}
    assert len(exact) == 2  # empty files never form a group


def test_pixel_tier_keeps_exif_copy(cfg):
    _, dups = _run(cfg)
    (g,) = dups.by_tier("pixel")
    assert _actions(g) == {"Goa Trip 2015/IMG_0001.jpg": KEEP, "Stripped/IMG_0001.jpg": REMOVE}


def test_similar_tier_is_review_only(cfg):
    _, dups = _run(cfg)
    (g,) = dups.by_tier("similar")
    assert _actions(g) == {"Goa Trip 2015/IMG_0001.jpg": KEEP, "Thumbs/IMG_0001_small.jpg": REVIEW}


def test_each_file_in_at_most_one_removal(cfg):
    _, dups = _run(cfg)
    removed = [m.path for g in dups.groups for m in g.members if m.action != KEEP]
    assert len(removed) == len(set(removed))


def test_rescan_uses_cache(cfg):
    _run(cfg)
    res, _ = _run(cfg)
    assert res.new_or_changed == 0 and res.metadata_read == 0


def test_tiers_can_be_disabled(cfg):
    cfg.find_pixel_identical = False
    cfg.find_similar = False
    _, dups = _run(cfg)
    assert {g.tier for g in dups.groups} == {"exact"}


def test_bktree_matches_bruteforce():
    import random

    rnd = random.Random(3)
    vals = [rnd.getrandbits(128) for _ in range(400)]
    vals += [v ^ (1 << rnd.randrange(128)) for v in vals[:50]]  # near neighbours
    tree = BKTree()
    for i, v in enumerate(vals):
        tree.add(v, i)
    for i in range(0, len(vals), 37):
        got = {j for j, _ in tree.search(vals[i], 6)}
        want = {j for j, v in enumerate(vals) if (v ^ vals[i]).bit_count() <= 6}
        assert got == want


def test_changing_disk_root_rebuilds_index(cfg, tmp_path):
    _run(cfg)
    other = tmp_path / "other_disk"
    (other / "Goa Trip 2015").mkdir(parents=True)
    (other / "Goa Trip 2015" / "IMG_0001.jpg").write_bytes(b"not the same file")
    cfg.root = other
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        row = idx.get("Goa Trip 2015/IMG_0001.jpg")
        assert row.size == len(b"not the same file") and row.full_hash is None
        assert idx.count() == 1


def test_sector_carved_copy_is_pixel_duplicate(cfg):
    """Real-world case (DIR69 on the user's disk): a sector-carving tool recovered one
    photo twice. The second carve starts later, so it is the first file minus its
    EXIF header: different size, same pixels. It must be flagged, keeping the EXIF copy."""
    import struct

    from PIL import Image

    from tests.conftest import make_image

    d = cfg.root / "Files Lost Original Directory" / "DIR69"
    tail = make_image(d / "sector255116147.jpg", seed=11, size=(400, 300))   # no EXIF
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2013:04:14 12:35:05"
    payload = exif.tobytes()
    if not payload.startswith(b"Exif"):
        payload = b"Exif\x00\x00" + payload
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    full = d / "sector255116120.jpg"
    full.write_bytes(b"\xff\xd8" + app1 + tail.read_bytes()[2:])
    assert full.read_bytes().endswith(tail.read_bytes()[2:]) and full.stat().st_size > tail.stat().st_size

    _, dups = _run(cfg)
    assert not any("DIR69" in m.path for g in dups.by_tier("exact") for m in g.members)
    (g,) = [g for g in dups.by_tier("pixel") if any("DIR69" in m.path for m in g.members)]
    acts = {m.path.rsplit("/", 1)[1]: m.action for m in g.members}
    assert acts == {"sector255116120.jpg": KEEP, "sector255116147.jpg": REMOVE}


def test_pixel_tier_prefers_exif_over_location(cfg):
    """A copy with EXIF in a recovery folder beats a stripped copy in a nice folder."""
    from tests.conftest import make_image

    make_image(cfg.root / "Files Lost Original Directory/DIR1/sector1000.jpg", seed=21,
               exif_date="2014:01:01 10:00:00")
    make_image(cfg.root / "Kerala Holidays/beach.jpg", seed=21)
    _, dups = _run(cfg)
    g = next(g for g in dups.by_tier("pixel") if any("Kerala" in m.path for m in g.members))
    assert g.keeper.path == "Files Lost Original Directory/DIR1/sector1000.jpg"


def test_extensionless_recovered_jpeg_is_detected(cfg):
    from tests.conftest import make_image

    p = make_image(cfg.root / "Files Lost Original Directory/DIR2/tmp.jpg", seed=1, exif_date="2015:06:12 10:30:00")
    p.rename(p.with_name("sector2538737"))
    (cfg.root / "Files Lost Original Directory/DIR2/FILE0001.CHK").write_bytes(
        (cfg.root / "Docs/report.pdf").read_bytes()[:5000])
    with Index(cfg.db_path) as idx:
        scan(cfg, idx)
        row = idx.get("Files Lost Original Directory/DIR2/sector2538737")
        assert row.ext == ".jpg" and row.category == "photos" and row.meta_date.startswith("2015-06-12")
        assert idx.get("Files Lost Original Directory/DIR2/FILE0001.CHK").ext == ".pdf"
        dups = find_duplicates(cfg, idx)
    exact = {m.path for g in dups.by_tier("exact") for m in g.members}
    assert "Files Lost Original Directory/DIR2/sector2538737" in exact  # identical to the Goa Trip photo
