"""Media metadata: image dimensions, EXIF dates, pixel hash, perceptual hash,
and MP4/MOV creation time (parsed natively, no ffmpeg needed)."""

from __future__ import annotations

import datetime as dt
import hashlib
import struct
import warnings
from pathlib import Path

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 400_000_000  # allow big panoramas, still guard against bombs
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

try:  # optional HEIC/HEIF support (iPhone photos)
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover
    HEIF_AVAILABLE = False

from .filetypes import DECODABLE_IMAGE_EXT, MP4_FAMILY_EXT

EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_DATETIME = 306


def parse_exif_datetime(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    s = str(value).strip().replace("\x00", "")
    if not s or s.startswith("0000"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y:%m:%d"):
        try:
            return dt.datetime.strptime(s[: len(dt.datetime(2000, 1, 1).strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def image_info(path: Path) -> dict:
    """Read dimensions and EXIF date from the image header (no full decode)."""
    out = {"width": None, "height": None, "date": None, "has_exif": False}
    if not PIL_AVAILABLE:
        return out
    with Image.open(path) as im:
        w, h = im.size
        exif = im.getexif()
        if exif:
            out["has_exif"] = True
            # EXIF orientation 5-8 = rotated 90/270 -> report displayed size
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
    im = Image.open(path)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    return im


def pixel_hash(path: Path) -> str:
    """Hash of the fully decoded, orientation-corrected RGB(A) pixels.
    Two files with the same pixel hash show exactly the same picture."""
    with _open_upright(path) as im:
        mode = "RGBA" if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info else "RGB"
        im = im.convert(mode)
        h = hashlib.blake2b(digest_size=20)
        h.update(f"{im.size[0]}x{im.size[1]}:{mode}".encode())
        h.update(im.tobytes())
        return h.hexdigest()


def dhash128(path: Path) -> str:
    """128-bit difference hash (64 horizontal + 64 vertical gradient bits) as hex.
    Robust to resizing, recompression and small colour changes."""
    with Image.open(path) as im:
        try:
            im.draft("L", (64, 64))  # JPEG: decode at reduced scale - much faster
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
    return (int(a, 16) ^ int(b, 16)).bit_count()


# ---------------------------------------------------------------------------
# MP4 / MOV / 3GP creation time
# ---------------------------------------------------------------------------
_MAC_EPOCH = dt.datetime(1904, 1, 1)
_CONTAINERS = {b"moov", b"trak", b"mdia"}


def _iter_atoms(fh, start: int, end: int):
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
    """Creation time from the 'mvhd' (movie header) atom; None if absent or zero."""
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
                fh.read(3)  # flags
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
    """Return {'width','height','date','date_source','has_exif'}; raises on unreadable files."""
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
