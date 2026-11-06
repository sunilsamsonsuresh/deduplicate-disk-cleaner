import datetime as dt

import pytest

from diskcleaner.dates import (
    clean_event_name,
    date_from_filename,
    date_from_folders,
    event_name_for,
    is_meaningful_folder,
    resolve_date,
)

TODAY = dt.date(2026, 11, 5)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("IMG_20150612_143000.jpg", "2015-06-12"),
        ("VID_20150612_143000.mp4", "2015-06-12"),
        ("IMG-20150612-WA0001.jpg", "2015-06-12"),
        ("2015-06-12 14.30.00.jpg", "2015-06-12"),
        ("Screenshot_2015-06-12-14-30-00.png", "2015-06-12"),
        ("PXL_20210101_120000123.jpg", "2021-01-01"),
        ("photo 12-06-2015.jpg", "2015-06-12"),
        ("FB_IMG_1434112200000.jpg", "2015-06-12"),
        ("1434112200.jpg", "2015-06-12"),
    ],
)
def test_filename_dates(name, expected):
    d = date_from_filename(name, today=TODAY)
    assert d is not None and d.iso() == expected and d.source == "filename"


@pytest.mark.parametrize(
    "name",
    ["f20150612.jpg",        # PhotoRec sector number, not a date
     "IMG_0001.jpg",
     "DSC_1234.JPG",
     "IMG_20991231_000000.jpg",  # future
     "IMG_19800101.jpg",          # before min_year
     "20151340.jpg"],             # invalid month/day
)
def test_filename_without_dates(name):
    assert date_from_filename(name, today=TODAY) is None


@pytest.mark.parametrize(
    "folder, expected",
    [
        ("Photos/Goa Trip 2015", "2015"),
        ("Photos/2015-06 Goa", "2015-06"),
        ("Photos/June 2015", "2015-06"),
        ("Photos/2015/06", "2015-06"),
        ("Photos/2015/June", "2015-06"),
        ("Photos/2015/Diwali", "2015"),
        ("Photos/2015-12-25 Christmas", "2015-12-25"),
        ("Pics/Dec 2014 Kerala", "2014-12"),
    ],
)
def test_folder_dates(folder, expected):
    d = date_from_folders(folder, today=TODAY)
    assert d is not None and d.iso() == expected


def test_folder_word_is_not_a_month():
    # "Marriage 2015" must not be read as March 2015
    d = date_from_folders("Marriage 2015", today=TODAY)
    assert d.iso() == "2015"


def test_folder_without_dates():
    assert date_from_folders("recup_dir.12", today=TODAY) is None
    assert date_from_folders("Holidays", today=TODAY) is None


@pytest.mark.parametrize(
    "name, meaningful",
    [
        ("Goa Trip 2015", True),
        ("Marriage", True),
        ("Kerala Holidays", True),
        ("DCIM", False),
        ("100CANON", False),
        ("recup_dir.12", False),
        ("FOUND.000", False),
        ("New Folder (2)", False),
        ("2015", False),
        ("2015-06", False),
        ("June", False),
        ("Camera", False),
        ("WhatsApp Images", False),
        ("$RECYCLE.BIN", False),
    ],
)
def test_meaningful_folder(name, meaningful):
    assert is_meaningful_folder(name) is meaningful


def test_event_names():
    assert clean_event_name("2015-06 Goa Trip") == "Goa Trip"
    assert clean_event_name("Goa_Trip_2015") == "Goa Trip"
    assert event_name_for("Photos/Goa Trip 2015/DCIM/100CANON") == "Goa Trip"
    assert event_name_for("Photos/Goa Trip 2015/100CANON") == "Goa Trip"
    assert event_name_for("Kerala/2015/06") == "Kerala"
    assert event_name_for("Kerala/2015/June") == "Kerala"
    assert event_name_for("recup_dir.3") is None
    # a generic folder stops the walk: a backup root is not an event
    assert event_name_for("My Old Laptop/New Folder") is None
    assert event_name_for("My Old Laptop/Photos/2017/June") is None


def _ns(y, m, d):
    return int(dt.datetime(y, m, d, 12).timestamp() * 1e9)


def test_resolve_priority():
    kw = dict(min_year=1990, use_mtime=True, ignore_mtime_after=None, today=TODAY)
    # metadata beats everything
    d = resolve_date(rel_path="2019/IMG_20180101_000000.jpg", meta_date="2015-06-12T10:00:00",
                     meta_source="exif", mtime_ns=_ns(2020, 1, 1), **kw)
    assert (d.iso(), d.source) == ("2015-06-12", "exif")
    # then file name
    d = resolve_date(rel_path="2019/IMG_20180101_000000.jpg", meta_date=None, meta_source=None,
                     mtime_ns=_ns(2020, 1, 1), **kw)
    assert (d.iso(), d.source) == ("2018-01-01", "filename")
    # then folder
    d = resolve_date(rel_path="Trip 2019/IMG_1.jpg", meta_date=None, meta_source=None, mtime_ns=_ns(2020, 1, 1), **kw)
    assert (d.iso(), d.source) == ("2019", "folder")
    # then mtime
    d = resolve_date(rel_path="x/IMG_1.jpg", meta_date=None, meta_source=None, mtime_ns=_ns(2020, 1, 1), **kw)
    assert (d.iso(), d.source) == ("2020-01-01", "mtime")


def test_resolve_ignores_recovery_mtime():
    d = resolve_date(rel_path="x/IMG_1.jpg", meta_date=None, meta_source=None, mtime_ns=_ns(2026, 10, 1),
                     min_year=1990, use_mtime=True, ignore_mtime_after=dt.date(2026, 9, 30), today=TODAY)
    assert not d.known
    d = resolve_date(rel_path="x/IMG_1.jpg", meta_date=None, meta_source=None, mtime_ns=_ns(2016, 10, 1),
                     min_year=1990, use_mtime=False, ignore_mtime_after=None, today=TODAY)
    assert not d.known


def test_resolve_rejects_bogus_exif():
    # Camera clock never set -> 1980; fall through to the folder
    d = resolve_date(rel_path="Goa 2015/IMG_1.jpg", meta_date="1980-01-01T00:00:00", meta_source="exif",
                     mtime_ns=0, min_year=1990, use_mtime=False, ignore_mtime_after=None, today=TODAY)
    assert (d.iso(), d.source) == ("2015", "folder")


def test_resolve_ignores_specific_mtime_days():
    kw = dict(rel_path="x/sector1.jpg", meta_date=None, meta_source=None, min_year=1990,
              use_mtime=True, ignore_mtime_after=None, today=TODAY)
    assert not resolve_date(mtime_ns=_ns(2015, 10, 21), ignore_mtime_dates={dt.date(2015, 10, 21)}, **kw).known
    assert resolve_date(mtime_ns=_ns(2015, 10, 22), ignore_mtime_dates={dt.date(2015, 10, 21)}, **kw).iso() == "2015-10-22"
