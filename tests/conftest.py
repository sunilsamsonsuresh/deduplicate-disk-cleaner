import os
import random
import shutil
import struct
import sys
import datetime as dt
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diskcleaner import helpers  # noqa: E402
from diskcleaner.config import load_config  # noqa: E402

helpers.QUIET = True


def make_image(path: Path, seed: int, size=(320, 240), exif_date: str | None = None, quality=90) -> Path:
    """Textured test image (gradient + blocks) so perceptual hashes are meaningful."""
    rnd = random.Random(seed)
    w, h = size
    im = Image.new("RGB", (w, h))
    px = im.load()
    blocks = [(rnd.randrange(w), rnd.randrange(h), rnd.randrange(20, w // 2), rnd.randrange(20, h // 2),
               (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))) for _ in range(8)]
    for y in range(h):
        for x in range(w):
            c = ((x * 255) // w, (y * 255) // h, ((x + y) * 128) // (w + h))
            for bx, by, bw, bh, col in blocks:
                if bx <= x < bx + bw and by <= y < by + bh:
                    c = col
            px[x, y] = c
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if exif_date:
        exif = Image.Exif()
        exif[0x0110] = "TestCam"
        exif.get_ifd(0x8769)[36867] = exif_date
        kwargs["exif"] = exif
    if path.suffix.lower() in (".jpg", ".jpeg"):
        im.save(path, "JPEG", quality=quality, **kwargs)
    else:
        im.save(path, **kwargs)
    return path


def make_mp4(path: Path, created: dt.datetime | None, payload: bytes = b"\x00" * 2048) -> Path:
    def atom(typ: bytes, body: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(body), typ) + body

    secs = int((created - dt.datetime(1904, 1, 1)).total_seconds()) if created else 0
    mvhd = atom(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">III", secs, secs, 1000) + b"\x00" * 84)
    data = atom(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + atom(b"mdat", payload) + atom(b"moov", mvhd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_config(tmp: Path, root: Path, organize: str = 'ignore_mtime_after = "2026-09-30"', extra: str = "") -> Path:
    """Config for tests. `organize` lines go in [organize]; `extra` is appended (own sections)."""
    cfg = tmp / "config.toml"
    cfg.write_text(
        f'''[disk]
root = "{root}"
[state]
dir = "{tmp / 'state'}"
[organize]
{organize}
{extra}
''',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def disk(tmp_path):
    """A small fake recovered disk.

    Goa Trip 2015/IMG_0001.jpg            original (EXIF 2015-06-12)
    recup_dir.1/f1234567.jpg              exact copy (PhotoRec style)
    Recovered/Goa Trip 2015/IMG_0001.jpg  exact copy
    Stripped/IMG_0001.jpg                 same pixels, no EXIF   -> pixel tier
    Thumbs/IMG_0001_small.jpg             resized                -> similar tier
    Family/2016/07/beach.png              dated by folders
    Camera/IMG_20170812_101500.jpg        dated by file name
    DCIM/100CANON/MVI_0001.mp4            dated by video metadata (2018-12-25)
    Docs/report.pdf + Docs/report - Copy.pdf   exact copies (non-media)
    empty1.txt, empty2.txt                0-byte, never duplicates
    .hidden/secret.jpg                    ignored
    """
    root = tmp_path / "disk"
    orig = make_image(root / "Goa Trip 2015" / "IMG_0001.jpg", seed=1, exif_date="2015:06:12 10:30:00")
    (root / "recup_dir.1").mkdir(parents=True)
    shutil.copy2(orig, root / "recup_dir.1" / "f1234567.jpg")
    (root / "Recovered" / "Goa Trip 2015").mkdir(parents=True)
    shutil.copy2(orig, root / "Recovered" / "Goa Trip 2015" / "IMG_0001.jpg")
    make_image(root / "Stripped" / "IMG_0001.jpg", seed=1)  # same pixels, no EXIF
    with Image.open(orig) as im:
        (root / "Thumbs").mkdir()
        im.resize((160, 120)).save(root / "Thumbs" / "IMG_0001_small.jpg", quality=70)
    make_image(root / "Family" / "2016" / "07" / "beach.png", seed=2)
    make_image(root / "Camera" / "IMG_20170812_101500.jpg", seed=3)
    make_mp4(root / "DCIM" / "100CANON" / "MVI_0001.mp4", dt.datetime(2018, 12, 25, 9, 0, 0))
    (root / "Docs").mkdir()
    (root / "Docs" / "report.pdf").write_bytes(b"%PDF-1.4 " + os.urandom(200_000))
    shutil.copy2(root / "Docs" / "report.pdf", root / "Docs" / "report - Copy.pdf")
    (root / "empty1.txt").write_bytes(b"")
    (root / "empty2.txt").write_bytes(b"")
    make_image(root / ".hidden" / "secret.jpg", seed=1, exif_date="2015:06:12 10:30:00")
    # Make all modified-times look like "recovery day" except where tests override.
    recovery = dt.datetime(2026, 10, 1, 12, 0).timestamp()
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (recovery, recovery))
    cfg_path = write_config(tmp_path, root)
    return root, cfg_path


@pytest.fixture
def cfg(disk):
    root, cfg_path = disk
    return load_config(cfg_path)
