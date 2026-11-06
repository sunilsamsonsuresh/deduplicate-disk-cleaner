import datetime as dt

from PIL import Image

from diskcleaner import hashing, media
from tests.conftest import make_image, make_mp4


def test_exif_date_and_dimensions(tmp_path):
    p = make_image(tmp_path / "a.jpg", seed=1, size=(300, 200), exif_date="2015:06:12 10:30:00")
    info = media.image_info(p)
    assert (info["width"], info["height"]) == (300, 200)
    assert info["date"] == dt.datetime(2015, 6, 12, 10, 30)
    assert info["has_exif"]


def test_no_exif(tmp_path):
    info = media.image_info(make_image(tmp_path / "a.png", seed=1))
    assert info["date"] is None and not info["has_exif"]


def test_bogus_exif_values():
    assert media.parse_exif_datetime("0000:00:00 00:00:00") is None
    assert media.parse_exif_datetime("") is None
    assert media.parse_exif_datetime("2015:06:12 10:30:00.123") == dt.datetime(2015, 6, 12, 10, 30)


def test_mp4_creation_date(tmp_path):
    p = make_mp4(tmp_path / "v.mp4", dt.datetime(2018, 12, 25, 9, 0, 0))
    assert media.video_creation_date(p) == dt.datetime(2018, 12, 25, 9, 0, 0)
    assert media.video_creation_date(make_mp4(tmp_path / "z.mp4", None)) is None
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x01garbage" * 50)
    assert media.video_creation_date(junk) is None


def test_pixel_hash_ignores_metadata(tmp_path):
    a = make_image(tmp_path / "a.jpg", seed=1, exif_date="2015:06:12 10:30:00")
    b = make_image(tmp_path / "b.jpg", seed=1)
    assert hashing.full_hash(a) != hashing.full_hash(b)  # different bytes
    assert media.pixel_hash(a) == media.pixel_hash(b)    # same picture
    c = make_image(tmp_path / "c.jpg", seed=2)
    assert media.pixel_hash(a) != media.pixel_hash(c)


def test_dhash_similarity(tmp_path):
    a = make_image(tmp_path / "a.jpg", seed=1, size=(400, 300))
    with Image.open(a) as im:
        im.resize((200, 150)).save(tmp_path / "small.jpg", quality=60)
    other = make_image(tmp_path / "o.jpg", seed=7, size=(400, 300))
    ha, hs, ho = media.dhash128(a), media.dhash128(tmp_path / "small.jpg"), media.dhash128(other)
    assert media.hamming(ha, hs) <= 6
    assert media.hamming(ha, ho) > 20


def test_partial_and_full_hash(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    data = bytes(range(256)) * 2000
    a.write_bytes(data)
    b.write_bytes(data[:250_000] + b"X" + data[250_001:])  # differs only in the middle
    assert hashing.partial_hash(a) == hashing.partial_hash(b)   # quick check can't tell...
    assert hashing.full_hash(a) != hashing.full_hash(b)         # ...full hash can
