"""Read information from photos and videos: the size in pixels, the EXIF date,
the pixel hash, the look-alike hash and the MP4/MOV creation date (no ffmpeg needed)."""

import datetime as dt
import hashlib
import struct
import warnings
from pathlib import Path

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 400_000_000  # allow big panoramas, but stop "image bombs"
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

try:  # HEIC/HEIF (iPhone photos) works only if pillow-heif is installed
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover
    HEIF_AVAILABLE = False

from .file_types import DECODABLE_IMAGE_EXT, MP4_FAMILY_EXT

EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_DATETIME = 306


# The EXIF date formats that the tool knows, and the length of each one.
EXIF_DATE_FORMATS = [
    ("%Y:%m:%d %H:%M:%S", 19),
    ("%Y-%m-%d %H:%M:%S", 19),
    ("%Y:%m:%d %H:%M", 16),
    ("%Y:%m:%d", 10),
]


def parse_exif_datetime(value) -> dt.datetime | None:
    """Change an EXIF date text ("2015:06:12 10:30:00") to a datetime. Give None if it is not a date."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    s = str(value).strip().replace("\x00", "")
    if not s or s.startswith("0000"):
        return None
    for fmt, length in EXIF_DATE_FORMATS:
        try:
            return dt.datetime.strptime(s[:length], fmt)
        except ValueError:
            continue
    return None


def image_info(path: Path) -> dict:
    """Read the size and the EXIF date from the start of the image. (The tool does not open the full image.)"""
    out = {"width": None, "height": None, "date": None, "has_exif": False}
    if not PIL_AVAILABLE:
        return out
    with Image.open(path) as im:
        w, h = im.size
        exif = im.getexif()
        if exif:
            out["has_exif"] = True
            # EXIF orientation 5 to 8: the photo is turned 90 or 270 degrees. Give the size as shown.
            if exif.get(0x0112) in (5, 6, 7, 8):
                w, h = h, w
            sub = {}
            try:
                sub = exif.get_ifd(EXIF_IFD)
            except Exception:
                pass
            for tag in (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED):
                d = parse_exif_datetime(sub.get(tag))
                if d:
                    out["date"] = d
                    break
            if out["date"] is None:
                out["date"] = parse_exif_datetime(exif.get(TAG_DATETIME))
        out["width"], out["height"] = w, h
    return out


def _open_upright(path: Path):
    """Open an image and turn it upright (from the EXIF orientation)."""
    im = Image.open(path)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    return im


def pixel_hash(path: Path) -> str:
    """Make a hash from all the pixels of the image, after the tool turns it upright.
    Two files with the same pixel hash show exactly the same picture."""
    with _open_upright(path) as im:
        mode = "RGBA" if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info else "RGB"
        im = im.convert(mode)
        h = hashlib.blake2b(digest_size=20)
        h.update(f"{im.size[0]}x{im.size[1]}:{mode}".encode())
        h.update(im.tobytes())
        return h.hexdigest()


def dhash128(path: Path) -> str:
    """Make a 128-bit "look-alike" hash (a difference hash), as hex text.
    It compares the brightness of pixels next to each other, on a small grey copy.
    It stays almost the same when a photo is resized, saved again or changed a little in colour."""
    with Image.open(path) as im:
        try:
            im.draft("L", (64, 64))  # for JPEG, open a small version. This is much faster.
        except Exception:
            pass
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        g = im.convert("L")
        horiz = g.resize((9, 8), Image.Resampling.LANCZOS)
        vert = g.resize((8, 9), Image.Resampling.LANCZOS)
    hp = horiz.tobytes()
    vp = vert.tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (hp[y * 9 + x] > hp[y * 9 + x + 1])
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (vp[y * 8 + x] > vp[(y + 1) * 8 + x])
    return f"{bits:032x}"


def hamming(a: str, b: str) -> int:
    """Count the bits that are different between two hex hashes."""
    return (int(a, 16) ^ int(b, 16)).bit_count()


# ---------------------------------------------------------------------------
# MP4 / MOV / 3GP creation time
# ---------------------------------------------------------------------------
_MAC_EPOCH = dt.datetime(1904, 1, 1)
_CONTAINERS = {b"moov", b"trak", b"mdia"}


def _iter_atoms(fh, start: int, end: int):
    """Give (type, body start, end) for each part ("atom") of an MP4 file between start and end."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        header_len = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len:
            return
        yield typ, pos + header_len, pos + size
        pos += size


def video_creation_date(path: Path) -> dt.datetime | None:
    """Read the creation time from the 'mvhd' part (the movie header). Give None if it is not there or is zero."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        file_end = fh.tell()
        for typ, body, end in _iter_atoms(fh, 0, file_end):
            if typ != b"moov":
                continue
            for sub, sbody, _send in _iter_atoms(fh, body, end):
                if sub != b"mvhd":
                    continue
                fh.seek(sbody)
                version = fh.read(1)
                if not version:
                    return None
                fh.read(3)  # skip the 3 flag bytes
                if version[0] == 1:
                    data = fh.read(8)
                    if len(data) < 8:
                        return None
                    secs = struct.unpack(">Q", data)[0]
                else:
                    data = fh.read(4)
                    if len(data) < 4:
                        return None
                    secs = struct.unpack(">I", data)[0]
                if secs == 0:
                    return None
                try:
                    return _MAC_EPOCH + dt.timedelta(seconds=secs)
                except OverflowError:
                    return None
            return None
    return None


def extract_metadata(path: Path, ext: str) -> dict:
    """Give {'width', 'height', 'date', 'date_source', 'has_exif'}. Raise an error if the file cannot be read."""
    ext = ext.lower()
    out = {"width": None, "height": None, "date": None, "date_source": None, "has_exif": False}
    if ext in DECODABLE_IMAGE_EXT and PIL_AVAILABLE:
        info = image_info(path)
        out.update(width=info["width"], height=info["height"], has_exif=info["has_exif"])
        if info["date"]:
            out["date"], out["date_source"] = info["date"], "exif"
    elif ext in MP4_FAMILY_EXT:
        d = video_creation_date(path)
        if d:
            out["date"], out["date_source"] = d, "video-metadata"
    return out
